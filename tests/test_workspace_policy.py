import json
import os
import subprocess
from pathlib import Path

import pytest
from jsonschema import validate

from josh_room import workspace_policy
from josh_room.workspace_policy import load_capture_policy


def test_policy_excludes_generated_directories_at_any_depth(tmp_path: Path) -> None:
    policy = load_capture_policy(tmp_path)

    for name in (
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".robocorp",
        ".rcc_home",
    ):
        assert policy.is_excluded(f"services/api/{name}/nested/file")
    assert policy.is_excluded("services/api/.josh-room.json")
    assert policy.is_excluded("services/api/.DS_Store")
    assert not policy.is_excluded("services/api/src/main.py")


def test_shared_defaults_are_versioned_against_the_schema() -> None:
    repository = Path(__file__).resolve().parents[1]
    defaults = json.loads(
        (repository / "src/josh_room/workspace_capture_defaults.json").read_text()
    )
    schema = json.loads(
        (repository / "schemas/workspace-capture-policy-v1.schema.json").read_text()
    )
    validate(defaults, schema)


def test_user_ignore_is_exclusion_only_validated_and_hashed(tmp_path: Path) -> None:
    ignore = tmp_path / ".josh-roomignore"
    ignore.write_text("# Generated assets\n**/generated-*/cache\n", encoding="utf-8")
    policy = load_capture_policy(tmp_path)
    assert policy.is_excluded("app/generated-build/cache/item.bin")
    assert not policy.is_excluded("app/generated-build/source.py")
    first = policy.sha256

    ignore.write_text(
        "# Generated assets changed\n**/generated-*/cache\n", encoding="utf-8"
    )
    assert load_capture_policy(tmp_path).sha256 != first

    for rule in (
        "!src/important.txt\n",
        "/etc/passwd\n",
        "C:/private\n",
        "src/../outside\n",
        "src\\private\n",
        "src/[ab]\n",
    ):
        ignore.write_text(rule, encoding="utf-8")
        with pytest.raises(ValueError):
            load_capture_policy(tmp_path)


def test_user_rules_preserve_root_and_basename_matching_in_python_and_restic(
    tmp_path: Path,
) -> None:
    (tmp_path / ".josh-roomignore").write_text(
        "generated/**\n*.tmp\n?.txt\n**/**/cache\n", encoding="utf-8"
    )
    policy = load_capture_policy(tmp_path)
    assert policy.is_excluded("generated/file")
    assert not policy.is_excluded("app/generated/file")
    assert policy.is_excluded("app/file.tmp")
    assert policy.is_excluded("app/é.txt")
    assert policy.is_excluded("😀.txt")
    assert policy.is_excluded("app/deep/cache/item")
    assert "generated/**" in policy.restic_excludes()
    assert "**/generated/**" not in policy.restic_excludes()


def test_ignore_replacement_after_lstat_is_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    ignore = tmp_path / ".josh-roomignore"
    replacement = tmp_path / "replacement"
    ignore.write_text("src/generated\n", encoding="utf-8")
    replacement.write_text("secrets/**\n", encoding="utf-8")
    original_open = os.open

    def replace_before_open(path, flags, *args, **kwargs):
        if Path(path) == ignore:
            os.replace(replacement, ignore)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_policy.os, "open", replace_before_open)
    with pytest.raises(ValueError, match="changed while opening"):
        load_capture_policy(tmp_path)


def test_ignore_growth_after_lstat_is_rejected(tmp_path: Path, monkeypatch) -> None:
    ignore = tmp_path / ".josh-roomignore"
    ignore.write_text("src/generated\n", encoding="utf-8")
    original_open = os.open

    def grow_before_open(path, flags, *args, **kwargs):
        if Path(path) == ignore:
            with ignore.open("a", encoding="utf-8") as output:
                output.write("x" * (64 * 1024 + 1))
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(workspace_policy.os, "open", grow_before_open)
    with pytest.raises(ValueError, match="changed while opening"):
        load_capture_policy(tmp_path)


def test_active_runtime_root_is_excluded_without_environment_discovery(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "tools" / "runtime"
    runtime.mkdir(parents=True)
    policy = load_capture_policy(tmp_path, active_runtime_root=runtime)
    assert policy.is_excluded("tools/runtime")
    assert policy.is_excluded("tools/runtime/cache/data")
    assert not policy.is_excluded("tools/other/data")
    assert policy.sha256 != load_capture_policy(tmp_path).sha256
    assert policy.restic_excludes()


def test_ignore_file_symlink_and_oversize_fail_closed(tmp_path: Path) -> None:
    ignore = tmp_path / ".josh-roomignore"
    outside = tmp_path.parent / "josh-room-ignore-outside"
    outside.write_text("secrets/**\n", encoding="utf-8")
    try:
        ignore.symlink_to(outside)
        with pytest.raises(ValueError, match="regular file"):
            load_capture_policy(tmp_path)
        ignore.unlink()
        ignore.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
        with pytest.raises(ValueError, match="64 KiB"):
            load_capture_policy(tmp_path)
    finally:
        ignore.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)


def test_python_and_javascript_share_policy_for_nested_virtualenv_symlink(
    tmp_path: Path,
) -> None:
    project = tmp_path / "services" / "example"
    virtualenv = project / ".venv" / "bin"
    virtualenv.mkdir(parents=True)
    interpreter = virtualenv / "python"
    interpreter.symlink_to("/opt/private-runtime/bin/python")
    (tmp_path / ".josh-roomignore").write_text(
        "generated/**\n*.tmp\n?.txt\n**/**/cache\n", encoding="utf-8"
    )
    policy = load_capture_policy(tmp_path)
    relative = "services/example/.venv/bin/python"
    corpus = [
        relative,
        "services/example/src/main.py",
        "generated/cache/index",
        "app/generated/cache/index",
        "app/file.tmp",
        "app/é.txt",
        "😀.txt",
        "app/deep/cache/item",
        ".DS_Store",
    ]

    completed = subprocess.run(
        [
            "node",
            "-e",
            (
                "const {loadCapturePolicy,shouldMarkDirty}=require('./vscode-extension/dirty'); "
                "const args=process.argv.slice(1); "
                "const policy=loadCapturePolicy(args.pop()); "
                "process.stdout.write(JSON.stringify({sha256:policy.sha256,excluded:args.map(p=>!shouldMarkDirty(p,policy)),restic:policy.resticExcludes()}));"
            ),
            *corpus,
            str(tmp_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    assert policy.is_excluded(relative)
    js_policy = json.loads(completed.stdout)
    assert js_policy["sha256"] == policy.sha256
    assert js_policy["excluded"] == [policy.is_excluded(item) for item in corpus]
    assert js_policy["restic"] == list(policy.restic_excludes())
    assert os.readlink(interpreter) == "/opt/private-runtime/bin/python"
