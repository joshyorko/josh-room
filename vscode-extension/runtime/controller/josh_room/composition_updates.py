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
import uuid
from pathlib import Path

from .catalog import Catalog, corroborate_logical_snapshot
from .composition import MANIFEST, parse_manifest, portable_path, validate_manifest
from .composition_lifecycle import (
    CONTENT_DIGEST_ALGORITHM,
    _admitted_source,
    _capture,
    _commit,
    _content_digest,
    _copy_paths,
    _finish_view,
    _records,
    _slot,
)
from .composition_safety import CompositionSafetyState, _identifier, plan_digest
from .composition_transaction import (
    canonical,
    manifest_for,
    prepare_parent,
    record_parent,
)
from .room_store_operations import _validate_snapshot_entries, scan_workspace_for_status
from .workspace_onboarding import _directory
from .workspace_policy import load_capture_policy
from .workspace_state import context_status, write_stat_workspace_marker


def _open(authority, namespace):
    if not sys.platform.startswith('linux') or namespace != authority.namespace:
        raise ValueError('namespace updates require the selected Linux authority')
    envelope, etag = authority.read()
    if envelope is None or envelope['state'] is None:
        raise ValueError('namespace is unavailable')
    return envelope, etag, CompositionSafetyState.from_dict(envelope['state'])


def _v2(state):
    body = state.to_dict()
    body['contract'] = 'composition-safety-v2'
    body.setdefault('child_updates', {})
    return body


def _descriptor(authority, state, room, snapshot):
    _identifier(room)
    _identifier(snapshot)
    record = Catalog.from_body(state.to_dict()['dimension_catalog']).resolve_snapshot(room, snapshot)
    descriptor = authority.descriptor(record)
    corroborate_logical_snapshot(room, record, descriptor)
    value = descriptor.to_dict()
    if value['workspace']['repository_id'] != state.to_dict()['scope']['repository_id']:
        raise ValueError('descriptor repository differs from namespace')
    if value.get('capture_mode') != 'source-only' or any(value['components'].values()):
        raise ValueError('updates require source-only component-free snapshots')
    return record, descriptor


def _proof(store, descriptor, private):
    value = descriptor.to_dict()['workspace']
    if store.snapshot(value['snapshot_id']).tree_id != value['tree_id']:
        raise ValueError('snapshot tree differs from descriptor')
    _validate_snapshot_entries(store.entries(value['snapshot_id']))
    target = private / ('update-proof-' + uuid.uuid4().hex)
    store.restore(value['snapshot_id'], target)
    if load_capture_policy(target).sha256 != descriptor.to_dict()['capture_policy_sha256']:
        raise ValueError('saved capture policy differs')
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


def _child_view(workspace, private, namespace, room, baseline, accepted):
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
        policy = load_capture_policy(workspace, directory_fd=fd)
        if policy.capture_mode != 'source-only' or not policy.is_excluded('.josh-room-split.json'):
            raise ValueError('child policy must preserve the source-only namespace fence')
        scan = scan_workspace_for_status(workspace)
        for path in scan.paths:
            if path != '.josh-roomignore':
                portable_path(path)
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
            'capture_mode': 'source-only', 'content_sha256': _content_digest(view),
            'content_digest_algorithm': CONTENT_DIGEST_ALGORITHM,
        }
    finally:
        os.close(fd)


def _child_origin(state, room):
    return any(slot['room_id'] == room for op in state.to_dict()['operations'].values()
               if op['phase'] == 'committed' for slot in op['plan']['children'])


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
        now = scan_workspace_for_status(workspace)
        records = _records(workspace, now.paths, fd)
        if (now.signature != plan['source_signature'] or now.capture_policy_sha256 != plan['capture_policy_sha256']
                or plan_digest({**records, '.': records['.'][:3]}) != plan['source_records_sha256']):
            raise ValueError('child saved remotely but local source changed; preserve marker')
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
               authority, store_factory, private, *, approval=None):
    """Plan or apply an independent child Save; never refresh a parent implicitly."""
    _identifier(operation_id)
    envelope, etag, state = _open(authority, namespace)
    if not _child_origin(state, room):
        raise ValueError('Room is not a child in this namespace')
    old = state.to_dict().get('child_updates', {}).get(operation_id)
    if old and old['plan'].get('content_digest_algorithm') != CONTENT_DIGEST_ALGORITHM:
        raise ValueError('legacy child update digest requires reconciliation; immutable history was not changed')
    output = 'jat-' + _slot(namespace, 'child-update:' + operation_id)
    if old and (old['plan']['room_id'], old['plan']['expected_logical_jat_id'], old['plan']['logical_jat_id']) != (room, baseline, output):
        raise ValueError('operation ID already binds another child update')
    accepted = {baseline, output} if old and old['phase'] == 'committed' else {baseline}
    latest = state.to_dict()['dimension_catalog']['projects'][room]['latest']
    if latest not in accepted:
        raise ValueError('child latest advanced; select its exact baseline')
    record, previous = _descriptor(authority, state, room, baseline)
    if Path(workspace).resolve() == private.resolve() or Path(workspace).resolve() in private.resolve().parents:
        raise ValueError('private runtime must be outside child source')
    view, plan = _child_view(workspace, private, namespace, room, baseline, accepted)
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
        changed = _content_digest(proof) != plan['content_sha256']
        if approval is None:
            return {'ok': True, 'status': 'approval-required', 'approval_sha256': digest,
                    'request': request, 'changed': changed, 'snapshot_id': output if changed else baseline}
        if approval != digest:
            raise ValueError('child Save approval is stale')
        if not changed and old is None:
            try:
                _bind_child(workspace, dimension, namespace, room, baseline, plan, authority)
            except (OSError, ValueError):
                return _binding_failure(namespace, room, baseline, operation_id, False)
            return {'ok': True, 'status': 'unchanged', 'snapshot_id': baseline,
                    'namespace': namespace, 'catalog_writes': False, 'parent_refreshed': False,
                    'marker_verified': context_status(workspace).get('snapshot_id') == baseline}
        if old is None:
            body = _v2(state)
            body['child_updates'][operation_id] = {'plan': plan, 'plan_sha256': plan_digest(plan), 'phase': 'reserved', 'pin': None}
            body['revision'] += 1
            state = CompositionSafetyState.from_dict(body)
            envelope, etag = _commit(authority, envelope, etag, state)
        if state.to_dict()['child_updates'][operation_id]['phase'] != 'committed':
            def claim():
                nonlocal envelope, etag
                envelope, etag = _claim(authority, envelope, etag, plan)
            descriptor, scan = _capture(view, store, private, plan, dimension,
                authority.binding['encryption_domain_id'], claim=claim, parent=previous)
            value = descriptor.to_dict()
            if value['parent_logical_jat_id'] != baseline or value['workspace']['parent_snapshot_id'] != record['workspace_snapshot_id']:
                raise ValueError('child update lineage differs')
            verified = _proof(store, descriptor, private)
            if _content_digest(verified) != plan['content_sha256']:
                raise ValueError('saved child bytes differ from approved inventory')
            ref = authority.put_descriptor(descriptor)
            body = state.to_dict()
            catalog = Catalog.from_body(body['dimension_catalog']).add_logical_snapshot(room, 'Child Room', descriptor, ref, scan.signature)
            body['dimension_catalog'] = catalog.body
            body['child_updates'][operation_id].update(phase='committed', pin={'room_id': room, 'logical_jat_id': output, 'descriptor_sha256': ref.sha256})
            body['revision'] += 1
            state = CompositionSafetyState.from_dict(body)
            envelope, etag = _commit(authority, envelope, etag, state)
        else:
            _, committed = _descriptor(authority, state, room, output)
            if committed.to_dict().get('parent_logical_jat_id') != baseline or _content_digest(_proof(store, committed, private)) != plan['content_sha256']:
                raise ValueError('committed child output differs; preserve history')
        try:
            _bind_child(workspace, dimension, namespace, room, output, plan, authority)
        except (OSError, ValueError):
            return _binding_failure(namespace, room, output, operation_id, True)
        return {'ok': True, 'status': 'saved', 'namespace': namespace, 'room_id': room,
                'snapshot_id': output, 'parent_refreshed': False, 'marker_verified': context_status(workspace).get('snapshot_id') == output}


def refresh_parent(dimension, namespace, room, baseline, operation_id, pins,
                   authority, store_factory, private, *, approval=None):
    """Publish a new immutable parent from saved bytes and explicitly selected pins."""
    _identifier(operation_id)
    envelope, etag, state = _open(authority, namespace)
    body = state.to_dict()
    previous_op = next((op for op in body['operations'].values() if op['phase'] == 'committed'
        and op['parent_pin']['room_id'] == room and op['parent_pin']['logical_jat_id'] == baseline), None)
    if previous_op is None:
        raise ValueError('parent baseline is not committed')
    old = body['operations'].get(operation_id)
    output = 'jat-' + _slot(namespace, 'parent-refresh:' + operation_id)
    if old and (old['plan']['expected_parent_logical_jat_id'], old['plan']['parent_logical_jat_id']) != (baseline, output):
        raise ValueError('operation ID already binds another parent refresh')
    latest = body['dimension_catalog']['projects'][room]['latest']
    if latest != baseline and not (old and old['phase'] == 'committed' and latest == output):
        raise ValueError('parent latest advanced; select its exact baseline')
    _, previous = _descriptor(authority, state, room, baseline)
    if not isinstance(pins, dict) or not pins:
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
        request = {'action': 'refresh-parent', 'namespace': namespace, 'binding': authority.binding,
                   'operation_id': operation_id, 'room_id': room, 'baseline': baseline, 'pins': pins}
        digest = plan_digest(request)
        if approval is None:
            return {'ok': True, 'status': 'approval-required', 'request': request, 'approval_sha256': digest,
                    'changed': changed, 'snapshot_id': output if changed else baseline}
        if approval != digest:
            raise ValueError('parent refresh approval differs')
        if not changed and old is None:
            return {'ok': True, 'status': 'unchanged', 'snapshot_id': baseline, 'catalog_writes': False}
        (view / MANIFEST).write_bytes(canonical(manifest))
        scan = scan_workspace_for_status(view)
        plan = {'source_path_sha256': plan_digest(request), 'capture_policy_sha256': scan.capture_policy_sha256,
                'parent_room_id': room, 'parent_logical_jat_id': output, 'expected_parent_logical_jat_id': baseline,
                'capture_mode': 'source-only', 'children': [{'path': row['path'], 'room_id': row['room_id'],
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
            state = CompositionSafetyState.from_dict(candidate)
            envelope, etag = _commit(authority, envelope, etag, state)
        op = state.to_dict()['operations'][operation_id]
        if op['phase'] == 'reserved':
            candidate = state.to_dict()
            candidate['operations'][operation_id]['phase'] = 'children-recorded'
            candidate['revision'] += 1
            state = CompositionSafetyState.from_dict(candidate)
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
            state = record_parent(state, operation_id, plan_digest(plan), descriptor=descriptor,
                object_ref=authority.put_descriptor(descriptor), workspace_signature=saved_scan.signature, display_name='Catalog Room')
            envelope, etag = _commit(authority, envelope, etag, state)
        else:
            _, committed = _descriptor(authority, state, room, output)
            if _content_digest(_proof(store, committed, private)) != _content_digest(view):
                raise ValueError('committed parent output differs; preserve history')
        return {'ok': True, 'status': 'saved', 'namespace': namespace, 'room_id': room,
                'snapshot_id': output, 'workspace_modified': False, 'previous_parent_snapshot_id': baseline}
