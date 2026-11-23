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

    def test_v3_child_capture_policy_checked_against_descriptor(self):
        self.body.update(contract="composition-safety-v3", child_updates={})
        operation = self.body["operations"]["operation"]
        plan = operation["plan"]
        plan["plan_version"] = 3
        slot = plan["children"][0]
        slot.update(capture_mode="source-only", capture_policy_sha256="b" * 64,
                    approved_paths_sha256="a" * 64, source_records_sha256="e" * 64)
        operation["plan_sha256"] = plan_digest(plan)
        self.assertTrue(self.report()["ok"])
        slot["capture_mode"] = "full-repository"
        operation["plan_sha256"] = plan_digest(plan)
        with self.assertRaisesRegex(ValueError, "capture contract"):
            self.report()
        self.body["contract"] = "composition-safety-v2"
        with self.assertRaises(ValueError):
            CompositionSafetyState.from_dict(self.body)

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

    def test_candidate_schema_and_decoder_cover_closed_contract(self):
        try:
            from jsonschema import Draft202012Validator, ValidationError
            from referencing import Registry, Resource
        except ModuleNotFoundError:
            self.skipTest('schema validation dependency unavailable')
        directory = Path(__file__).resolve().parents[1] / 'schemas'
        catalog_schema = json.loads((directory / 'dimension-catalog-v3.schema.json').read_text())
        schema = json.loads((directory / 'composition-safety-v1.schema.json').read_text())
        registry = Registry().with_resource(catalog_schema['$id'], Resource.from_contents(catalog_schema))
        validator = Draft202012Validator(schema, registry=registry)
        validator.validate(self.body)
        CompositionSafetyState.from_dict(self.body)
        for change in ['origin', 'version', 'unknown']:
            invalid = copy.deepcopy(self.body)
            if change == 'origin':
                invalid['dimension_catalog']['projects']['child']['snapshots']['jat-child']['origin_room_id'] = None
            elif change == 'version':
                invalid['format_version'] = 5
            else:
                invalid['unknown'] = True
            with self.subTest(change=change), self.assertRaises(ValidationError):
                validator.validate(invalid)
            with self.subTest(change=change), self.assertRaises(ValueError):
                CompositionSafetyState.from_dict(invalid)

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
                RoomStoreOperations,
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
            # Restore/export share this scanner: historical files remain valid.
            scan = _scan_workspace(root, load_capture_policy(root))
            self.assertNotIn('.josh-room-composition.json', scan.paths)
            (root / '.josh-roomignore').unlink()
            self.assertIn('.josh-room-composition.json', _scan_workspace(root, load_capture_policy(root)).paths)
            operations = object.__new__(RoomStoreOperations)
            operations.workspace = root
            for operation in [operations.preview, operations.save]:
                with self.assertRaisesRegex(RoomStoreOperationsError, '^composed Save is blocked'):
                    operation()
            with (
                patch.object(room_store_bridge, '_resolve_material', side_effect=AssertionError('provider admission attempted')),
                self.assertRaisesRegex(ValueError, '^composed Save is blocked'),
            ):
                room_store_bridge.save_room_store(root, None, 'parent', root, None, components={})
            with (
                patch.object(room_store_bridge, '_resolve_material', side_effect=AssertionError('provider admission attempted')),
                self.assertRaisesRegex(ValueError, '^composed Save is blocked'),
            ):
                room_store_bridge.preview_room_store(root, None, 'parent', root, None, components={})
            from josh_room.operations import create_snapshot
            with (
                patch('josh_room.operations.run_build', side_effect=AssertionError('portable build attempted')),
                self.assertRaisesRegex(ValueError, '^composed Save is blocked'),
            ):
                create_snapshot(root / 'not-created', 'parent', root, root, [])
            self.assertFalse((root / 'not-created').exists())

    def test_headless_create_guard_precedes_receipts_and_provider_setup(self):
        from unittest.mock import patch

        try:
            from josh_room import cli
        except ModuleNotFoundError as error:
            self.skipTest(f'CLI runtime dependency unavailable: {error.name}')
        with tempfile.TemporaryDirectory(prefix='composition-create-') as directory:
            root = Path(directory)
            (root / '.josh-room-split.json').write_text('{}')
            for backend in ['local', 'minio', 's3']:
                stdout = io.StringIO()
                with (
                    patch.object(cli, 'initialize_system_trust'),
                    patch.object(cli, '_write_runtime_result'),
                    patch.object(cli, '_instance_root', side_effect=AssertionError('runtime setup attempted')),
                    redirect_stdout(stdout),
                ):
                    status = cli.main(['snapshot', 'create', 'parent', '--backend', backend,
                                       '--source', str(root), '--json'])
                self.assertNotEqual(status, 0)
                self.assertFalse(json.loads(stdout.getvalue())['ok'])

    def test_headless_preview_guard_precedes_runtime_and_provider_setup(self):
        from unittest.mock import patch

        from josh_room import cli
        with tempfile.TemporaryDirectory(prefix='composition-preview-') as directory:
            root = Path(directory)
            (root / '.josh-room-split.json').write_text('{}')
            stdout = io.StringIO()
            with (
                patch.object(cli, 'initialize_system_trust'),
                patch.object(cli, '_write_runtime_result'),
                patch.object(cli, '_instance_root', side_effect=AssertionError('runtime setup attempted')),
                redirect_stdout(stdout),
            ):
                status = cli.main(['snapshot', 'preview', 'parent', '--source', str(root), '--json'])
            self.assertNotEqual(status, 0)
            self.assertFalse(json.loads(stdout.getvalue())['ok'])

    def test_legacy_snapshot_identity_must_match_its_catalog_slot(self):
        legacy = {'snapshot_id': 'different-id', 'object_key': 'objects/sha256/' + '0' * 64,
                  'ciphertext_sha256': '0' * 64, 'ciphertext_size': 1,
                  'created_at': '2026-11-18T12:00:00Z', 'workspace_fingerprint': 'b' * 64}
        self.body['dimension_catalog']['projects']['legacy'] = {
            'display_name': 'Synthetic Legacy', 'latest': 'legacy-jat', 'snapshots': {'legacy-jat': legacy}}
        with self.assertRaises(ValueError):
            CompositionSafetyState.from_dict(self.body)

    def test_missing_parent_baseline_has_a_bounded_validation_error(self):
        del self.body['dimension_catalog']['projects']['parent']
        operation = self.body['operations']['operation']
        operation['plan']['expected_parent_logical_jat_id'] = 'missing-parent-pin'
        operation['plan_sha256'] = plan_digest(operation['plan'])
        with self.assertRaisesRegex(ValueError, '^composition parent baseline is missing$'):
            CompositionSafetyState.from_dict(self.body)

    def test_historical_pins_allow_one_coherent_active_continuation(self):
        historical = copy.deepcopy(self.body['operations']['operation'])
        pending = copy.deepcopy(historical)
        pending['plan']['parent_logical_jat_id'] = 'jat-parent-next'
        pending['plan']['expected_parent_logical_jat_id'] = 'jat-parent'
        pending['plan']['children'][0]['logical_jat_id'] = 'jat-child-next'
        pending['plan_sha256'] = plan_digest(pending['plan'])
        pending.update(phase='reserved', child_pins={}, parent_pin=None,
                       manifest_sha256=None, parent_policy_sha256=None)
        self.body['operations']['next-operation'] = pending
        self.assertTrue(self.report()['ok'])
        self.assertEqual(self.body['operations']['operation'], historical)
        self.body['operations'] = dict(reversed(list(self.body['operations'].items())))
        self.assertTrue(self.report()['ok'])
        missing = copy.deepcopy(self.body)
        del missing['dimension_catalog']['projects']['child']
        with self.assertRaises(ValueError):
            self.report(missing)
        self.body['operations']['overlapping-operation'] = copy.deepcopy(pending)
        self.body['operations']['overlapping-operation']['plan']['parent_logical_jat_id'] = 'jat-parent-conflict'
        self.body['operations']['overlapping-operation']['plan_sha256'] = plan_digest(
            self.body['operations']['overlapping-operation']['plan'])
        with self.assertRaisesRegex(ValueError, 'overlapping incomplete'):
            self.report()
        del self.body['operations']['overlapping-operation']
        pending['plan']['expected_parent_logical_jat_id'] = None
        pending['plan_sha256'] = plan_digest(pending['plan'])
        with self.assertRaisesRegex(ValueError, 'baseline differs from current parent'):
            self.report()
        pending['plan']['expected_parent_logical_jat_id'] = 'jat-parent'
        pending['plan']['children'][0]['room_id'] = 'parent'
        pending['plan_sha256'] = plan_digest(pending['plan'])
        with self.assertRaises(ValueError):
            self.report()

    def test_active_parent_baseline_must_be_current_but_history_can_be_older(self):
        original = self.body['dimension_catalog']['projects']['parent']['snapshots']['jat-parent']
        newer = {**original, 'snapshot_id': 'jat-parent-newer'}
        self.body['dimension_catalog']['projects']['parent']['snapshots']['jat-parent-newer'] = newer
        self.body['dimension_catalog']['projects']['parent']['latest'] = 'jat-parent-newer'
        # Historical pin decoding does not require that historical parent to be latest.
        CompositionSafetyState.from_dict(self.body)
        pending = copy.deepcopy(self.body['operations']['operation'])
        pending['plan'].update(parent_logical_jat_id='jat-parent-next', expected_parent_logical_jat_id='jat-parent')
        pending['plan']['children'][0]['logical_jat_id'] = 'jat-child-next'
        pending['plan_sha256'] = plan_digest(pending['plan'])
        pending.update(phase='reserved', child_pins={}, parent_pin=None,
                       manifest_sha256=None, parent_policy_sha256=None)
        self.body['operations']['next-operation'] = pending
        with self.assertRaisesRegex(ValueError, 'baseline differs from current parent'):
            CompositionSafetyState.from_dict(self.body)

    def test_truncated_siblings_are_not_selectable_even_if_alias_was_omitted(self):
        from unittest.mock import patch

        from josh_room import composition
        with tempfile.TemporaryDirectory(prefix='composition-alias-') as directory:
            root = Path(directory)
            (root / 'Tools').mkdir()
            (root / 'tools').mkdir()
            with patch.dict(composition.LIMITS, {'entries_per_directory': 1}):
                result = composition.discover(root)
                self.assertIn('entry-cap', result['coverage']['omissions'])
                self.assertTrue(result['candidates'])
                self.assertTrue(all(not row['selectable'] for row in result['candidates']))

    def test_candidate_boundary_closes_legacy_records_without_changing_v3(self):
        legacy = {'snapshot_id': 'legacy-jat', 'object_key': 'objects/sha256/' + 'a' * 64,
                  'ciphertext_sha256': 'a' * 64, 'ciphertext_size': 1,
                  'created_at': '2026-11-18T12:00:00Z', 'workspace_fingerprint': 'b' * 64}
        self.body['dimension_catalog']['projects']['legacy'] = {
            'display_name': 'Synthetic Legacy', 'latest': 'legacy-jat', 'snapshots': {'legacy-jat': legacy}}
        CompositionSafetyState.from_dict(self.body)
        legacy['unexpected'] = 'synthetic-sensitive-sentinel'
        Catalog.from_body(self.body['dimension_catalog'])  # preserve v3 behavior
        with self.assertRaisesRegex(ValueError, 'legacy composition evidence'):
            CompositionSafetyState.from_dict(self.body)

    def test_actual_cli_reconcile_remains_offline_and_reports_only_counts(self):
        from unittest.mock import patch

        try:
            from josh_room import cli
        except ModuleNotFoundError as error:
            self.skipTest(f'CLI runtime dependency unavailable: {error.name}')
        state, descriptors, inventory = fixture()
        bundle = {'state': state, 'descriptors': [d.to_dict() for d in descriptors.values()],
                  'repository': {'repository_id': REPOSITORY, 'repository_format': 2},
                  'inventory': [{'snapshot_id': i.snapshot_id, 'tree_id': i.tree_id, 'time': i.time,
                                 'parent_snapshot_id': None} for i in inventory], 'expected_scope': state['scope']}
        with tempfile.TemporaryDirectory(prefix='composition-cli-') as directory:
            path = Path(directory) / 'evidence.json'
            path.write_text(json.dumps(bundle))
            stdout = io.StringIO()
            with (
                patch.object(cli, 'initialize_system_trust', side_effect=AssertionError('trust initialized')),
                patch.object(cli, '_instance_root', side_effect=AssertionError('runtime configured')),
                patch.object(cli, '_write_runtime_result'),
                redirect_stdout(stdout),
            ):
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
        digest = hashlib.sha256(b'synthetic newer child').hexdigest()
        latest.update(object_key=f'objects/sha256/{digest}', ciphertext_sha256=digest)
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
            del state['dimension_catalog']['projects']['parent']
            descriptors = {key: value for key, value in self.descriptors.items() if key[0] != 'parent'}
            result = self.report(state, descriptors=descriptors)
            self.assertFalse(result['can_resume'])
            self.assertEqual(result['operations'][0]['phase'], phase)

    def test_precommit_phases_refuse_already_published_parent_output(self):
        for phase in ['reserved', 'children-recorded', 'parent-prepared']:
            bad = copy.deepcopy(self.body)
            op = bad['operations']['operation']
            op['phase'] = phase
            op['parent_pin'] = None
            if phase != 'parent-prepared':
                op['manifest_sha256'] = op['parent_policy_sha256'] = None
            with self.subTest(phase=phase), self.assertRaises(ValueError):
                CompositionSafetyState.from_dict(bad)

    def test_parent_output_cannot_equal_prior_baseline(self):
        op = self.body['operations']['operation']
        op['plan']['expected_parent_logical_jat_id'] = op['plan']['parent_logical_jat_id']
        op['plan_sha256'] = plan_digest(op['plan'])
        with self.assertRaises(ValueError):
            CompositionSafetyState.from_dict(self.body)

    def test_present_legacy_origin_must_be_schema_identifier(self):
        record = {'snapshot_id': 'legacy-jat', 'object_key': 'objects/sha256/' + 'a' * 64,
                  'ciphertext_sha256': 'a' * 64, 'ciphertext_size': 1,
                  'created_at': '2026-11-18T12:00:00Z', 'workspace_fingerprint': 'b' * 64,
                  'origin_project_id': None}
        self.body['dimension_catalog']['projects']['legacy'] = {
            'display_name': 'Synthetic Legacy', 'latest': 'legacy-jat', 'snapshots': {'legacy-jat': record}}
        Catalog.from_body(self.body['dimension_catalog'])
        with self.assertRaises(ValueError):
            CompositionSafetyState.from_dict(self.body)
        record['origin_project_id'] = 'origin-room'
        CompositionSafetyState.from_dict(self.body)

    def test_present_logical_origin_must_be_schema_identifier(self):
        record = self.body['dimension_catalog']['projects']['child']['snapshots']['jat-child']
        record['origin_room_id'] = None
        Catalog.from_body(self.body['dimension_catalog'])
        with self.assertRaises(ValueError):
            CompositionSafetyState.from_dict(self.body)

    def test_invalid_timezone_offset_is_not_normalized(self):
        record = {'snapshot_id': 'legacy-jat', 'object_key': 'objects/sha256/' + 'a' * 64,
                  'ciphertext_sha256': 'a' * 64, 'ciphertext_size': 1,
                  'created_at': '2026-11-18T12:00:00+00:60', 'workspace_fingerprint': 'b' * 64}
        self.body['dimension_catalog']['projects']['legacy'] = {
            'display_name': 'Synthetic Legacy', 'latest': 'legacy-jat', 'snapshots': {'legacy-jat': record}}
        with self.assertRaises(ValueError):
            CompositionSafetyState.from_dict(self.body)

    def test_logical_timezone_offset_is_not_normalized(self):
        invalid = '2026-11-18T12:00:00+00:60'
        self.body['dimension_catalog']['projects']['parent']['snapshots']['jat-parent']['created_at'] = invalid
        descriptor = self.descriptors[('parent', 'jat-parent')].to_dict()
        descriptor['created_at'] = invalid
        self.descriptors[('parent', 'jat-parent')] = LogicalJat.from_dict(descriptor)
        with self.assertRaises(ValueError):
            self.report()

    def test_evidence_parent_traversal_is_not_normalized_to_another_tree(self):
        state, descriptors, inventory = fixture()
        bundle = {'state': state, 'descriptors': [d.to_dict() for d in descriptors.values()],
                  'repository': {'repository_id': REPOSITORY, 'repository_format': 2},
                  'inventory': [{
                      'snapshot_id': i.snapshot_id, 'tree_id': i.tree_id, 'time': i.time,
                      'parent_snapshot_id': i.parent_snapshot_id} for i in inventory],
                  'expected_scope': state['scope']}
        with tempfile.TemporaryDirectory(prefix='composition-parent-') as directory:
            path = Path(directory) / 'evidence.json'
            path.write_text(json.dumps(bundle))
            self.assertTrue(inspect_evidence_file(path)['ok'])
            with self.assertRaises(ValueError):
                inspect_evidence_file(Path(directory) / 'missing' / '..' / path.name)

    def test_one_ciphertext_cannot_claim_different_descriptors_or_sizes(self):
        for change in ['descriptor', 'size', 'legacy-size', 'legacy-kind']:
            body = copy.deepcopy(self.body)
            record = body['dimension_catalog']['projects']['child']['snapshots']['jat-child']
            if change in ['descriptor', 'size']:
                parent = body['dimension_catalog']['projects']['parent']['snapshots']['jat-parent']
                parent.update(object_key=record['object_key'], ciphertext_sha256=record['ciphertext_sha256'])
                body['operations']['operation']['parent_pin']['descriptor_sha256'] = record['ciphertext_sha256']
                if change == 'size':
                    parent['ciphertext_size'] += 1
            else:
                legacy = {field: record[field] for field in [
                    'object_key', 'ciphertext_sha256', 'ciphertext_size', 'created_at']}
                legacy.update(snapshot_id='legacy-jat', workspace_fingerprint='b' * 64)
                if change == 'legacy-size':
                    legacy.update(object_key='objects/sha256/' + '0' * 64, ciphertext_sha256='0' * 64)
                    other = {**legacy, 'snapshot_id': 'legacy-copy', 'ciphertext_size': legacy['ciphertext_size'] + 1}
                    body['dimension_catalog']['projects']['legacy-copy'] = {
                        'display_name': 'Synthetic Copy', 'latest': 'legacy-copy',
                        'snapshots': {'legacy-copy': other}}
                body['dimension_catalog']['projects']['legacy'] = {
                    'display_name': 'Synthetic Legacy', 'latest': 'legacy-jat',
                    'snapshots': {'legacy-jat': legacy}}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.report(body)

    def test_consistent_shared_legacy_objects_and_restic_snapshots_are_allowed(self):
        legacy = {'snapshot_id': 'legacy-jat', 'object_key': 'objects/sha256/' + '0' * 64,
                  'ciphertext_sha256': '0' * 64, 'ciphertext_size': 1,
                  'created_at': '2026-11-18T12:00:00Z', 'workspace_fingerprint': 'b' * 64}
        for room in ['legacy-a', 'legacy-b']:
            self.body['dimension_catalog']['projects'][room] = {
                'display_name': 'Synthetic Legacy', 'latest': 'legacy-jat',
                'snapshots': {'legacy-jat': copy.deepcopy(legacy)}}
        child = self.descriptors[('child', 'jat-child')].to_dict()
        child['workspace'].update(snapshot_id='2' * 64, tree_id='4' * 64)
        self.descriptors[('child', 'jat-child')] = LogicalJat.from_dict(child)
        record = self.body['dimension_catalog']['projects']['child']['snapshots']['jat-child']
        record.update(workspace_snapshot_id='2' * 64, tree_id='4' * 64)
        self.assertTrue(self.report(inventory=self.inventory[1:])['ok'])

    def test_committed_parent_explicit_lineage_must_match_baseline(self):
        for baseline in [None, 'jat-before']:
            body, descriptors, inventory = fixture()
            operation = body['operations']['operation']
            operation['plan']['expected_parent_logical_jat_id'] = baseline
            operation['plan_sha256'] = plan_digest(operation['plan'])
            if baseline is not None:
                prior = descriptors[('parent', 'jat-parent')].to_dict()
                prior['logical_jat_id'] = baseline
                prior['workspace'].update(snapshot_id='4' * 64, tree_id='5' * 64)
                prior = LogicalJat.from_dict(prior)
                digest = hashlib.sha256(b'synthetic prior parent').hexdigest()
                catalog = Catalog.from_body(body['dimension_catalog']).add_logical_snapshot(
                    'parent', 'Synthetic Room', prior,
                    ObjectRef(f'objects/sha256/{digest}', digest, 50), 'c' * 64)
                body['dimension_catalog'] = catalog.body
                descriptors[('parent', baseline)] = prior
                inventory.append(SnapshotInventoryItem('4' * 64, '5' * 64, '', None))
            # Save may omit lineage when Restic reports no parent, even with a baseline.
            self.assertTrue(self.report(body, descriptors, inventory)['ok'])
            parent = descriptors[('parent', 'jat-parent')].to_dict()
            if baseline is not None:
                parent['parent_logical_jat_id'] = baseline
                parent['workspace']['parent_snapshot_id'] = '4' * 64
                descriptors[('parent', 'jat-parent')] = LogicalJat.from_dict(parent)
                self.assertTrue(self.report(body, descriptors, inventory)['ok'])
            for change in ['logical', 'workspace', 'missing-logical', 'missing-workspace']:
                invalid = copy.deepcopy(parent)
                invalid['parent_logical_jat_id'] = baseline or 'unplanned-parent'
                invalid['workspace']['parent_snapshot_id'] = '4' * 64
                if change == 'logical':
                    invalid['parent_logical_jat_id'] = 'unplanned-parent'
                elif change == 'workspace':
                    invalid['workspace']['parent_snapshot_id'] = '9' * 64
                elif change == 'missing-logical':
                    invalid.pop('parent_logical_jat_id')
                else:
                    invalid['workspace'].pop('parent_snapshot_id')
                changed = {**descriptors, ('parent', 'jat-parent'): LogicalJat.from_dict(invalid)}
                with self.subTest(baseline=baseline, change=change), self.assertRaises(ValueError):
                    self.report(body, changed, inventory)

    def test_empty_evidence_arrays_are_valid_but_other_iterables_are_not(self):
        body = copy.deepcopy(self.body)
        body['dimension_catalog']['projects'].clear()
        op = body['operations']['operation']
        op.update(phase='reserved', child_pins={}, parent_pin=None,
                  manifest_sha256=None, parent_policy_sha256=None)
        bundle = {'state': body, 'descriptors': [], 'inventory': [], 'expected_scope': body['scope'],
                  'repository': {'repository_id': REPOSITORY, 'repository_format': 2}}
        with tempfile.TemporaryDirectory(prefix='composition-array-') as directory:
            path = Path(directory) / 'evidence.json'
            path.write_text(json.dumps(bundle))
            self.assertTrue(inspect_evidence_file(path)['ok'])
            for field in ['descriptors', 'inventory']:
                for value in ['', {}]:
                    invalid = copy.deepcopy(bundle)
                    invalid[field] = value
                    path.write_text(json.dumps(invalid))
                    with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                        inspect_evidence_file(path)

    def test_reserved_child_record_cannot_exist_without_its_atomic_pin(self):
        body = copy.deepcopy(self.body)
        del body['dimension_catalog']['projects']['parent']
        operation = body['operations']['operation']
        operation.update(phase='reserved', parent_pin=None, manifest_sha256=None,
                         parent_policy_sha256=None)
        # A committed child plus its pin is a valid partial reservation.
        CompositionSafetyState.from_dict(body)
        operation['child_pins'].clear()
        with self.assertRaises(ValueError):
            CompositionSafetyState.from_dict(body)
        # A genuinely unfilled slot has neither record nor pin.
        del body['dimension_catalog']['projects']['child']
        CompositionSafetyState.from_dict(body)

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
                  'inventory': [{'snapshot_id': i.snapshot_id, 'tree_id': i.tree_id, 'time': i.time,
                                 'parent_snapshot_id': None} for i in inventory], 'expected_scope': state['scope']}
        with tempfile.TemporaryDirectory(prefix='composition-evidence-') as directory:
            path = Path(directory) / 'evidence.json'
            path.write_text(json.dumps(bundle))
            result = inspect_evidence_file(path)
            self.assertEqual(result['evidence_trust'], 'offline-untrusted-input')
            self.assertNotIn('projects/-child folder', json.dumps(result))
            for field, value in [('time', {}), ('time', '2026-11-18'), ('parent_snapshot_id', 5)]:
                invalid = copy.deepcopy(bundle)
                invalid['inventory'][0][field] = value
                path.write_text(json.dumps(invalid))
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, '^invalid composition reconciliation evidence$'):
                    inspect_evidence_file(path)
            path.write_text('{"state":0,"state":"synthetic-sensitive-sentinel"}')
            with self.assertRaisesRegex(ValueError, '^invalid composition reconciliation evidence$'):
                inspect_evidence_file(path)
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                inspect_evidence_file(link)


if __name__ == '__main__':
    unittest.main()
