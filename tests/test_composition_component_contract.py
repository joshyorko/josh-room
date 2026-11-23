from types import SimpleNamespace

import pytest
from test_room_store_export import REPOSITORY_ID, _descriptor, _Restic

from josh_room.component_selection import COMPONENTS
from josh_room.composition_component_contract import (
    artifacts_digest,
    validate_component_contract,
    validate_component_plan,
    verify_component_snapshots,
)
from josh_room.logical_jat import LogicalJat
from josh_room.room_store_export import PortableExportError


def slot(mode):
    return {"component_plan": {
        "selection": {"format_version": 1, "components": dict.fromkeys(COMPONENTS, mode)},
        "inputs_sha256": "a" * 64,
        "artifacts_sha256": artifacts_digest(_descriptor().to_dict()["components"]),
    }}


@pytest.mark.parametrize("invalid", [None, "{}", {"format_version": True, "components": {}}])
def test_selection_in_ledger_is_closed(invalid):
    plan = slot("keep")["component_plan"]
    plan["selection"] = invalid
    with pytest.raises(ValueError):
        validate_component_plan(plan)


def test_legacy_omitted_missing_and_changed_component_refs_rejected():
    previous = _descriptor()
    empty = _descriptor(dict.fromkeys(COMPONENTS))
    validate_component_contract({}, empty)
    validate_component_contract(slot("keep"), previous, previous)
    validate_component_contract(slot("refresh"), previous)
    for reviewed, actual, baseline in [
        ({}, previous, None), (slot("omit"), previous, None),
        (slot("keep"), empty, previous), (slot("refresh"), empty, previous),
    ]:
        with pytest.raises(ValueError, match="component"):
            validate_component_contract(reviewed, actual, baseline)
    changed = previous.to_dict()
    changed["components"]["rcc_environment"]["archive_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="component"):
        validate_component_contract(slot("keep"), LogicalJat.from_dict(changed), previous)


@pytest.mark.parametrize("fault", ["none", "cross-namespace", "missing", "tree", "archive", "provenance", "rcc-input"])
def test_reachable_and_exact_component_refs(tmp_path, fault):
    descriptor = _descriptor()
    store = _Restic(descriptor)
    store.open_existing = lambda: SimpleNamespace(
        repository_id="0" * 64 if fault == "cross-namespace" else REPOSITORY_ID, repository_format=2
    )
    if fault == "missing":
        store.snapshot = lambda _id: (_ for _ in ()).throw(ValueError("missing"))
    elif fault == "tree":
        store.trees["d" * 64] = "0" * 64
    elif fault == "archive":
        store.data["f" * 64]["homebrew-recovery.tar.zst"] = b"corrupt"
    elif fault == "provenance":
        store.data["2" * 64]["metadata.json"] = b"{}"
    elif fault == "rcc-input":
        value = descriptor.to_dict()
        value["components"]["rcc_environment"]["source_input_sha256"] = "9" * 64
        descriptor = LogicalJat.from_dict(value)
    if fault == "none":
        verify_component_snapshots(store, descriptor, tmp_path)
    else:
        with pytest.raises((ValueError, PortableExportError)):
            verify_component_snapshots(store, descriptor, tmp_path)
    assert not list(tmp_path.iterdir())


def test_v4_reserved_child_cannot_publish_or_hydrate_different_artifact():
    import json
    from pathlib import Path

    from jsonschema import Draft202012Validator, ValidationError
    from referencing import Registry, Resource
    from test_composition_transaction import output, setup

    from josh_room.catalog import Catalog
    from josh_room.composition_safety import (
        CompositionSafetyState,
        plan_digest,
        verify_snapshot_artifacts,
    )
    from josh_room.composition_transaction import record_child, reserve

    reserved, descriptors, _, original = setup()
    plan = original["operations"]["operation"]["plan"]
    plan["plan_version"] = 4
    row = plan["children"][0]
    row.update(slot("refresh"), capture_mode="source-only", capture_policy_sha256="b" * 64,
               source_records_sha256="c" * 64, approved_paths_sha256="d" * 64)
    descriptor = descriptors[("child", "jat-child")].to_dict()
    descriptor["components"] = _descriptor().to_dict()["components"]
    descriptors[("child", "jat-child")] = LogicalJat.from_dict(descriptor)
    catalog = Catalog.from_body(reserved.to_dict()["dimension_catalog"])
    state = reserve(catalog, original["scope"], "operation", plan)
    assert state.to_dict()["contract"] == "composition-safety-v4"
    schema_dir = Path(__file__).resolve().parents[1] / "schemas"
    schemas = [json.loads(path.read_text()) for path in schema_dir.glob("*.schema.json")]
    registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas if "$id" in s)
    schema = next(s for s in schemas if s.get("$id", "").endswith("composition-safety-v4.schema.json"))
    validator = Draft202012Validator(schema, registry=registry)
    saved = record_child(state, "operation", plan_digest(plan), row["path"], **output(original, descriptors, "child"))
    validator.validate(saved.to_dict())
    verify_snapshot_artifacts(saved, descriptors[("child", "jat-child")])
    descriptor["components"]["hauler_content"]["archive_sha256"] = "9" * 64
    descriptors[("child", "jat-child")] = LogicalJat.from_dict(descriptor)
    with pytest.raises(ValueError, match="artifact identities"):
        record_child(state, "operation", plan_digest(plan), row["path"], **output(original, descriptors, "child"))
    with pytest.raises(ValueError, match="artifact identities"):
        verify_snapshot_artifacts(saved, descriptors[("child", "jat-child")])
    bad = saved.to_dict()
    bad["operations"]["operation"]["plan"]["children"][0]["component_plan"]["secret_input"] = "forbidden"
    with pytest.raises(ValidationError):
        validator.validate(bad)
    with pytest.raises(ValueError):
        CompositionSafetyState.from_dict(bad)
    stale = saved.to_dict()
    stale["contract"] = "composition-safety-v3"
    with pytest.raises(ValueError, match="plan version"):
        CompositionSafetyState.from_dict(stale)
