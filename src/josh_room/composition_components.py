"""Explicit per-child component choices using the ordinary Save producers."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

from . import room_store_bridge as bridge
from .component_selection import COMPONENTS, plan_token, selection_request
from .composition_component_contract import artifacts_digest
from .room_store_components import _source_files

_INPUTS = {"homebrew_archive", "homebrew_directory", "homebrew_installed", "hauler_selection"}


def _options(options, previous):
    if options is None:
        if previous is None or not any(previous.to_dict()["components"].values()):
            return None
        options = {"component_selection": {"format_version": 1, "components": dict.fromkeys(COMPONENTS, "keep")}}
    if not isinstance(options, dict) or set(options) - (_INPUTS | {"component_selection"}):
        raise ValueError("invalid namespace component options")
    selection = selection_request(options.get("component_selection"))
    if selection is None:
        raise ValueError("namespace components require explicit selection")
    inputs = {key: options.get(key) for key in _INPUTS}
    for key in ("homebrew_archive", "homebrew_directory"):
        if inputs[key] is not None:
            inputs[key] = _absolute(inputs[key])
    installed = inputs["homebrew_installed"]
    if installed is not None:
        if not isinstance(installed, dict) or set(installed) != {"prefix", "formula"} or not isinstance(installed["formula"], str):
            raise ValueError("invalid installed Homebrew input")
        inputs["homebrew_installed"] = {**installed, "prefix": _absolute(installed["prefix"])}
    hauler = inputs["hauler_selection"]
    if hauler is not None:
        if (not isinstance(hauler, dict) or set(hauler) != {"images", "all_images", "manifests", "files"}
                or type(hauler["all_images"]) is not bool or not isinstance(hauler["images"], list)
                or any(not isinstance(name, str) for name in hauler["images"])
                or (hauler["images"] and hauler["all_images"])
                or not isinstance(hauler["manifests"], list) or not isinstance(hauler["files"], list)
                or any(not isinstance(row, (list, tuple)) or len(row) != 2 or not isinstance(row[1], str) for row in hauler["files"])):
            raise ValueError("invalid Hauler content selection")
        inputs["hauler_selection"] = {
            **hauler, "manifests": [_absolute(path) for path in hauler["manifests"]],
            "files": [(_absolute(path), name) for path, name in hauler["files"]],
        }
    return selection, inputs


def _absolute(path):
    if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
        raise ValueError("namespace component input needs an absolute path")
    return Path(path)


def _verify_hauler_stage(stage):
    archive = stage / "hauler-content.tar.zst"
    if archive.exists():
        from .composition_oci import verify_oci_archive
        metadata = json.loads((stage / "metadata.json").read_bytes())
        verify_oci_archive(archive, metadata["references"])
        return metadata
    return None


class _PreviewStore:
    """Discard staged archives after the producer validates their native identities.

    Temporary locators satisfy the producer interface and are stripped before
    approval. This store has no repository credentials or network operations.
    """

    def __init__(self, previous):
        repository_id = previous.to_dict()["workspace"]["repository_id"] if previous else "0" * 64
        self.repository_info = SimpleNamespace(repository_id=repository_id, repository_format=2)

    def backup(self, stage, **kwargs):
        _verify_hauler_stage(stage)
        return SimpleNamespace(snapshot_id="0" * 64)

    def snapshot(self, snapshot_id):
        return SimpleNamespace(tree_id="0" * 64)


def _resolve(workspace, dimension, room, policy_sha256, options, previous, *, source, store, expected=None):
    from .cli import _jat_root

    parsed = _options(options, previous)
    if parsed is None:
        if expected is not None:
            raise ValueError("missing approved namespace component options")
        return None, dict.fromkeys(COMPONENTS)
    selection, inputs = parsed
    modes = selection["components"]
    installed_capture = bridge._component_inputs(workspace, modes, **inputs)

    def input_token():
        return plan_token(selection, dimension=dimension.dimension_id, project=room, workspace=workspace,
                          policy_sha256=policy_sha256, archive=inputs["homebrew_archive"],
                          directory=inputs["homebrew_directory"], hauler=inputs["hauler_selection"],
                          installed=inputs["homebrew_installed"],
                          prior_snapshot=previous.to_dict()["logical_jat_id"] if previous else None)

    token = input_token()
    if expected is not None and (selection != expected["selection"] or token != expected["inputs_sha256"]):
        raise ValueError("namespace component input approval is stale")

    def check(_previous):
        if token != input_token():
            raise ValueError("namespace component inputs changed during capture")

    with tempfile.TemporaryDirectory(prefix="josh-room-child-components-") as temporary:
        private = Path(temporary)
        # RCC may run relative to its input. Give it only the admitted robot
        # environment closure in private storage, never the original child.
        robot_source = private / "workspace"
        robot_source.mkdir(mode=0o700)
        if modes["rcc_environment"] == "refresh":
            if not (source / "robot.yaml").is_file():
                raise ValueError("selected RCC robot is unavailable")
            for name, data in _source_files(source).items():
                target = robot_source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        prepare, resolve = bridge._component_hooks(
            robot_source, private, modes, component_value=dict.fromkeys(COMPONENTS),
            required_components=[name for name, mode in modes.items() if mode == "refresh"],
            check_component_plan=check, installed_capture=installed_capture,
            homebrew_archive=inputs["homebrew_archive"], homebrew_directory=inputs["homebrew_directory"],
            hauler_selection=inputs["hauler_selection"], jat_root=_jat_root(),
            rcc_runtime=os.environ.get("JOSH_ROOM_RCC_EXE"), cancellation=None,
        )
        prepare(previous)
        components = resolve(store, previous)
        plan = {"selection": selection, "inputs_sha256": token, "artifacts_sha256": artifacts_digest(components)}
        if expected is not None and plan != expected:
            raise ValueError("namespace component artifact differs from approval")
        return plan, components


def component_plan(workspace, dimension, room, policy_sha256, options, previous=None, *, source=None):
    workspace = Path(workspace)
    plan, _ = _resolve(workspace, dimension, room, policy_sha256, options, previous,
                       source=source or workspace, store=_PreviewStore(previous))
    return plan


class _CaptureStore:
    """Reconcile each producer backup within its own transaction slot."""

    def __init__(self, store, logical_id, claim, private):
        self.store = store
        self.repository_info = store.repository_info
        self.logical_id = logical_id
        self.claim = claim
        self.private = private
        self.recovered_hauler = None

    def backup(self, stage, *, parent, cancellation):
        from .composition_lifecycle import _content_digest
        from .room_store_export import _COMPONENT_FILES
        from .room_store_operations import _validate_snapshot_entries

        candidate = _verify_hauler_stage(stage)
        names = {p.name for p in stage.iterdir()}
        name = next((name for name, files in _COMPONENT_FILES.items() if names == set(files)), None)
        if name is None or not callable(self.claim):
            raise ValueError("namespace component backup requires a reserved component slot")
        tags = ("composition-component:" + self.logical_id + ":" + name,)
        prior = self.store.snapshots(tags=tags)
        if not prior:
            self.claim(name)
            return self.store.backup(stage, parent=parent, cancellation=cancellation, tags=tags)
        if len(prior) != 1:
            raise ValueError("ambiguous namespace component backup")
        snapshot = self.store.snapshot(prior[0].snapshot_id)
        entries = _validate_snapshot_entries(self.store.entries(snapshot.snapshot_id))
        if (snapshot.parent_snapshot_id != parent or set(entries) - {"."} != names
                or any(entries[path].entry_type != "file" for path in names)):
            raise ValueError("interrupted namespace component ancestry or inventory differs")
        with tempfile.TemporaryDirectory(dir=self.private) as temp:
            restored = Path(temp) / "component"
            self.store.restore(snapshot.snapshot_id, restored)
            if candidate is not None:
                actual = _verify_hauler_stage(restored)
                transport = {"archive_sha256", "archive_size"}
                if not actual or ({k: v for k, v in actual.items() if k not in transport}
                                  != {k: v for k, v in candidate.items() if k not in transport}):
                    raise ValueError("interrupted Hauler content differs from approval")
                self.recovered_hauler = {k: actual[k] for k in transport}
            elif _content_digest(restored) != _content_digest(stage):
                raise ValueError("interrupted namespace component bytes differ from approval")
        return snapshot

    def snapshot(self, snapshot_id):
        return self.store.snapshot(snapshot_id)


def capture_components(workspace, source, dimension, slot, policy_sha256, options, previous, store, claim, private):
    expected = slot.get("component_plan")
    if expected is None and _options(options, previous) is not None:
        raise ValueError("namespace components require an approved plan")
    capture_store = _CaptureStore(store, slot["logical_jat_id"], claim, private)
    _, components = _resolve(Path(workspace), dimension, slot["room_id"], policy_sha256, options,
                             previous, source=source, store=capture_store,
                             expected=expected)
    if capture_store.recovered_hauler:
        components["hauler_content"].update(capture_store.recovered_hauler)
    return components
