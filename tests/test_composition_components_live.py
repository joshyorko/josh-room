"""Namespace component behavior with real Restic/age and synthetic S3 transport."""
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
import zstandard
from test_composition_live_tools import NAMESPACE

from josh_room import homebrew_prefix, room_store_bridge
from josh_room.component_selection import COMPONENTS
from josh_room.composition_lifecycle import (
    capture_plan,
    hydrate_composition,
    save_composition,
)
from josh_room.composition_updates import refresh_parent, save_child
from josh_room.restic_store import ResticStore
from josh_room.room_store_export import materialize_components
from josh_room.room_store_hauler_runner import _preflight_brew_archive

pytest_plugins = ("test_composition_live_tools", "test_homebrew_declared")

pytestmark = pytest.mark.integration


def options(**inputs):
    modes = dict.fromkeys(COMPONENTS, "omit")
    for name, key in (("homebrew_directory", "homebrew_recovery"),
                      ("homebrew_installed", "homebrew_recovery"), ("hauler_selection", "hauler_content")):
        if name in inputs:
            modes[key] = "refresh"
    if inputs.pop("rcc", False):
        modes["rcc_environment"] = "refresh"
    return {"component_selection": {"format_version": 1, "components": modes}, **inputs}


def capture(r):
    args = (r.source, r.dimension, NAMESPACE, "parent", r.assignments)
    preview = capture_plan(*args)
    return save_composition(*args, preview["approval_sha256"], r.authority, r.factory, r.private)


def enter(r, parent_pin, target, path=None):
    return hydrate_composition(r.dimension, NAMESPACE, "parent", parent_pin, target,
                               r.authority, r.factory, r.private, child_path=path)


def descriptor(r, room, pin):
    envelope, _ = r.authority.read()
    record = envelope["state"]["dimension_catalog"]["projects"][room]["snapshots"][pin]
    return r.authority.descriptor(record)


def read_recipe(archive):
    with (archive.open("rb") as compressed,
          zstandard.ZstdDecompressor().stream_reader(compressed) as stream,
          tarfile.open(fileobj=stream, mode="r|") as tar):
        for member in tar:
            if member.name == "homebrew-recovery/Brewfile":
                return tar.extractfile(member).read()
    pytest.fail("verified Homebrew component is missing its selected Brewfile")


@pytest.fixture
def recipe_validator(runtime, monkeypatch):
    if os.environ.get("JOSH_ROOM_TEST_REFERENCE_JAT_RUNTIME"):
        managed_handoff(monkeypatch)
        return
    # Use the normal tar/zstd verifier without claiming packaged JAT qualification.
    class LocalValidator:
        validate_brew_archive = staticmethod(_preflight_brew_archive)

    monkeypatch.setattr(room_store_bridge, "create_managed_hauler_adapter",
                        lambda *args, **kwargs: LocalValidator())
    monkeypatch.setenv("JOSH_ROOM_JAT_ROOT", str(runtime.private))


def test_component_only_child_save_and_historical_recipes(runtime, recipe_validator, monkeypatch):
    r = runtime
    recipe = r.tmp / "recipe"
    recipe.mkdir()
    original = b'brew "hello"\n'
    updated = b'brew "jq"\n'
    (recipe / "Brewfile").write_bytes(original)
    selection = options(homebrew_directory=str(recipe))
    r.assignments[1]["component_options"] = selection
    parent = capture(r)["parent_snapshot_id"]
    root = r.tmp / "entered"
    enter(r, parent, root)
    child = enter(r, parent, root, "a")
    sibling = enter(r, parent, root, "b")
    empty = dict.fromkeys(COMPONENTS)
    assert descriptor(r, "parent", parent).to_dict()["components"] == empty
    assert descriptor(r, sibling["room_id"], sibling["snapshot_id"]).to_dict()["components"] == empty

    def update(pin, op, approval=None):
        return save_child(root / "a", r.dimension, NAMESPACE, child["room_id"], pin, op,
                          r.authority, r.factory, r.private, approval=approval, component_options=selection)

    old = child["snapshot_id"]
    noop = update(old, "unchanged")
    assert not noop["changed"]
    assert update(old, "unchanged", noop["approval_sha256"])["status"] == "unchanged"
    (recipe / "Brewfile").write_bytes(updated)
    change = update(old, "recipe-update")
    assert change["changed"]
    writes = r.transport.writes
    (recipe / "Brewfile").write_bytes(b'brew "wget"\n')
    with pytest.raises((RuntimeError, ValueError), match="approval|stale|changed"):
        update(old, "recipe-update", change["approval_sha256"])
    assert r.transport.writes == writes
    (recipe / "Brewfile").write_bytes(updated)
    class ProcessLoss(BaseException):
        pass

    component_backups = []
    original_backup = ResticStore.backup

    def interrupt_after_component_backup(store, *args, **kwargs):
        result = original_backup(store, *args, **kwargs)
        if any(tag.startswith("composition-component:") for tag in kwargs.get("tags", ())):
            component_backups.append(result.snapshot_id)
            if len(component_backups) == 1:
                raise ProcessLoss()
        return result

    monkeypatch.setattr(ResticStore, "backup", interrupt_after_component_backup)
    with pytest.raises(ProcessLoss):
        update(old, "recipe-update", change["approval_sha256"])
    envelope, _ = r.authority.read()
    assert envelope["state"]["dimension_catalog"]["projects"][child["room_id"]]["latest"] == old
    new = update(old, "recipe-update", change["approval_sha256"])["snapshot_id"]
    assert new != old
    component = descriptor(r, child["room_id"], new).to_dict()["components"]["homebrew_recovery"]
    assert component_backups == [component["snapshot"]["snapshot_id"]]
    args = (r.dimension, NAMESPACE, "parent", parent, "recipe-refresh", {"a": new},
            r.authority, r.factory, r.private)
    plan = refresh_parent(*args)
    current = refresh_parent(*args, approval=plan["approval_sha256"])["snapshot_id"]
    for parent_pin, pin, expected in ((parent, old, original), (current, new, updated)):
        target = r.tmp / pin
        enter(r, parent_pin, target)
        assert enter(r, parent_pin, target, "a")["snapshot_id"] == pin
        assert not (target / "b").exists()
        saved = descriptor(r, child["room_id"], pin)
        refs = saved.to_dict()["components"]
        assert refs["rcc_environment"] is None and refs["hauler_content"] is None
        envelope, _ = r.authority.read()
        with r.factory(envelope["secret"]) as store:
            store.open_existing()
            assert refs["homebrew_recovery"]["snapshot"]["repository_id"] == store.repository_info.repository_id
            with materialize_components(saved, store, r.private) as files:
                assert read_recipe(files.brew_archive) == expected


def test_selected_installed_formula_materializes_exact_declared_closure(runtime, installed, recipe_validator, monkeypatch):
    r = runtime
    selection = options(homebrew_installed={"prefix": str(installed), "formula": "jq"})
    r.assignments[1]["component_options"] = selection
    parent = capture(r)["parent_snapshot_id"]
    target = r.tmp / "inspect"
    enter(r, parent, target)
    child = enter(r, parent, target, "a")
    saved = descriptor(r, child["room_id"], child["snapshot_id"])
    envelope, _ = r.authority.read()
    with r.factory(envelope["secret"]) as store:
        store.open_existing()
        with materialize_components(saved, store, r.private) as files:
            manifest, contents = homebrew_prefix.read_archive(files.brew_archive)
    expected = homebrew_prefix.capture(installed, "jq")
    assert (manifest, contents) == expected
    assert not (target / "a/Cellar").exists()
    broken = r.tmp / "component-corruption"
    enter(r, parent, broken)
    reference = saved.to_dict()["components"]["homebrew_recovery"]
    snapshot = reference["snapshot"]["snapshot_id"]
    restore = ResticStore.restore

    def corrupt_component(store, snapshot_id, destination):
        restore(store, snapshot_id, destination)
        if snapshot_id == snapshot:
            (destination / reference["member_basename"]).write_bytes(b"corrupted synthetic recovery")

    monkeypatch.setattr(ResticStore, "restore", corrupt_component)
    with pytest.raises((ValueError, RuntimeError), match="component|archive"):
        enter(r, parent, broken, "a")
    assert not (broken / "a").exists()


def managed_handoff(monkeypatch):
    from josh_room import crypto

    # The test runner uses the local Python environment and actual local age.
    # Only the component producer and materializer use the managed JAT runtime.
    age_prefix = Path(shutil.which("age")).parent.parent
    monkeypatch.setattr(crypto, "_runtime_prefixes", lambda _platform: [age_prefix])
    receipt = json.loads(Path(os.environ["JOSH_ROOM_TEST_REFERENCE_JAT_RUNTIME"]).read_text())
    jat = receipt["jat"]
    monkeypatch.setenv("JOSH_ROOM_JAT_ROOT", jat["jatRoot"])
    monkeypatch.setenv("JOSH_ROOM_JAT_RCC_EXE", receipt.get("jatRcc", receipt["rcc"])["executable"])
    monkeypatch.setenv("JOSH_ROOM_JAT_ARTIFACT", jat["artifact"])
    monkeypatch.setenv("JOSH_ROOM_RCC_EXE", receipt["rcc"]["executable"])
    monkeypatch.setenv("JOSH_ROOM_EXTENSION_MODE", "1")
    monkeypatch.setenv("JOSH_ROOM_RCC_HOME", receipt["paths"]["rccHome"])
    monkeypatch.setenv("ROBOCORP_HOME", receipt["paths"]["rccHome"])


@pytest.mark.skipif(not os.environ.get("JOSH_ROOM_TEST_REFERENCE_JAT_RUNTIME"),
                    reason="requires explicitly acquired managed JAT/RCC runtime handoff")
def test_native_rcc_and_hauler_on_independent_children(runtime, monkeypatch):
    """Opt-in capture and hydration using a disposable managed runtime receipt."""
    r = runtime
    managed_handoff(monkeypatch)
    (r.source / "a/robot.yaml").write_text(
        "tasks:\n  Demo:\n    shell: python --version\ncondaConfigFile: conda.yaml\n")
    (r.source / "a/conda.yaml").write_text("channels: [conda-forge]\ndependencies: [python=3.11]\n")
    artifact = r.tmp / "artifact.txt"
    artifact.write_text("independent child artifact\n")
    r.assignments[1]["component_options"] = options(rcc=True)
    r.assignments[2]["component_options"] = options(hauler_selection={
        "images": [], "all_images": False, "manifests": [],
        "files": [[str(artifact), "synthetic-file"]]})
    parent = capture(r)["parent_snapshot_id"]
    root = r.tmp / "entered"
    enter(r, parent, root)
    assert descriptor(r, "parent", parent).to_dict()["components"] == dict.fromkeys(COMPONENTS)
    for path, component in (("a", "rcc_environment"), ("b", "hauler_content")):
        child = enter(r, parent, root, path)
        saved = descriptor(r, child["room_id"], child["snapshot_id"])
        assert {k for k, v in saved.to_dict()["components"].items() if v is not None} == {component}
        envelope, _ = r.authority.read()
        with r.factory(envelope["secret"]) as store:
            store.open_existing()
            with materialize_components(saved, store, r.private) as files:
                if component == "rcc_environment":
                    assert files.rcc_archive.is_file() and files.rcc_metadata.is_file()
                else:
                    assert files.hauler_archive.is_file()
                    consumer = r.private / "hauler-consumer"
                    consumer.mkdir(mode=0o700)
                    for command, flags in (("load", ["--filename", str(files.hauler_archive)]), ("info", [])):
                        receipt_path = consumer / (command + ".json")
                        result = subprocess.run([
                            os.environ["JOSH_ROOM_JAT_RCC_EXE"], "--no-build", "env", "exec",
                            "--artifact", os.environ["JOSH_ROOM_JAT_ARTIFACT"], "--permissive-local",
                            "--inherit-streams", "--receipt-file", str(receipt_path), "--json", "--",
                            "hauler", "store", command, "--store", str(consumer / "store"),
                            "--tempdir", str(consumer), *flags,
                        ], cwd=os.environ["JOSH_ROOM_JAT_ROOT"], check=True, timeout=120,
                            capture_output=True, text=True)
                        receipt = json.loads(receipt_path.read_text())
                        assert receipt["artifactDigest"] == os.environ["JOSH_ROOM_JAT_ARTIFACT"]
                        assert receipt["exitCode"] == 0
                        if command == "info":
                            assert "hauler/synthetic-file:latest" in result.stdout
