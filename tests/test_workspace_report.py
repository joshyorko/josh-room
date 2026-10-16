import os
from pathlib import Path

import pytest

from josh_room.workspace_report import workspace_report


def test_mixed_findings_keep_collision_pair_and_exclusions(tmp_path):
    (tmp_path / "Case").write_text("a")
    (tmp_path / "case").write_text("b")
    (tmp_path / "CON").write_text("c")
    (tmp_path / "external").symlink_to("/unavailable-fixture")
    os.mkfifo(tmp_path / "special")
    (tmp_path / ".venv").mkdir()
    os.mkfifo(tmp_path / ".venv" / "ignored")
    result = workspace_report(tmp_path)
    assert not result["ok"] and result["complete"]
    assert {row["code"] for row in result["findings"]} == {
        "path-collision",
        "invalid-name",
        "unsafe-symlink",
        "special-file",
    }
    assert next(
        row["paths"] for row in result["findings"] if row["code"] == "path-collision"
    ) == ["Case", "case"]
    assert ".venv" not in str(result["findings"])
    assert result == workspace_report(tmp_path)


@pytest.mark.parametrize(
    "limit",
    [
        {"max_entries": 1},
        {"max_findings": 1},
        {"cancelled": lambda: True},
        {"max_seconds": 1e-12},
    ],
)
def test_limits_never_clean(tmp_path, limit):
    for name in ("AUX", "CON", "NUL"):
        (tmp_path / name).write_text("x")
    result = workspace_report(tmp_path, **limit)
    assert not result["ok"] and not result["complete"]
    assert result["stop_reason"]
    assert "workspace_signature" not in result


def test_unreadable_subtree_is_incomplete(tmp_path, monkeypatch):
    (tmp_path / "blocked").mkdir()
    (tmp_path / "safe").write_text("x")
    original = os.scandir

    def scan(path):
        if Path(path).name == "blocked":
            raise PermissionError("fixture inaccessible")
        return original(path)

    monkeypatch.setattr(os, "scandir", scan)
    result = workspace_report(tmp_path)
    assert not result["ok"] and not result["complete"]
    assert result["findings"][0]["paths"] == ["blocked"]


def test_multiple_linked_worktrees_and_main_records_reported_without_mutation(tmp_path):
    import subprocess

    from test_workspace_check import _tree_digest

    repository = tmp_path / "repo"

    def git(*args):
        subprocess.run(["git", *args], check=True, capture_output=True, timeout=20)

    git("init", str(repository))
    (repository / "source.txt").write_text("keep")
    git("-C", str(repository), "add", ".")
    git(
        "-C",
        str(repository),
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "fixture",
    )
    for name in ("one", "two"):
        git("-C", str(repository), "worktree", "add", "-b", name, str(tmp_path / name))
    before = _tree_digest(tmp_path)
    result = workspace_report(tmp_path)
    assert not result["ok"] and result["complete"]
    assert {row.get("exclusion_candidate") for row in result["findings"]} >= {
        "one",
        "two",
    }
    assert len(result["findings"]) >= 4
    assert _tree_digest(tmp_path) == before
    (tmp_path / ".josh-roomignore").write_text("one/**\ntwo/**\n")
    result = workspace_report(tmp_path)
    assert not result["ok"]
    assert all(not row.get("exclusion_candidate") for row in result["findings"])
    (tmp_path / ".josh-roomignore").write_text(
        "one/**\ntwo/**\n# josh-room capture-mode: source-only\n"
    )
    assert workspace_report(tmp_path)["ok"]


def test_relative_link_escapes_and_cycles_never_stat_external_paths(
    tmp_path, monkeypatch
):
    root = tmp_path / "source"
    root.mkdir()
    (root / "dir").mkdir()
    (root / "dir" / "outside").symlink_to("../../outside")
    (root / "nested").symlink_to("dir/outside/payload")
    (root / "cycle-a").symlink_to("cycle-b")
    (root / "cycle-b").symlink_to("cycle-a")
    original = Path.lstat

    def checked_lstat(path, *args, **kwargs):
        assert path != tmp_path / "outside"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", checked_lstat)
    result = workspace_report(root)
    assert not result["ok"] and result["complete"]
    assert {row["paths"][0] for row in result["findings"]} == {
        "dir/outside",
        "nested",
        "cycle-a",
        "cycle-b",
    }
    assert all(row["code"] == "unsafe-symlink" for row in result["findings"])
