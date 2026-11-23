"""Authenticated child component selections and private pre-publication verification."""
from __future__ import annotations

import re

from .component_selection import COMPONENTS, selection_request


def validate_component_plan(plan):
    if not isinstance(plan, dict) or set(plan) != {"selection", "inputs_sha256", "artifacts_sha256"}:
        raise ValueError("invalid namespace component plan")
    selection = plan["selection"]
    if not isinstance(selection, dict) or selection_request(selection) != selection:
        raise ValueError("invalid namespace component selection")
    for field in ("inputs_sha256", "artifacts_sha256"):
        digest = plan[field]
        if not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid namespace component plan digest")


def validate_component_contract(slot, descriptor, previous=None):
    """Validate the result against the approved action for each component slot."""
    components = descriptor.to_dict()["components"]
    prior = previous.to_dict()["components"] if previous is not None else dict.fromkeys(COMPONENTS)
    validate_component_choices(slot, components, prior)
    verify_artifact_identities(slot, components)


def verify_artifact_identities(slot, components):
    if "component_plan" in slot:
        plan = slot["component_plan"]
        validate_component_plan(plan)
        if artifacts_digest(components) != plan["artifacts_sha256"]:
            raise ValueError("namespace component artifact identities differ from approval")


def validate_component_choices(slot, components, prior):
    """Also applies to the compact exact snapshot/tree refs in the retained catalog."""
    if "component_plan" not in slot:
        if any(components.values()):
            raise ValueError("namespace capture has unapproved components")
        return
    plan = slot["component_plan"]
    validate_component_plan(plan)
    for name, mode in plan["selection"]["components"].items():
        actual = components[name]
        if mode == "omit" and actual is not None:
            raise ValueError("omitted namespace component is present")
        if mode == "refresh" and actual is None:
            raise ValueError("required namespace component is missing")
        if mode == "keep" and actual != prior.get(name):
            raise ValueError("kept namespace component differs from the exact baseline")


def verify_component_snapshots(store, descriptor, private):
    """Prove referenced bytes in this open namespace before committing or reusing a pin."""
    value = descriptor.to_dict()
    if not any(value["components"].values()):
        return
    repository = store.open_existing()
    workspace = value["workspace"]
    if (repository.repository_id != workspace["repository_id"]
            or repository.repository_format != workspace["repository_format"]):
        raise ValueError("component repository differs from the opened namespace")
    from .room_store_export import materialize_components

    with materialize_components(descriptor, store, private) as files:
        if files.hauler_archive is not None:
            from .composition_oci import verify_oci_archive
            verify_oci_archive(files.hauler_archive, value["components"]["hauler_content"]["references"])


def artifacts_digest(components):
    """Bind exact approved artifacts, independent of their destination Restic identities."""
    import hashlib
    import json

    identities = {}
    for name, component in components.items():
        if component is None:
            identities[name] = None
            continue
        # Hauler emits new store UUIDs, audit records and tar timestamps.
        # Its verified content-addressed OCI graph identifies the approved payload.
        transport = {"snapshot", "archive_sha256", "archive_size"} if name == "hauler_content" else {"snapshot"}
        identities[name] = {key: value for key, value in component.items() if key not in transport}
    return hashlib.sha256(json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
