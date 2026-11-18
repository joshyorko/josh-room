"""Small stdlib-only synthetic safety checks; no provider or capture processes."""
import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from josh_room.catalog import Catalog
from josh_room.composition_safety import (
    CompositionSafetyState,
    inspect_evidence_file,
    plan_digest,
    reconcile,
)
from josh_room.local_store import ObjectRef
from josh_room.logical_jat import LogicalJat
from josh_room.restic_store import RepositoryInfo, SnapshotInventoryItem

DOMAIN = "00000000-0000-4000-8000-000000000001"
REPOSITORY = "a" * 64


def fixture():
    catalog = Catalog.empty("dimension", DOMAIN)
    descriptors, inventory, pins = {}, [], {}
    for index, room in enumerate(("child", "parent"), 1):
        descriptor = LogicalJat.from_dict({
            "format_version": 1, "payload_kind": "room-store-v1",
            "logical_jat_id": f"jat-{room}", "dimension_id": "dimension",
            "encryption_domain_id": DOMAIN, "room_id": room,
            "created_at": "2026-11-18T12:00:00Z", "capture_policy_sha256": "b" * 64,
            "capture_mode": "source-only",
            "workspace": {"engine": "restic", "repository_id": REPOSITORY,
                          "repository_format": 2, "snapshot_id": str(index) * 64,
                          "tree_id": str(index + 2) * 64, "source_path": ".",
                          "logical_bytes": 1, "data_added": 1, "data_added_packed": 1},
            "components": {"rcc_environment": None, "homebrew_recovery": None, "hauler_content": None},
            "source": {}, "producer": {"josh_room_version": "0.1.0", "restic_version": "0.19.1",
                                       "source_platform": "linux-x64", "restore_platforms": ["linux-x64"]}})
        digest = hashlib.sha256(room.encode()).hexdigest()
        catalog = catalog.add_logical_snapshot(room, "Synthetic Room", descriptor,
                                              ObjectRef(f"objects/sha256/{digest}", digest, 50), "c" * 64)
        descriptors[(room, f"jat-{room}")] = descriptor
        inventory.append(SnapshotInventoryItem(str(index) * 64, str(index + 2) * 64, "2026-11-18T12:00:00Z", None))
        pins[room] = {"room_id": room, "logical_jat_id": f"jat-{room}", "descriptor_sha256": digest}
    plan = {"source_path_sha256": "d" * 64, "capture_policy_sha256": "e" * 64,
            "parent_room_id": "parent", "parent_logical_jat_id": "jat-parent",
            "expected_parent_logical_jat_id": None, "capture_mode": "source-only",
            "children": [{"path": "projects/-child folder", "room_id": "child", "logical_jat_id": "jat-child"}]}
    scope = {"provider": "minio", "connection_id": "connection", "dimension_id": "dimension",
             "encryption_domain_id": DOMAIN, "repository_id": REPOSITORY, "catalog_key": "catalog.jroom.age"}
    state = {"format_version": 4, "contract": "composition-safety-v1", "scope": scope,
             "revision": 5, "dimension_catalog": catalog.body,
             "operations": {"operation": {"plan": plan, "plan_sha256": plan_digest(plan),
                           "phase": "committed", "child_pins": {plan['children'][0]['path']: pins['child']},
                           "parent_pin": pins['parent'], "manifest_sha256": "f" * 64,
                           "parent_policy_sha256": "b" * 64}}}
    return state, descriptors, inventory


class CompositionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.body, self.descriptors, self.inventory = fixture()

    def report(self, body=None, descriptors=None, inventory=None, **kwargs):
        body = self.body if body is None else body
        return reconcile(CompositionSafetyState.from_dict(body),
                         self.descriptors if descriptors is None else descriptors,
                         RepositoryInfo(REPOSITORY, 2), self.inventory if inventory is None else inventory,
                         expected_scope=kwargs.get('scope', body['scope']))

    def test_exact_historical_pin_not_latest_and_no_mutation(self):
        before = copy.deepcopy(self.body)
        self.body['dimension_catalog']['projects']['child']['latest'] = 'jat-child'
        first = self.report()
        self.assertTrue(first['ok'])
        self.assertFalse(first['can_apply'])
        self.assertFalse(first['can_resume'])
        self.assertFalse(first['writes_performed'])
        self.assertEqual(first, self.report())
        self.assertEqual(before, self.body)

    def test_old_catalog_decoder_refuses_v4_without_changing_v3(self):
        inner = copy.deepcopy(self.body['dimension_catalog'])
        self.assertEqual(Catalog.from_body(inner).body, inner)
        with self.assertRaises(ValueError):
            Catalog.from_body(self.body)

    def test_source_maps_are_not_retained(self):
        state = CompositionSafetyState.from_dict(self.body)
        self.body['operations'].clear()
        result = state.to_dict()
        result['operations'].clear()
        self.assertEqual(len(state.to_dict()['operations']), 1)

    def test_admission_guard_refuses_every_metadata_type_and_ignored_rules(self):
        from josh_room.composition import require_uncomposed_workspace
        with tempfile.TemporaryDirectory(prefix='composition-admission-') as directory:
            root = Path(directory)
            require_uncomposed_workspace(root)
            (root / '.josh-roomignore').write_text('**/.josh-room-composition.json\n')
            for name in ['.josh-room-composition.json', '.josh-room-split.json']:
                marker = root / name
                for kind in ['file', 'directory', 'dangling-link']:
                    if kind == 'file':
                        marker.write_text('synthetic-sensitive-sentinel')
                    elif kind == 'directory':
                        marker.mkdir()
                    else:
                        marker.symlink_to(root / 'absent')
                    with self.subTest(name=name, kind=kind), self.assertRaisesRegex(ValueError, '^composed Save is blocked'):
                        require_uncomposed_workspace(root)
                    if kind == 'directory':
                        marker.rmdir()
                    else:
                        marker.unlink()

    def test_local_noop_receipt_is_not_consulted_for_unadmitted_composition(self):
        from unittest.mock import patch

        from josh_room.local_save_receipt import read_noop
        with tempfile.TemporaryDirectory(prefix='composition-noop-') as directory:
            root = Path(directory)
            (root / '.josh-room-split.json').write_text('{}')
            with patch('josh_room.local_save_receipt.receipt_path', side_effect=AssertionError('cached receipt consulted')):
                self.assertIsNone(read_noop(root, root, None, 'parent'))

    def test_native_scanner_and_direct_save_refuse_before_provider_work(self):
        from unittest.mock import patch

        try:
            from josh_room import room_store_bridge
            from josh_room.room_store_operations import (
                RoomStoreOperationsError,
                _scan_workspace,
            )
        except ModuleNotFoundError as error:
            self.skipTest(f'native runtime dependency unavailable: {error.name}')
        from josh_room.workspace_policy import load_capture_policy
        with tempfile.TemporaryDirectory(prefix='composition-native-') as directory:
            root = Path(directory)
            (root / '.josh-room-composition.json').write_text('{}')
            (root / '.josh-roomignore').write_text('.josh-room-composition.json\n')
            with self.assertRaisesRegex(RoomStoreOperationsError, '^composed Save is blocked'):
                _scan_workspace(root, load_capture_policy(root))
            with patch.object(room_store_bridge, '_resolve_material', side_effect=AssertionError('provider admission attempted')):
                with self.assertRaisesRegex(ValueError, '^composed Save is blocked'):
                    room_store_bridge.save_room_store(root, None, 'parent', root, None, components={})

    def test_actual_cli_reconcile_remains_offline_and_reports_only_counts(self):
        from unittest.mock import patch

        try:
            from josh_room import cli
        except ModuleNotFoundError as error:
            self.skipTest(f'CLI runtime dependency unavailable: {error.name}')
        state, descriptors, inventory = fixture()
        bundle = {'state': state, 'descriptors': [d.to_dict() for d in descriptors.values()],
                  'repository': {'repository_id': REPOSITORY, 'repository_format': 2},
                  'inventory': [dict(snapshot_id=i.snapshot_id, tree_id=i.tree_id, time=i.time,
                                     parent_snapshot_id=None) for i in inventory], 'expected_scope': state['scope']}
        with tempfile.TemporaryDirectory(prefix='composition-cli-') as directory:
            path = Path(directory) / 'evidence.json'
            path.write_text(json.dumps(bundle))
            stdout = io.StringIO()
            with patch.object(cli, 'initialize_system_trust', side_effect=AssertionError('trust initialized')):
                with patch.object(cli, '_instance_root', side_effect=AssertionError('runtime configured')):
                    with patch.object(cli, '_write_runtime_result'), redirect_stdout(stdout):
                        status = cli.main(['composition', 'reconcile', '--evidence-file', str(path), '--json'])
            result = json.loads(stdout.getvalue())
            self.assertEqual(status, 0)
            self.assertEqual(result['evidence_trust'], 'offline-untrusted-input')
            self.assertFalse(result['can_resume'])
            self.assertEqual([p.name for p in Path(directory).iterdir()], ['evidence.json'])

    def test_historical_pin_remains_required_after_newer_child_save(self):
        body = self.body['dimension_catalog']
        latest = copy.deepcopy(body['projects']['child']['snapshots']['jat-child'])
        latest['snapshot_id'] = 'jat-newer'
        latest['workspace_snapshot_id'] = '6' * 64
        latest['tree_id'] = '7' * 64
        body['projects']['child']['snapshots']['jat-newer'] = latest
        body['projects']['child']['latest'] = 'jat-newer'
        new = self.descriptors[('child', 'jat-child')].to_dict()
        new['logical_jat_id'] = 'jat-newer'
        new['workspace'].update(snapshot_id='6' * 64, tree_id='7' * 64)
        self.descriptors[('child', 'jat-newer')] = LogicalJat.from_dict(new)
        self.inventory.append(SnapshotInventoryItem('6' * 64, '7' * 64, '', None))
        self.assertTrue(self.report()['ok'])
        self.assertFalse(self.report(inventory=self.inventory[1:])['ok'])

    def test_missing_component_snapshot_is_not_hidden_by_workspace_availability(self):
        descriptor = self.descriptors[('child', 'jat-child')].to_dict()
        descriptor['components']['rcc_environment'] = {
            'kind': 'rcca', 'snapshot': {'repository_id': REPOSITORY, 'repository_format': 2,
                                       'snapshot_id': '8' * 64, 'tree_id': '9' * 64},
            'archive_sha256': 'b' * 64, 'archive_size': 123, 'member_basename': 'rcc-environment.rcca',
            'artifact_digest': 'sha256:' + 'c' * 64, 'specification_digest': 'sha256:' + 'd' * 64,
            'platform': 'linux_amd64', 'rcc_version': 'v18.19.5', 'robot_relative_path': 'robot.yaml'}
        self.descriptors[('child', 'jat-child')] = LogicalJat.from_dict(descriptor)
        record = self.body['dimension_catalog']['projects']['child']['snapshots']['jat-child']
        record['component_refs']['rcc_environment'] = {'snapshot_id': '8' * 64, 'tree_id': '9' * 64}
        self.assertFalse(self.report()['ok'])
        self.inventory.append(SnapshotInventoryItem('8' * 64, '9' * 64, '', None))
        self.assertTrue(self.report()['ok'])

    def test_wrong_scope_or_repository_refused(self):
        for field, value in [('dimension_id', 'other'), ('repository_id', '9' * 64),
                             ('connection_id', 'other'), ('catalog_key', 'alternate.age')]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.report(scope={**self.body['scope'], field: value})

    def test_invalid_exact_pins_refused(self):
        path = self.body['operations']['operation']['plan']['children'][0]['path']
        for field, value in [('logical_jat_id', 'latest'), ('room_id', 'other'), ('descriptor_sha256', '0' * 64)]:
            bad = copy.deepcopy(self.body)
            bad['operations']['operation']['child_pins'][path][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.report(bad)

    def test_missing_pin_or_descriptor_refused(self):
        bad = copy.deepcopy(self.body)
        del bad['dimension_catalog']['projects']['child']
        with self.assertRaises(ValueError):
            self.report(bad)
        with self.assertRaises(ValueError):
            self.report(descriptors={})

    def test_missing_restic_data_is_failure_not_success(self):
        result = self.report(inventory=self.inventory[1:])
        self.assertFalse(result['ok'])
        self.assertEqual(result['missing_snapshot_count'], 1)
        self.assertFalse(result['can_resume'])

    def test_parent_git_mode_and_policy_digest_must_match(self):
        for mode in ['full-repository', None]:
            descriptor = self.descriptors[('parent', 'jat-parent')].to_dict()
            if mode is None:
                descriptor.pop('capture_mode')
            else:
                descriptor['capture_mode'] = mode
            descriptors = {**self.descriptors, ('parent', 'jat-parent'): LogicalJat.from_dict(descriptor)}
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'parent capture contract'):
                self.report(descriptors=descriptors)
        self.body['operations']['operation']['parent_policy_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'parent capture contract'):
            self.report()

    def test_wrong_tree_and_descriptor_refused(self):
        with self.assertRaises(ValueError):
            self.report(inventory=[SnapshotInventoryItem('1' * 64, '0' * 64, '', None)])
        descriptors = dict(self.descriptors)
        body = descriptors[('child', 'jat-child')].to_dict()
        body['workspace']['tree_id'] = '0' * 64
        descriptors[('child', 'jat-child')] = LogicalJat.from_dict(body)
        with self.assertRaises(ValueError):
            self.report(descriptors=descriptors)

    def test_unknown_fields_versions_and_missing_domain_refused(self):
        for change in ['unknown', 'version', 'domain', 'phase']:
            bad = copy.deepcopy(self.body)
            if change == 'unknown':
                bad['unknown'] = 'synthetic-sensitive-sentinel'
            elif change == 'version':
                bad['format_version'] = 5
            elif change == 'domain':
                del bad['dimension_catalog']['encryption_domain_id']
            else:
                bad['operations']['operation']['phase'] = 'repair'
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.report(bad)

    def test_unproven_response_cannot_jump_to_committed(self):
        bad = copy.deepcopy(self.body)
        bad['operations']['operation']['parent_pin'] = None
        with self.assertRaises(ValueError):
            self.report(bad)
        for phase in ['reserved', 'children-recorded', 'parent-prepared']:
            state = copy.deepcopy(bad)
            op = state['operations']['operation']
            op['phase'] = phase
            if phase != 'parent-prepared':
                op['manifest_sha256'] = op['parent_policy_sha256'] = None
            result = self.report(state)
            self.assertFalse(result['can_resume'])
            self.assertEqual(result['operations'][0]['phase'], phase)

    def test_conflicting_operation_and_plan_change_refused(self):
        bad = copy.deepcopy(self.body)
        bad['operations']['operation']['plan']['children'][0]['path'] = 'changed'
        with self.assertRaises(ValueError):
            self.report(bad)
        bad = copy.deepcopy(self.body)
        bad['operations']['other'] = copy.deepcopy(bad['operations']['operation'])
        with self.assertRaises(ValueError):
            self.report(bad)

    def test_offline_file_duplicate_keys_links_and_content_redaction(self):
        state, descriptors, inventory = fixture()
        bundle = {'state': state, 'descriptors': [d.to_dict() for d in descriptors.values()],
                  'repository': {'repository_id': REPOSITORY, 'repository_format': 2},
                  'inventory': [dict(snapshot_id=i.snapshot_id, tree_id=i.tree_id, time=i.time,
                                     parent_snapshot_id=None) for i in inventory], 'expected_scope': state['scope']}
        with tempfile.TemporaryDirectory(prefix='composition-evidence-') as directory:
            path = Path(directory) / 'evidence.json'
            path.write_text(json.dumps(bundle))
            result = inspect_evidence_file(path)
            self.assertEqual(result['evidence_trust'], 'offline-untrusted-input')
            self.assertNotIn('projects/-child folder', json.dumps(result))
            path.write_text('{"state":0,"state":"synthetic-sensitive-sentinel"}')
            with self.assertRaisesRegex(ValueError, '^invalid composition reconciliation evidence$'):
                inspect_evidence_file(path)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                inspect_evidence_file(link)


if __name__ == '__main__':
    unittest.main()
