"""Append-only whole-child updates and explicit immutable parent pin refresh.

No deletion, unload, implicit latest selection or legacy repository mutation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from .catalog import Catalog, corroborate_logical_snapshot
from .composition import (
    MANIFEST,
    parse_manifest,
    portable_path,
    validate_child_git,
    validate_manifest,
    validate_payload_path,
)
from .composition_child_admission import admit_catalog_child
from .composition_component_contract import verify_component_snapshots
from .composition_git_integrity import verify_staged_git
from .composition_lifecycle import (
    CONTENT_DIGEST_ALGORITHM,
    INHERITED_POLICY,
    CompositionAdmissionError,
    _admitted_source,
    _capture,
    _catalog_root_scan,
    _commit,
    _content_digest,
    _copy_paths,
    _finish_view,
    _records,
    _slot,
    _verify_descriptor_contract,
)
from .composition_safety import (
    _CONTRACTS,
    MAX_BYTES,
    CompositionSafetyState,
    _identifier,
    plan_digest,
)
from .composition_save_intent import (
    INTENT_FORMAT,
    IntentWriter,
    projected_capacity,
    require_writer,
)
from .composition_transaction import (
    canonical,
    manifest_for,
    prepare_parent,
    record_parent,
)
from .progress import report_progress
from .room_store_operations import _validate_snapshot_entries, scan_workspace_for_status
from .workspace_onboarding import _directory
from .workspace_policy import load_capture_policy
from .workspace_state import context_status, write_stat_workspace_marker


def _open(authority, namespace):
    from .composition_retirement import require_active
    if not sys.platform.startswith('linux') or namespace != authority.namespace:
        raise ValueError('namespace updates require the selected Linux authority')
    envelope, etag = authority.read()
    if envelope is None or envelope['state'] is None:
        raise ValueError('namespace is unavailable')
    require_active(envelope)
    return envelope, etag, CompositionSafetyState.from_envelope(envelope)


def _descriptor(authority, state, room, snapshot):
    from .composition_retirement import require_active
    envelope, _ = authority.read()
    require_active(envelope, room, snapshot)
    _identifier(room)
    _identifier(snapshot)
    record = Catalog.from_body(state.to_dict()['dimension_catalog']).resolve_snapshot(room, snapshot)
    descriptor = authority.descriptor(record)
    corroborate_logical_snapshot(room, record, descriptor)
    value = descriptor.to_dict()
    if value['workspace']['repository_id'] != state.to_dict()['scope']['repository_id']:
        raise ValueError('descriptor repository differs from namespace')
    _verify_descriptor_contract(state, descriptor)
    return record, descriptor


@contextmanager
def open_catalog_snapshot(dimension, namespace, room, snapshot, authority, store_factory, private):
    """Resolve a committed Catalog recovery point in its own encrypted repository."""
    _identifier(snapshot)
    envelope, _, state = _open(authority, namespace)
    body = state.to_dict()
    pins = [pin for op in body['operations'].values() if op['phase'] == 'committed'
            for pin in (op['parent_pin'], *op['child_pins'].values())]
    pins.extend(op['pin'] for op in body.get('child_updates', {}).values() if op['phase'] == 'committed')
    record, descriptor = _descriptor(authority, state, room, snapshot)
    if not any((pin['room_id'], pin['logical_jat_id'], pin['descriptor_sha256'])
               == (room, snapshot, record['ciphertext_sha256']) for pin in pins):
        raise ValueError('selected Catalog recovery point is not committed')
    with store_factory(envelope['secret']) as store:
        repository = store.open_existing()
        if repository.repository_id != envelope['repository_id']:
            raise ValueError('Catalog snapshot repository differs')
        yield SimpleNamespace(project_id=room, catalog=Catalog.from_body(body['dimension_catalog']),
            store=store, private_dir=private, material=authority.material, dimension=dimension,
            repository_info=repository, selected_record=record, selected_descriptor=descriptor)


def catalog_workspace_status(workspace):
    """Compare the current local parent and its bound children without contacting storage."""
    from .workspace_state import local_status

    parent = context_status(workspace)
    if not parent.get('composition_namespace'):
        return local_status(workspace)
    result = local_status(workspace)
    if not parent.get('path_matches'):
        return result
    fd = _directory(workspace)
    try:
        try:
            manifest = parse_manifest(_read_regular(fd, MANIFEST, MAX_BYTES))
        except (OSError, ValueError):
            return result
    finally:
        os.close(fd)
    children = []
    for row in manifest['children']:
        child = workspace / row['path']
        try:
            fd = _directory(child)
        except (OSError, ValueError):
            continue
        os.close(fd)
        current = context_status(child)
        if (current.get('path_matches')
                and current.get('composition_namespace') == parent['composition_namespace']
                and current.get('dimension_id') == parent.get('dimension_id')
                and current.get('encryption_domain_id') == parent.get('encryption_domain_id')
                and current.get('project_id') == row['room_id']
                and current.get('snapshot_id') == row['snapshot_id']):
            children.append({'path': row['path'], 'room_id': row['room_id'],
                             'snapshot_id': row['snapshot_id'], 'state': local_status(child)['state']})
    return {**result, 'catalog_children': children}


def _proof(store, descriptor, private):
    value = descriptor.to_dict()['workspace']
    if store.snapshot(value['snapshot_id']).tree_id != value['tree_id']:
        raise ValueError('snapshot tree differs from descriptor')
    entries = _validate_snapshot_entries(store.entries(value['snapshot_id']))
    target = private / ('update-proof-' + uuid.uuid4().hex)
    store.restore(value['snapshot_id'], target)
    policy = load_capture_policy(target)
    if policy.capture_mode != descriptor.to_dict()['capture_mode'] or policy.sha256 != descriptor.to_dict()['capture_policy_sha256']:
        raise ValueError('saved capture policy differs')
    scan = scan_workspace_for_status(target)
    if set(entries) - {'.'} != scan.paths:
        raise ValueError('saved inventory differs from effective policy')
    validate_child_git(target, scan.paths, policy.capture_mode)
    verify_staged_git(target, scan.paths, policy.capture_mode, private)
    verify_component_snapshots(store, descriptor, private)
    return target


def _repository(store, envelope):
    if store.open_existing().repository_id != envelope['repository_id']:
        raise ValueError('namespace repository differs')


def _claim(authority, envelope, etag, slot):
    key = slot['logical_jat_id']
    if key in envelope['capture_claims']:
        raise ValueError('capture claimed without a completed snapshot; reconcile stopped writer')
    updated = copy.deepcopy(envelope)
    updated['capture_claims'][key] = uuid.uuid4().hex
    return updated, authority.put(updated, etag)


def _read_regular(fd, name, limit=65536):
    opened = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        if not stat.S_ISREG(os.fstat(opened).st_mode):
            raise ValueError('namespace metadata must be a regular file')
        raw = os.read(opened, limit + 1)
        if len(raw) > limit:
            raise ValueError('namespace metadata exceeds limit')
        return raw
    finally:
        os.close(opened)


def _baseline_policy(workspace, expected, *, directory_fd=None):
    fd = _directory(workspace) if directory_fd is None else os.dup(directory_fd)
    try:
        policy = load_capture_policy(workspace, directory_fd=fd)
        if policy.sha256 != expected:
            raise ValueError('child policy differs from authenticated baseline; policy reconciliation is unsupported')
        return policy
    finally:
        os.close(fd)


def _child_view(workspace, private, namespace, room, baseline, accepted, expected_policy, expected_mode):
    """Stage only admitted records through one held source directory descriptor."""
    workspace = Path(os.path.abspath(workspace))
    fd = _directory(workspace)
    try:
        marker = context_status(workspace)
        if (not marker['ok'] or marker.get('composition_namespace') != namespace
                or marker.get('project_id') != room or marker.get('snapshot_id') not in accepted):
            raise ValueError('child marker does not match selected exact baseline')
        guard = json.loads(_read_regular(fd, '.josh-room-split.json'))
        if set(guard) != {'namespace', 'room_id', 'snapshot_id'} or guard['namespace'] != namespace or guard['room_id'] != room or guard['snapshot_id'] not in accepted:
            raise ValueError('child namespace guard differs')
        policy = _baseline_policy(workspace, expected_policy, directory_fd=fd)
        if policy.capture_mode != expected_mode or not policy.is_excluded('.josh-room-split.json'):
            raise ValueError('child policy must preserve the authenticated capture mode and namespace fence')
        scan = scan_workspace_for_status(workspace)
        validate_child_git(workspace, scan.paths, policy.capture_mode)
        for path in scan.paths:
            if path != '.josh-roomignore':
                validate_payload_path(path, allow_nested_policy=True)
        records = _records(workspace, scan.paths, fd)
        # Marker/guard replacement changes the root's mtime but no payload.
        evidence_records = {**records, '.': records['.'][:3]}
        view = private / ('child-update-view-' + uuid.uuid4().hex)
        view.mkdir(mode=0o700)
        ignore, modes = _copy_paths(workspace, view, scan.paths, source_fd=fd, records=records)
        (view / '.josh-roomignore').write_text(ignore)
        _finish_view(view, set(scan.paths), modes)
        after = scan_workspace_for_status(workspace)
        if scan.signature != after.signature or policy.sha256 != after.capture_policy_sha256:
            raise ValueError('child changed during staging')
        return view, {
            'room_id': room, 'expected_logical_jat_id': baseline,
            'source_path_sha256': hashlib.sha256(str(workspace).encode()).hexdigest(),
            'source_identity': [os.fstat(fd).st_dev, os.fstat(fd).st_ino],
            'source_records_sha256': plan_digest(evidence_records),
            'source_signature': scan.signature, 'capture_policy_sha256': policy.sha256,
            'capture_mode': policy.capture_mode, 'content_sha256': _content_digest(view),
            'content_digest_algorithm': CONTENT_DIGEST_ALGORITHM,
        }
    finally:
        os.close(fd)


def _child_origin(state, room):
    return any(slot['room_id'] == room for op in state.to_dict()['operations'].values()
               if op['phase'] == 'committed' for slot in op['plan']['children'])


def _recheck_source(workspace, fd, plan):
    policy = _baseline_policy(workspace, plan['capture_policy_sha256'], directory_fd=fd)
    now = scan_workspace_for_status(workspace)
    records = _records(workspace, now.paths, fd)
    if (policy.capture_mode != plan['capture_mode'] or now.signature != plan['source_signature']
            or now.capture_policy_sha256 != policy.sha256
            or plan_digest({**records, '.': records['.'][:3]}) != plan['source_records_sha256']):
        raise ValueError('child source changed after approval')
    return now


def _bind_child(workspace, dimension, namespace, room, output, plan, authority):
    # Recheck live evidence before rebinding local metadata. A process loss
    # between guard and marker is resumable only by this committed operation.
    with _admitted_source(Path(workspace), plan) as fd:
        accepted = {plan['expected_logical_jat_id'], output}
        marker = context_status(workspace)
        guard = json.loads(_read_regular(fd, '.josh-room-split.json'))
        if (not marker['ok'] or marker.get('composition_namespace') != namespace
                or marker.get('project_id') != room or marker.get('snapshot_id') not in accepted
                or marker.get('dimension_id') != dimension.dimension_id
                or marker.get('encryption_domain_id') != authority.binding['encryption_domain_id']
                or set(guard) != {'namespace', 'room_id', 'snapshot_id'}
                or guard['namespace'] != namespace or guard['room_id'] != room or guard['snapshot_id'] not in accepted):
            raise ValueError('local namespace metadata changed before rebinding')
        now = _recheck_source(workspace, fd, plan)
        temp = '.josh-room-update-' + uuid.uuid4().hex
        opened = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
        try:
            with os.fdopen(opened, 'wb') as stream:
                stream.write(canonical({'namespace': namespace, 'room_id': room, 'snapshot_id': output}))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, '.josh-room-split.json', src_dir_fd=fd, dst_dir_fd=fd)
            write_stat_workspace_marker(Path(f'/proc/{os.getpid()}/fd/{fd}'), dimension_id=dimension.dimension_id,
                encryption_domain_id=authority.binding['encryption_domain_id'], project_id=room, snapshot_id=output,
                display_name='Child Room', workspace_signature=now.signature, signature_algorithm=now.signature_algorithm,
                capture_policy_sha256=now.capture_policy_sha256, path_binding=Path(workspace), composition_namespace=namespace)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass


def _binding_failure(namespace, room, snapshot, operation_id, new_snapshot):
    return {'ok': False, 'status': 'saved-local-binding-unverified', 'namespace': namespace,
            'room_id': room, 'snapshot_id': snapshot, 'operation_id': operation_id,
            'snapshot_verified': True, 'marker_verified': False, 'new_snapshot_committed': new_snapshot,
            'parent_refreshed': False,
            'error': f'Child recovery point {snapshot} is saved; local binding is unverified. Preserve edits and reconcile operation {operation_id}.'}


def save_child(workspace, dimension, namespace, room, baseline, operation_id,
               authority, store_factory, private, *, approval=None, component_options=None):
    """Plan or apply an independent child Save; never refresh a parent implicitly."""
    _identifier(operation_id)
    envelope, etag, state = _open(authority, namespace)
    from .composition_retirement import require_active
    require_active(envelope, room, baseline)
    if approval is not None:
        require_writer(envelope, getattr(authority, "operation_id", None))
    if not _child_origin(state, room):
        raise ValueError('Room is not a child in this namespace')
    old = state.to_dict().get('child_updates', {}).get(operation_id)
    if old and old['plan'].get('content_digest_algorithm') != CONTENT_DIGEST_ALGORITHM:
        raise ValueError('legacy child update digest requires reconciliation; immutable history was not changed')
    output = 'jat-' + _slot(namespace, 'child-update:' + operation_id)
    if old and (old['plan']['room_id'], old['plan']['expected_logical_jat_id'], old['plan']['logical_jat_id']) != (room, baseline, output):
        raise ValueError('operation ID already binds another child update')
    accepted = {baseline, output} if old and old['phase'] == 'committed' else {baseline}
    from .composition_retirement import active_inventory
    latest = active_inventory(envelope)['rooms'].get(room, {}).get('latest')
    if latest not in accepted:
        raise ValueError('child latest advanced; select its exact baseline')
    record, previous = _descriptor(authority, state, room, baseline)
    if previous.to_dict()['source'].get('composition_policy') != INHERITED_POLICY:
        raise ValueError('legacy child baseline lacks verified inherited-rule provenance; safe policy reconciliation is required before any update')
    if Path(workspace).resolve() == private.resolve() or Path(workspace).resolve() in private.resolve().parents:
        raise ValueError('private runtime must be outside child source')
    expected_policy = previous.to_dict()['capture_policy_sha256']
    mode = previous.to_dict()['capture_mode']
    view, plan = _child_view(workspace, private, namespace, room, baseline, accepted, expected_policy, mode)
    if old is None or 'plan_version' in old['plan']:
        plan['plan_version'] = 3
    from .composition_component_contract import artifacts_digest
    from .composition_components import component_plan
    selected = component_plan(workspace, dimension, room, expected_policy, component_options, previous, source=view)
    if selected is not None:
        plan.update(component_plan=selected, plan_version=4)
    _baseline_policy(view, expected_policy)
    marker = context_status(workspace)
    if marker.get('dimension_id') != dimension.dimension_id or marker.get('encryption_domain_id') != authority.binding['encryption_domain_id']:
        raise ValueError('child marker authority differs')
    plan['logical_jat_id'] = output
    request = {'action': 'save-child', 'namespace': namespace, 'binding': authority.binding,
               'operation_id': operation_id, 'plan': plan}
    digest = plan_digest(request)
    if old and canonical(old['plan']) != canonical(plan):
        raise ValueError('child update request changed; preserve reserved history')
    with store_factory(envelope['secret']) as store:
        _repository(store, envelope)
        proof = _proof(store, previous, private)
        prior_components = previous.to_dict()['components']
        component_change = selected is not None and selected['artifacts_sha256'] != artifacts_digest(prior_components)
        changed = component_change or _content_digest(proof) != plan['content_sha256']
        if approval is None:
            return {'ok': True, 'status': 'approval-required', 'approval_sha256': digest,
                    'request': request, 'changed': changed, 'snapshot_id': output if changed else baseline}
        if approval != digest:
            raise ValueError('child Save approval is stale')
        _baseline_policy(workspace, expected_policy)
        if not changed and old is None:
            try:
                _bind_child(workspace, dimension, namespace, room, baseline, plan, authority)
            except (OSError, ValueError):
                return _binding_failure(namespace, room, baseline, operation_id, False)
            return {'ok': True, 'status': 'unchanged', 'snapshot_id': baseline,
                    'namespace': namespace, 'capture_mode': mode, 'components': prior_components, 'catalog_writes': False, 'parent_refreshed': False,
                    'marker_verified': context_status(workspace).get('snapshot_id') == baseline}
        if old is None:
            body = state.to_dict()
            required_contract = 'composition-safety-v4' if selected is not None else 'composition-safety-v3'
            body['contract'] = max(required_contract, body['contract'], key=_CONTRACTS.index)
            body.setdefault('child_updates', {})
            body['child_updates'][operation_id] = {'plan': plan, 'plan_sha256': plan_digest(plan), 'phase': 'reserved', 'pin': None}
            body['revision'] += 1
            state = state.replace(body)
            envelope, etag = _commit(authority, envelope, etag, state)
        if state.to_dict()['child_updates'][operation_id]['phase'] != 'committed':
            def claim(name=None):
                nonlocal envelope, etag
                slot = plan if name is None else {'logical_jat_id': _slot(namespace, output + ':' + name)}
                envelope, etag = _claim(authority, envelope, etag, slot)
            _baseline_policy(workspace, expected_policy)
            _baseline_policy(view, expected_policy)
            descriptor, scan = _capture(view, store, private, plan, dimension,
                authority.binding['encryption_domain_id'], claim=claim, parent=previous, inherited_policy=INHERITED_POLICY,
                **({'component_options': component_options, 'component_workspace': workspace, 'component_claim': claim}
                   if selected is not None else {}))
            value = descriptor.to_dict()
            if (value['capture_mode'] != mode or value['capture_policy_sha256'] != expected_policy
                    or value['source'].get('composition_policy') != INHERITED_POLICY):
                raise ValueError('captured policy differs from authenticated inherited baseline')
            if value['parent_logical_jat_id'] != baseline or value['workspace']['parent_snapshot_id'] != record['workspace_snapshot_id']:
                raise ValueError('child update lineage differs')
            verified = _proof(store, descriptor, private)
            if _content_digest(verified) != plan['content_sha256']:
                raise ValueError('saved child bytes differ from approved inventory')
            _baseline_policy(workspace, expected_policy)
            ref = authority.put_descriptor(descriptor)
            body = state.to_dict()
            catalog = Catalog.from_body(body['dimension_catalog']).add_logical_snapshot(room, 'Child Room', descriptor, ref, scan.signature)
            body['dimension_catalog'] = catalog.body
            body['child_updates'][operation_id].update(phase='committed', pin={'room_id': room, 'logical_jat_id': output, 'descriptor_sha256': ref.sha256})
            body['revision'] += 1
            state = state.replace(body)
            with _admitted_source(Path(workspace), plan) as fd:
                if selected is not None and component_plan(
                    workspace, dimension, room, expected_policy, component_options, previous, source=view
                ) != selected:
                    raise ValueError("child component source changed before publication")
                _recheck_source(workspace, fd, plan)
                envelope, etag = _commit(authority, envelope, etag, state)
        else:
            _, descriptor = _descriptor(authority, state, room, output)
            if descriptor.to_dict().get('parent_logical_jat_id') != baseline or _content_digest(_proof(store, descriptor, private)) != plan['content_sha256']:
                raise ValueError('committed child output differs; preserve history')
        try:
            _bind_child(workspace, dimension, namespace, room, output, plan, authority)
        except (OSError, ValueError):
            return _binding_failure(namespace, room, output, operation_id, True)
        return {'ok': True, 'status': 'saved', 'namespace': namespace, 'room_id': room,
                'snapshot_id': output, 'capture_mode': mode, 'components': descriptor.to_dict()['components'], 'parent_refreshed': False, 'marker_verified': context_status(workspace).get('snapshot_id') == output}


def _verify_parent_workspace(workspace, authority, room, snapshot, operation, *alternate_manifest_digests):
    marker = context_status(workspace)
    if (not marker.get('path_matches') or marker.get('composition_namespace') != authority.namespace
            or marker.get('dimension_id') != authority.binding['dimension_id']
            or marker.get('encryption_domain_id') != authority.binding['encryption_domain_id']
            or marker.get('project_id') != room or marker.get('snapshot_id') != snapshot):
        raise ValueError('open the restored parent at its exact saved location')
    fd = _directory(workspace)
    try:
        raw = _read_regular(fd, MANIFEST, MAX_BYTES)
    finally:
        os.close(fd)
    if hashlib.sha256(raw).hexdigest() not in (operation['manifest_sha256'], *alternate_manifest_digests):
        raise ValueError('local parent manifest differs from its saved recovery point')


def parent_children(workspace, authority, namespace, room, snapshot, *, child_path=None, offset=0, revision=None):
    """Read immutable choices for the exact entered parent from its namespace ledger."""
    envelope, _, state = _open(authority, namespace)
    from .composition_retirement import active_inventory, require_active
    require_active(envelope, room, snapshot)
    active = active_inventory(envelope)['rooms']
    body = state.to_dict()
    picker_revision = body['revision'] + len(envelope.get('retirements', []))
    if ((revision is not None and revision != picker_revision)
            or (offset or child_path is not None) and revision is None):
        raise CompositionAdmissionError('composition-picker-stale', 'Catalog choices changed; reopen the picker')
    committed = [op for op in body['operations'].values() if op['phase'] == 'committed']
    parent = next((op for op in committed if (op['parent_pin']['room_id'],
                   op['parent_pin']['logical_jat_id']) == (room, snapshot)), None)
    if parent is None:
        raise ValueError('parent recovery point is unavailable')
    _verify_parent_workspace(workspace, authority, room, snapshot, parent)
    history = {}
    for op in committed:
        for pin in op['child_pins'].values():
            history.setdefault(pin['room_id'], set()).add(pin['logical_jat_id'])
    for update in body.get('child_updates', {}).values():
        if update['phase'] == 'committed':
            pin = update['pin']
            history.setdefault(pin['room_id'], set()).add(pin['logical_jat_id'])
    history = {child: points.intersection(active.get(child, {}).get('recovery_points', []))
               for child, points in history.items()}
    projects = body['dimension_catalog']['projects']
    result = {'ok': True, 'namespace': namespace, 'parent_room_id': room, 'parent_snapshot_id': snapshot,
              'revision': picker_revision, 'current_parent': active[room]['latest'] == snapshot}
    if child_path is None:
        rows = [{'path': path, 'room_id': pin['room_id'], 'snapshot_id': pin['logical_jat_id'],
                 'other_point_count': len(history[pin['room_id']]) - 1}
                for path, pin in sorted(parent['child_pins'].items())]
        key = 'children'
    else:
        pin = parent['child_pins'].get(child_path)
        if pin is None:
            raise ValueError('selected child path is absent from the saved parent')
        child, current = pin['room_id'], pin['logical_jat_id']
        records = projects[child]['snapshots']
        points = sorted(history[child] - {current}, key=lambda point: (records[point]['created_at'], point), reverse=True)
        rows = [{'snapshot_id': point, 'created_at': records[point]['created_at']} for point in points]
        result.update(child_path=child_path, child_room_id=child, pinned_snapshot_id=current)
        key = 'recovery_points'
    return _picker_page(result, key, rows, offset)


def _picker_page(result, key, rows, offset):
    if type(offset) is not int or offset < 0 or offset >= len(rows) and offset != 0:
        raise ValueError('invalid Catalog picker offset')
    # Leave room under the CLI/extension 64 KiB ceiling for the envelope and
    # escaped Unicode, and under the JSON sanitizer's 128-item array limit.
    page = []
    size = 0
    for row in rows[offset:offset + 64]:
        row_size = len(json.dumps(row, ensure_ascii=True)) + 2
        if size + row_size > 48 * 1024:
            break
        page.append(row)
        size += row_size
    end = offset + len(page)
    return {**result, key: page, 'next_offset': end if end < len(rows) else None}


def _parent_save_view(workspace, private, manifest, policy_sha256, root_files_only, *alternate_manifests):
    fd = _directory(workspace)
    try:
        policy = load_capture_policy(workspace, directory_fd=fd)
        if policy.sha256 != policy_sha256 or any(
            not policy.is_excluded(row['path']) for row in manifest['children']
        ):
            raise ValueError('Catalog capture policy differs from the saved parent')
        if _read_regular(fd, MANIFEST, MAX_BYTES) not in (
            canonical(manifest), *(canonical(value) for value in alternate_manifests),
        ):
            raise ValueError('local Catalog manifest differs from the saved parent')
        scan = _catalog_root_scan(policy, fd, []) if root_files_only else scan_workspace_for_status(workspace)
        records = _records(workspace, scan.paths, fd)
        paths = set(scan.paths) - {MANIFEST, '.josh-roomignore'}
        if root_files_only:
            paths = {path for path in paths if stat.S_ISREG(records[path][2])}
        for path in paths:
            validate_payload_path(path, allow_nested_policy=True)
        view = private / ('parent-save-view-' + uuid.uuid4().hex)
        view.mkdir(mode=0o700)
        ignore, modes = _copy_paths(workspace, view, paths, source_fd=fd, records=records)
        (view / '.josh-roomignore').write_text(ignore)
        (view / MANIFEST).write_bytes(canonical(manifest))
        _finish_view(view, paths | {'.josh-roomignore', MANIFEST}, modes)
        if load_capture_policy(view).sha256 != policy_sha256:
            raise ValueError('staged Catalog capture policy differs')
        if _records(workspace, scan.paths, fd) != records:
            raise ValueError('Catalog source changed during staging')
        return view
    finally:
        os.close(fd)


def _bind_parent_save(workspace, dimension, authority, namespace, room, snapshot, display_name,
                      private, original_manifest, policy_sha256, root_files_only, accepted,
                      previous_op, committed_manifest, content_sha256, replace_manifest=None):
    fd = _directory(workspace)
    try:
        opened = os.fstat(fd)
        scan = scan_workspace_for_status(workspace)
        live = _parent_save_view(workspace, private, original_manifest, policy_sha256, root_files_only,
                                 *((parse_manifest(replace_manifest),) if committed_manifest and replace_manifest else ()))
        after = scan_workspace_for_status(workspace)
        _verify_parent_workspace(workspace, authority, room, accepted, previous_op, *committed_manifest)
        current = workspace.lstat()
        if (scan.signature != after.signature or after.capture_policy_sha256 != policy_sha256
                or _content_digest(live) != content_sha256
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
            raise ValueError('Catalog was saved, but its local files changed before the marker could be updated')
        held = Path(f'/proc/{os.getpid()}/fd/{fd}')
        if replace_manifest and _read_regular(fd, MANIFEST, MAX_BYTES) != replace_manifest:
            with tempfile.NamedTemporaryFile(prefix='.catalog-manifest-', dir=held, delete=False) as handle:
                temporary = Path(handle.name)
                try:
                    handle.write(replace_manifest)
                    handle.flush()
                    os.fsync(handle.fileno())
                    os.replace(temporary, held / MANIFEST)
                finally:
                    temporary.unlink(missing_ok=True)
            after = scan_workspace_for_status(workspace)
        write_stat_workspace_marker(held, dimension_id=dimension.dimension_id,
            encryption_domain_id=authority.binding['encryption_domain_id'], project_id=room, snapshot_id=snapshot,
            display_name=display_name,
            workspace_signature=after.signature, signature_algorithm=after.signature_algorithm,
            capture_policy_sha256=after.capture_policy_sha256, path_binding=workspace, composition_namespace=namespace)
    finally:
        os.close(fd)


def refresh_parent(dimension, namespace, room, baseline, operation_id, pins,
                   authority, store_factory, private, *, approval=None, workspace=None, save_root=False,
                   expected_content=None, before_publish=None):
    """Publish a new immutable parent from saved bytes and explicitly selected pins."""
    _identifier(operation_id)
    envelope, etag, state = _open(authority, namespace)
    from .composition_retirement import require_active
    require_active(envelope, room, baseline)
    if approval is not None:
        require_writer(envelope, getattr(authority, "operation_id", None))
    body = state.to_dict()
    previous_op = next((op for op in body['operations'].values() if op['phase'] == 'committed'
        and op['parent_pin']['room_id'] == room and op['parent_pin']['logical_jat_id'] == baseline), None)
    if previous_op is None:
        raise ValueError('parent baseline is not committed')
    old = body['operations'].get(operation_id)
    committed_manifest = (old['manifest_sha256'],) if save_root and old and old['phase'] == 'committed' else ()
    action = 'save-catalog-root' if save_root else 'refresh-parent'
    output = 'jat-' + _slot(namespace, ('parent-save:' if save_root else 'parent-refresh:') + operation_id)
    if old and (old['plan']['expected_parent_logical_jat_id'], old['plan']['parent_logical_jat_id']) != (baseline, output):
        raise ValueError('operation ID already binds another parent refresh')
    if workspace is not None:
        marker_snapshot = context_status(workspace).get('snapshot_id')
        accepted = output if save_root and old and old['phase'] == 'committed' and marker_snapshot == output else baseline
        _verify_parent_workspace(workspace, authority, room, accepted, previous_op, *committed_manifest)
    from .composition_retirement import active_inventory
    latest = active_inventory(envelope)['rooms'].get(room, {}).get('latest')
    if latest != baseline and not (old and old['phase'] == 'committed' and latest == output):
        raise ValueError('parent latest advanced; select its exact baseline')
    _, previous = _descriptor(authority, state, room, baseline)
    if not isinstance(pins, dict) or (save_root and workspace is None) or (not save_root and not pins):
        raise ValueError('select at least one exact child pin')
    with store_factory(envelope['secret']) as store:
        _repository(store, envelope)
        view = _proof(store, previous, private)
        raw = (view / MANIFEST).read_bytes()
        if hashlib.sha256(raw).hexdigest() != previous_op['manifest_sha256']:
            raise ValueError('saved parent manifest differs from committed history')
        manifest = parse_manifest(raw)
        original_manifest = manifest_for(state, next(k for k, v in body['operations'].items() if v == previous_op), previous_op['plan_sha256'])
        if canonical(manifest) != canonical(original_manifest):
            raise ValueError('saved parent pins differ from committed history')
        rows = {row['path']: row for row in manifest['children']}
        if not pins.keys() <= rows.keys():
            raise ValueError('refresh cannot add or remap child paths')
        for path, snapshot in sorted(pins.items()):
            portable_path(path)
            _, child = _descriptor(authority, state, rows[path]['room_id'], snapshot)
            # Also prove availability of the exact selected child, before parent publication.
            _proof(store, child, private)
            rows[path]['snapshot_id'] = snapshot
        manifest = validate_manifest(manifest)
        changed = canonical(manifest) != raw
        request = {'action': action, 'namespace': namespace, 'binding': authority.binding,
                   'operation_id': operation_id, 'room_id': room, 'baseline': baseline, 'pins': pins}
        if save_root:
            root_files_only = envelope['request'].get('root_files_only', False)
            policy_sha256 = previous.to_dict()['capture_policy_sha256']
            workspace = Path(workspace).resolve(strict=True)
            saved_content = _content_digest(view)
            view = _parent_save_view(workspace, private, original_manifest, policy_sha256, root_files_only,
                                     *((manifest,) if committed_manifest else ()))
            request['content_sha256'] = _content_digest(view)
            if expected_content is not None and request['content_sha256'] != expected_content:
                raise ValueError('Catalog root files changed after approval')
            request['workspace_sha256'] = hashlib.sha256(os.fsencode(workspace)).hexdigest()
            changed = changed or request['content_sha256'] != saved_content
        digest = plan_digest(request)
        if approval is None:
            return {'ok': True, 'status': 'approval-required', 'request': request, 'approval_sha256': digest,
                    'changed': changed, 'snapshot_id': output if changed else baseline}
        if approval != digest:
            raise ValueError('parent refresh approval differs')
        display_name = body['dimension_catalog']['projects'][room]['display_name']
        if not changed and old is None:
            if save_root:
                _bind_parent_save(workspace, dimension, authority, namespace, room, baseline, display_name,
                                  private, original_manifest, policy_sha256, root_files_only, accepted,
                                  previous_op, committed_manifest, request['content_sha256'])
            return {'ok': True, 'status': 'unchanged', 'snapshot_id': baseline, 'catalog_writes': False,
                    **({'marker_verified': True} if save_root else {})}
        (view / MANIFEST).write_bytes(canonical(manifest))
        scan = scan_workspace_for_status(view)
        plan = {'source_path_sha256': plan_digest(request), 'capture_policy_sha256': scan.capture_policy_sha256,
                'parent_room_id': room, 'parent_logical_jat_id': output, 'expected_parent_logical_jat_id': baseline,
                'capture_mode': previous.to_dict()['capture_mode'], 'children': [{'path': row['path'], 'room_id': row['room_id'],
                    'logical_jat_id': row['snapshot_id']} for row in manifest['children']]}
        if old and canonical(old['plan']) != canonical(plan):
            raise ValueError('reserved parent refresh pins differ')
        if old is None:
            candidate = copy.deepcopy(body)
            candidate['operations'][operation_id] = {'plan': plan, 'plan_sha256': plan_digest(plan), 'phase': 'reserved',
                'child_pins': {path: {'room_id': row['room_id'], 'logical_jat_id': row['snapshot_id'],
                    'descriptor_sha256': body['dimension_catalog']['projects'][row['room_id']]['snapshots'][row['snapshot_id']]['ciphertext_sha256']}
                    for path, row in rows.items()}, 'parent_pin': None, 'manifest_sha256': None, 'parent_policy_sha256': None}
            candidate['revision'] += 1
            state = state.replace(candidate)
            envelope, etag = _commit(authority, envelope, etag, state)
        op = state.to_dict()['operations'][operation_id]
        if op['phase'] == 'reserved':
            candidate = state.to_dict()
            candidate['operations'][operation_id]['phase'] = 'children-recorded'
            candidate['revision'] += 1
            state = state.replace(candidate)
            envelope, etag = _commit(authority, envelope, etag, state)
        state = prepare_parent(state, operation_id, plan_digest(plan), policy_sha256=scan.capture_policy_sha256)
        if canonical(state.to_dict()) != canonical(envelope['state']):
            envelope, etag = _commit(authority, envelope, etag, state)
        if state.to_dict()['operations'][operation_id]['phase'] != 'committed':
            slot = {'room_id': room, 'logical_jat_id': output}
            def claim():
                nonlocal envelope, etag
                envelope, etag = _claim(authority, envelope, etag, slot)
            descriptor, saved_scan = _capture(view, store, private, slot, dimension,
                authority.binding['encryption_domain_id'], claim=claim, parent=previous)
            restored = _proof(store, descriptor, private)
            if (restored / MANIFEST).read_bytes() != canonical(manifest) or _content_digest(restored) != _content_digest(view):
                raise ValueError('refreshed parent payload differs')
            ref = authority.put_descriptor(descriptor)
            state = record_parent(state, operation_id, plan_digest(plan), descriptor=descriptor,
                object_ref=ref, workspace_signature=saved_scan.signature,
                display_name=state.to_dict()['dimension_catalog']['projects'][room]['display_name'])
            if before_publish is not None:
                before_publish()
            envelope, etag = _commit(authority, envelope, etag, state)
        else:
            _, committed = _descriptor(authority, state, room, output)
            if _content_digest(_proof(store, committed, private)) != _content_digest(view):
                raise ValueError('committed parent output differs; preserve history')
        if save_root:
            _bind_parent_save(workspace, dimension, authority, namespace, room, output, display_name,
                              private, original_manifest, policy_sha256, root_files_only, accepted,
                              previous_op, committed_manifest, request['content_sha256'],
                              canonical(manifest) if canonical(manifest) != raw else None)
        return {'ok': True, 'status': 'saved', 'namespace': namespace, 'room_id': room,
                'snapshot_id': output, 'workspace_modified': save_root, 'previous_parent_snapshot_id': baseline,
                **({'marker_verified': context_status(workspace).get('snapshot_id') == output} if save_root else {})}


def _admit_aggregate_children(workspace, manifest, envelope, operation_id):
    body = envelope["state"]
    children, missing = [], []
    namespace = envelope["namespace"]
    for row in manifest["children"]:
        child_operation = _slot(namespace, operation_id + ":child:" + row["path"])
        pending = body.get("child_updates", {}).get(child_operation)
        split = None
        if pending and pending["phase"] == "committed":
            plan = pending["plan"]
            split = (plan["expected_logical_jat_id"], plan["logical_jat_id"])
        try:
            marker = admit_catalog_child(workspace, row, envelope["binding"], namespace, split)
        except ValueError:
            raise CompositionAdmissionError(
                "composition-child-occupied",
                "A Catalog child folder is occupied, unreadable or has a mismatched Room binding.",
                child=row["path"],
            ) from None
        if marker is None:
            missing.append(row["path"])
        else:
            child_baseline = pending["plan"]["expected_logical_jat_id"] if pending else marker["snapshot_id"]
            children.append((row, child_operation, child_baseline))
    return children, missing


def _pending_save_status(envelope):
    intent = envelope.get("save_intent")
    if intent is None:
        return {"ok": True, "status": "no-pending-save"}
    state = envelope["state"]
    return {
        "ok": True, "status": "pending-save", "intent": intent,
        "parent_committed": state["operations"].get(intent["operation_id"], {}).get("phase") == "committed",
        "children": [
            {"path": row["path"], "snapshot_id": row["approved_output"],
             "status": state.get("child_updates", {}).get(row["operation_id"], {}).get("phase",
                        "pending" if row["writes_snapshot"] else "retained")}
            for row in intent["children"]
        ],
    }


def inspect_catalog_save(authority, namespace):
    envelope, _, _ = _open(authority, namespace)
    return _pending_save_status(envelope)


def _start_save(authority, envelope, etag, intent, child_plans, parent_plan):
    previous = envelope.get("save_intent")
    if previous is not None:
        if previous != intent:
            raise ValueError("Catalog Save retry differs from its pending intent")
        return IntentWriter(authority, intent["operation_id"])
    state = envelope["state"]
    if (any(op["phase"] != "committed" for op in state["operations"].values())
            or any(op["phase"] != "committed" for op in state.get("child_updates", {}).values())):
        raise ValueError("reconcile the pending namespace operation before saving the Catalog")
    from .composition_retirement import completed_capture_claims

    if set(envelope["capture_claims"]) - completed_capture_claims(envelope):
        raise ValueError("reconcile the incomplete capture claim before saving the Catalog")
    projected_capacity(envelope, intent, child_plans, parent_plan)
    candidate = {**envelope, "format": INTENT_FORMAT, "save_intent": intent,
                 "retirements": envelope.get("retirements", [])}
    try:
        authority.put(candidate, etag, intent_id=intent["operation_id"])
    except ValueError:
        observed, _ = authority.read()
        if observed is None or observed.get("save_intent") != intent:
            raise ValueError("Catalog Save authority changed before exclusive admission; review again") from None
    return IntentWriter(authority, intent["operation_id"])


def save_catalog_workspace(workspace, dimension, namespace, room, baseline, operation_id,
                           authority, store_factory, private, *, approval=None):
    """Review, reserve capacity, and publish one parent with exact child pins."""
    _identifier(operation_id)
    workspace = Path(workspace)
    envelope, etag, state = _open(authority, namespace)
    require_writer(envelope, operation_id)
    body = state.to_dict()
    origin = next(((key, op) for key, op in body['operations'].items() if op['phase'] == 'committed'
                   and (op['parent_pin']['room_id'], op['parent_pin']['logical_jat_id']) == (room, baseline)), None)
    if origin is None:
        raise ValueError('Catalog baseline is not committed')
    key, original_operation = origin
    manifest = manifest_for(state, key, original_operation['plan_sha256'])
    candidates, missing = _admit_aggregate_children(workspace, manifest, envelope, operation_id)
    previous = {row['path']: row['snapshot_id'] for row in manifest['children']}
    committed = body['operations'].get(operation_id)
    old_pins = {path: pin['logical_jat_id'] for path, pin in committed['child_pins'].items()
                if previous.get(path) != pin['logical_jat_id']} if committed else {}
    parent_args = dimension, namespace, room, baseline, operation_id
    runtime = authority, store_factory, private
    root_options = {'workspace': workspace, 'save_root': True}
    root_preview = refresh_parent(*parent_args, old_pins, *runtime, **root_options)
    content = root_preview['request']['content_sha256']
    workspace_digest = root_preview['request']['workspace_sha256']
    root_approval = plan_digest({'content_sha256': content, 'workspace_sha256': workspace_digest, 'baseline': baseline})
    reviews = []
    for row, child_operation, child_baseline in candidates:
        args = workspace / row['path'], dimension, namespace, row['room_id'], child_baseline, child_operation
        preview = save_child(*args, *runtime)
        reviews.append((row, args, preview))
    pins = {row['path']: preview['snapshot_id'] for row, _, preview in reviews
            if preview['snapshot_id'] != previous[row['path']]}
    request = {'action': 'save-catalog', 'binding': authority.binding, 'namespace': namespace,
               'operation_id': operation_id, 'room_id': room, 'baseline': baseline,
               'root_approval': root_approval, 'retained_missing_children': missing,
               'children': [{'path': row['path'], 'approval_sha256': preview['approval_sha256'],
                             'snapshot_id': preview['snapshot_id']} for row, _, preview in reviews]}
    digest = plan_digest(request)
    changed = root_preview['changed'] or bool(pins)
    output = 'jat-' + _slot(namespace, 'parent-save:' + operation_id) if changed else baseline
    if approval is None:
        return {'ok': True, 'status': 'approval-required', 'approval_sha256': digest,
                'changed': changed, 'snapshot_id': output, 'children_to_update': list(pins),
                'retained_missing_children': missing}
    if approval != digest:
        raise ValueError('Catalog Save approval is stale')
    remote_committed = committed is not None and committed["phase"] == "committed"
    writer = None
    if changed and (not remote_committed or envelope.get("save_intent") is not None):
        intent = {
            "operation_id": operation_id, "room_id": room, "baseline": baseline, "approval_sha256": digest,
            "root_approval": root_approval, "parent_output": output, "retained_missing_children": missing,
            "children": [{"path": row["path"], "room_id": row["room_id"], "operation_id": args[-1],
                           "approved_output": preview["snapshot_id"], "approval_sha256": preview["approval_sha256"],
                           "writes_snapshot": preview["changed"]} for row, args, preview in reviews],
        }
        parent_plan = {
            "source_path_sha256": "f" * 64, "capture_policy_sha256": "f" * 64,
            "parent_room_id": room, "parent_logical_jat_id": output, "expected_parent_logical_jat_id": baseline,
            "capture_mode": original_operation["plan"]["capture_mode"],
            "children": [{"path": row["path"], "room_id": row["room_id"],
                          "logical_jat_id": pins.get(row["path"], row["snapshot_id"])} for row in manifest["children"]],
        }
        writer = _start_save(authority, envelope, etag, intent,
                             {args[-1]: preview["request"]["plan"] for _, args, preview in reviews}, parent_plan)
        runtime = writer, store_factory, private
    def check_occupancy():
        current, _, _ = _open(authority, namespace)
        present, still_missing = _admit_aggregate_children(workspace, manifest, current, operation_id)
        if still_missing != missing or [row["path"] for row, _, _ in present] != [row["path"] for row, _, _ in reviews]:
            raise ValueError("Catalog child occupancy changed after approval")
        for row, args, preview in reviews:
            if context_status(args[0]).get("snapshot_id") != preview["snapshot_id"]:
                raise ValueError("Catalog child marker changed after approval")

    ordered = [(row, args, preview) for row, args, preview in reviews if row['path'] in pins]
    try:
        for index, (_, args, preview) in enumerate(ordered):
            report_progress('catalog-save', 'Saving child Room', current=index, total=len(ordered))
            if preview['changed']:
                saved = save_child(*args, *runtime, approval=preview['approval_sha256'])
                if not saved.get('ok') or not saved.get('marker_verified') or saved['snapshot_id'] != preview['snapshot_id']:
                    raise ValueError('one child recovery point requires reconciliation before publishing the Catalog')
        # A byte-identical rewrite can make the stat marker stale. Rebind it
        # against the already verified descriptor without publishing a point.
        for row, args, preview in reviews:
            if preview["changed"]:
                continue
            marker = context_status(args[0])
            plan = preview["request"]["plan"]
            if marker.get("workspace_signature") != plan["source_signature"]:
                saved = save_child(*args, *runtime, approval=preview["approval_sha256"])
                if not saved.get("ok") or not saved.get("marker_verified"):
                    raise ValueError("unchanged child marker could not be rebound")
        if ordered:
            report_progress('catalog-save', 'Saving Catalog parent', current=len(ordered), total=len(ordered))
        check_occupancy()
        preview = refresh_parent(*parent_args, pins, *runtime, expected_content=content, **root_options)
        result = refresh_parent(*parent_args, pins, *runtime, approval=preview['approval_sha256'],
                                expected_content=content, before_publish=check_occupancy, **root_options)
        if writer is not None and result.get("ok") and result.get("marker_verified"):
            terminal, terminal_etag = authority.read()
            writer.put({**terminal, "save_intent": None}, terminal_etag)
        return {**result, "retained_missing_children": missing}
    except (ValueError, OSError, RuntimeError) as error:
        if not changed:
            raise
        latest, _ = authority.read()
        pending = _pending_save_status(latest)
        if pending.get("parent_committed") and writer is not None:
            terminal, terminal_etag = authority.read()
            if terminal.get("save_intent") == pending["intent"]:
                writer.put({**terminal, "save_intent": None}, terminal_etag)
            pending["status"] = "saved-local-binding-unverified"
        reported = CompositionAdmissionError("composition-save-pending",
            "Catalog Save stopped. Inspect the operation and resume the same operation ID.")
        reported.result.update(pending)
        reported.result.pop("ok", None)
        raise reported from error
