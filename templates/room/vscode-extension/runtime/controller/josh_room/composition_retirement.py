"""Exact logical retirement in encrypted namespace authority; never payload GC."""
from __future__ import annotations

import copy
import hashlib

from .composition_safety import CompositionSafetyState, _identifier
from .composition_transaction import canonical


def _target(kind, room=None, snapshot=None):
    if kind not in ('recovery-point', 'room', 'catalog'):
        raise ValueError('invalid retirement kind')
    if kind == 'catalog':
        if room is not None or snapshot is not None:
            raise ValueError('Catalog retirement selects only its namespace')
    else:
        _identifier(room)
        if kind == 'recovery-point':
            _identifier(snapshot)
            if snapshot.casefold() == 'latest':
                raise ValueError('retirement requires an exact recovery point')
        elif snapshot is not None:
            raise ValueError('Room retirement cannot select a recovery point')
    return {'kind': kind, 'room': room, 'snapshot': snapshot}


def _retired(envelope):
    return [record['target'] for record in envelope.get('retirements', [])]


def require_active(envelope, room=None, snapshot=None):
    """Fence exact reads and updates without modifying historical evidence."""
    for target in _retired(envelope):
        if (target['kind'] == 'catalog'
                or target['kind'] == 'room' and target['room'] == room
                or target['kind'] == 'recovery-point' and (target['room'], target['snapshot']) == (room, snapshot)):
            raise ValueError('selected Catalog, Room or recovery point is retired')


def active_inventory(envelope):
    """Authenticated callers retain empty Catalogs and empty child Rooms."""
    body = CompositionSafetyState.from_dict(envelope['state']).to_dict()
    retired = _retired(envelope)
    catalog_active = not any(t['kind'] == 'catalog' for t in retired)
    parents = {op['plan']['parent_room_id'] for op in body['operations'].values()}
    children = {row['room_id'] for op in body['operations'].values() for row in op['plan']['children']}
    rooms = {}
    for room in sorted(parents | children | body['dimension_catalog']['projects'].keys()):
        if not catalog_active or any(t['kind'] == 'room' and t['room'] == room for t in retired):
            continue
        project = body['dimension_catalog']['projects'].get(room, {})
        records = project.get('snapshots', {})
        points = [point for point in records if not any(t['kind'] == 'recovery-point'
                  and (t['room'], t['snapshot']) == (room, point) for t in retired)]
        points.sort(key=lambda point: (records[point]['created_at'], point), reverse=True)
        rooms[room] = {'kind': 'catalog' if room in parents else 'room',
                       'recovery_points': points,
                       'latest': project.get('latest') if project.get('latest') in points else (points[0] if points else None)}
    return {'catalog_active': catalog_active, 'rooms': rooms}


def _blockers(envelope, target, *, pending=True):
    body = envelope['state']
    inventory = active_inventory(envelope)
    if not inventory['catalog_active']:
        raise ValueError('Catalog is already retired')
    kind, room, snapshot = (target[key] for key in ('kind', 'room', 'snapshot'))
    selected = inventory['rooms'].get(room)
    if kind != 'catalog' and selected is None:
        raise ValueError('Room is unavailable or retired')
    if kind == 'recovery-point' and snapshot not in selected['recovery_points']:
        raise ValueError('exact recovery point is unavailable or retired')
    if kind == 'room' and selected['kind'] != 'room':
        raise ValueError('parent identity requires Catalog retirement')
    blockers = []
    if pending and (any(op['phase'] != 'committed' for op in body['operations'].values()) or any(
            op['phase'] != 'committed' for op in body.get('child_updates', {}).values())):
        blockers.append({'reason': 'incomplete-reservation'})
    # A claim without its committed catalog record is an interrupted writer.
    recorded = {point for project in body['dimension_catalog']['projects'].values() for point in project['snapshots']}
    if pending and set(envelope.get('capture_claims', {})) - recorded:
        blockers.append({'reason': 'incomplete-capture-claim'})
    if kind == 'recovery-point':
        for operation_id, op in sorted(body['operations'].items()):
            parent = op['parent_pin']
            if parent is None:
                continue
            active = inventory['rooms'].get(parent['room_id'], {})
            if parent['logical_jat_id'] not in active.get('recovery_points', []):
                continue
            if any((pin['room_id'], pin['logical_jat_id']) == (room, snapshot) for pin in op['child_pins'].values()):
                blockers.append({'reason': 'retained-parent-pin', 'operation_id': operation_id,
                                 'room': parent['room_id'], 'snapshot': parent['logical_jat_id']})
    elif kind == 'room':
        if selected['recovery_points']:
            blockers.append({'reason': 'room-not-empty', 'count': len(selected['recovery_points'])})
    else:
        children = [value for value in inventory['rooms'].values() if value['kind'] == 'room']
        points = sum(len(value['recovery_points']) for value in inventory['rooms'].values())
        if children:
            blockers.append({'reason': 'catalog-has-child-rooms', 'count': len(children)})
        if points:
            blockers.append({'reason': 'catalog-has-recovery-points', 'count': points})
    return blockers


def validate_retirements(envelope):
    """Closed, versioned ledger; historical operation and descriptor slots remain."""
    records = envelope['retirements']
    if not isinstance(records, list) or not 1 <= len(records) <= 4096 or envelope['state'] is None:
        raise ValueError('invalid retirement ledger')
    prior = copy.deepcopy(envelope)
    prior['retirements'] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict) or set(record) != {'sequence', 'target', 'approval_sha256'} or type(record['sequence']) is not int or record['sequence'] != index + 1:
            raise ValueError('invalid retirement record')
        target = record['target']
        if not isinstance(target, dict) or set(target) != {'kind', 'room', 'snapshot'} or target != _target(**target):
            raise ValueError('invalid retirement target')
        digest = record['approval_sha256']
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('invalid retirement approval')
        # Replaying against the immutable history also rejects newly introduced
        # live references to retired identities after a later writer commits.
        if _blockers(prior, target, pending=False):
            raise ValueError('retirement ledger violates retained dependencies')
        prior['retirements'].append(record)


def preview_retirement(authority, namespace, kind, room=None, snapshot=None):
    if namespace != authority.namespace:
        raise ValueError('retirement namespace differs from selected authority')
    envelope, etag = authority.read()
    if envelope is None or envelope['state'] is None or not isinstance(etag, str) or not etag:
        raise ValueError('authenticated namespace is unavailable')
    target = _target(kind, room, snapshot)
    blockers = _blockers(envelope, target)
    plan = {'format': 'composition-retirement-plan-v1', 'namespace': namespace,
            'binding': envelope['binding'], 'target': target, 'authority_etag': etag,
            'authority_sha256': hashlib.sha256(canonical(envelope)).hexdigest(),
            'blockers': blockers}
    return {'ok': True, 'can_apply': not blockers, 'plan': plan,
            'approval_sha256': hashlib.sha256(canonical(plan)).hexdigest(),
            'physical_deletion': False}


def validate_active_transition(previous, candidate):
    """Fence new reservations while preserving all existing historical bindings."""
    from .composition_transaction import _require_extension

    for field in ('namespace', 'binding', 'request', 'secret', 'repository_id'):
        if previous[field] != candidate[field]:
            raise ValueError('retirement authority binding is immutable')
    for key, value in previous['capture_claims'].items():
        if candidate['capture_claims'].get(key) != value:
            raise ValueError('capture claims are immutable')
    old, new = previous['state'], candidate['state']
    if old != new:
        _require_extension(old, new)
    for name in ('operations', 'child_updates'):
        for key, operation in new.get(name, {}).items():
            if operation == old.get(name, {}).get(key):
                continue
            plan = operation['plan']
            if name == 'operations':
                require_active(candidate, plan['parent_room_id'], plan['expected_parent_logical_jat_id'])
                require_active(candidate, plan['parent_room_id'], plan['parent_logical_jat_id'])
                for child in plan['children']:
                    require_active(candidate, child['room_id'], child['logical_jat_id'])
            else:
                require_active(candidate, plan['room_id'], plan['expected_logical_jat_id'])
                require_active(candidate, plan['room_id'], plan['logical_jat_id'])


def retire(authority, namespace, kind, room=None, snapshot=None, *, approved_plan_sha256):
    preview = preview_retirement(authority, namespace, kind, room, snapshot)
    if approved_plan_sha256 != preview['approval_sha256']:
        raise ValueError('retirement approval is stale; review a fresh preview')
    if not preview['can_apply']:
        raise ValueError('retirement is blocked by retained dependencies')
    envelope, etag = authority.read()
    if etag != preview['plan']['authority_etag'] or hashlib.sha256(canonical(envelope)).hexdigest() != preview['plan']['authority_sha256']:
        raise ValueError('retirement preview is stale; review a fresh preview')
    updated = copy.deepcopy(envelope)
    updated['format'] = 'composition-authority-v2'
    ledger = updated.setdefault('retirements', [])
    ledger.append({'sequence': len(ledger) + 1, 'target': preview['plan']['target'],
                   'approval_sha256': approved_plan_sha256})
    validate_retirements(updated)
    authority.put(updated, etag)
    return {'ok': True, 'status': 'retired', 'namespace': namespace,
            'target': preview['plan']['target'], 'physical_deletion': False}
