"""Ordinary metadata discovery never enrolls a home or authorizes capture."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from josh_room import native_agents_cli
from josh_room import native_session_inventory as inventory

pytestmark = pytest.mark.skipif(inventory.sys.platform != "linux", reason="Linux metadata inventory")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("T3CODE_HOME", str(tmp_path / "t3"))
    return workspace


def rollout(cwd, name="a", *, archived=False):
    path = Path(os.environ["CODEX_HOME"]) / ("archived_sessions" if archived else "sessions/2026/11/23") / (name + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": name, "cwd": str(cwd)}}) + '\n{"private_body":"never-display"}\n')
    return path


def database(workspace, *, v2=True, wal=False):
    path = Path(os.environ["T3CODE_HOME"]) / "userdata/statev2.sqlite"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript("""
        CREATE TABLE projection_projects(project_id TEXT, workspace_root TEXT, deleted_at TEXT);
        CREATE TABLE projection_threads(thread_id TEXT, project_id TEXT, worktree_path TEXT, deleted_at TEXT);
        CREATE TABLE provider_session_runtime(thread_id TEXT);
    """)
    if v2:
        connection.execute("CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT, project_id TEXT, payload_json TEXT, deleted_at TEXT)")
    connection.execute("INSERT INTO projection_projects VALUES ('p', ?, NULL)", (str(workspace),))
    connection.commit()
    return connection, path


def add_thread(connection, identity, *, v2=True, path=None, deleted=None):
    table = "orchestration_v2_projection_threads" if v2 else "projection_threads"
    value = json.dumps({"worktreePath": path, "title": "private-title"}) if v2 else path
    connection.execute(f"INSERT INTO {table} VALUES (?, 'p', ?, ?)", (identity, value, deleted))
    connection.commit()


def test_exact_and_child_counts_exclude_parent_sibling_and_deduplicate(homes):
    for name, cwd in (("exact", homes), ("child", homes / "child"), ("parent", homes.parent), ("sibling", homes.parent / "workspace-other")):
        rollout(cwd, name)
    rollout(homes, "exact", archived=True)
    result = inventory.inventory(homes)
    assert result["tools"]["codex"] == {"state": "complete", "exact": 1, "descendant": 1}
    encoded = json.dumps(result)
    for private in (str(homes), "private_body", "never-display", "workspace-other"):
        assert private not in encoded


def test_absent_homes_and_empty_inventory_are_content_free(homes):
    assert inventory.inventory(homes) == {"contract_version": 1, "tools": {tool: {"state": "complete", "exact": 0, "descendant": 0} for tool in ("codex", "t3")}}


@pytest.mark.parametrize("kind", ["symlink", "fifo", "malformed", "compressed", "oversized"])
def test_unsafe_unknown_and_bounded_codex_inputs_never_claim_complete(homes, kind):
    path = rollout(homes)
    if kind == "symlink":
        path.unlink()
        path.symlink_to(homes / "outside")
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif kind == "malformed":
        path.write_text('{"type":"future-format"}\n')
    elif kind == "compressed":
        path.rename(path.with_suffix(".jsonl.zst"))
    else:
        path.write_bytes(b"x" * (inventory.MAX_HEADER_BYTES + 1))
    result = inventory.inventory(homes)["tools"]["codex"]
    assert result["state"] == "partial"
    assert result["exact"] == 0


def test_entry_limit_is_reported_as_incomplete(homes, monkeypatch):
    rollout(homes)
    monkeypatch.setattr(inventory, "MAX_ENTRIES", 1)
    assert inventory.inventory(homes)["tools"]["codex"]["reason"] == "inventory-limit"


def test_disappearing_rollout_does_not_hide_other_sessions_or_claim_complete(homes, monkeypatch):
    rollout(homes, "a")
    rollout(homes, "z")
    original = os.open

    def vanished(path, *args, **kwargs):
        if path == "z.jsonl":
            raise FileNotFoundError
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", vanished)
    result = inventory.inventory(homes)["tools"]["codex"]
    assert result == {"state": "partial", "exact": 1, "descendant": 0, "reason": "source-changed"}


@pytest.mark.parametrize("variable", ["CODEX_HOME", "T3CODE_HOME"])
def test_relative_custom_home_is_not_treated_as_no_sessions(homes, monkeypatch, variable):
    monkeypatch.setenv(variable, "relative-home")
    tool = "codex" if variable == "CODEX_HOME" else "t3"
    assert inventory.inventory(homes)["tools"][tool]["state"] == "unavailable"


@pytest.mark.parametrize("v2", [True, False])
def test_t3_counts_exact_children_and_worktree_overrides_without_messages(homes, v2):
    connection, path = database(homes, v2=v2)
    add_thread(connection, "exact", v2=v2)
    add_thread(connection, "child", v2=v2, path=str(homes / "child"))
    add_thread(connection, "unrelated", v2=v2, path=str(homes.parent / "other"))
    add_thread(connection, "deleted", v2=v2, deleted="yesterday")
    connection.close()
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    result = inventory.inventory(homes)["tools"]["t3"]
    assert result == {"state": "complete", "exact": 1, "descendant": 1}
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == before
    assert "private-title" not in json.dumps(result)


def test_t3_reads_committed_wal_with_empty_legacy_tables(homes):
    connection, _path = database(homes, wal=True)
    try:
        add_thread(connection, "v2-only", path=str(homes / "child"))
        assert connection.execute("SELECT count(*) FROM projection_threads").fetchone()[0] == 0
        assert inventory.inventory(homes)["tools"]["t3"] == {"state": "complete", "exact": 0, "descendant": 1}
    finally:
        connection.close()


def test_closed_wal_database_does_not_create_sidecars(homes):
    connection, path = database(homes, wal=True)
    add_thread(connection, "closed")
    connection.close()
    before = {p.name: p.read_bytes() for p in path.parent.iterdir()}
    assert inventory.inventory(homes)["tools"]["t3"]["exact"] == 1
    assert {p.name: p.read_bytes() for p in path.parent.iterdir()} == before


def test_mixed_t3_layouts_keep_distinct_histories_but_v2_overrides_legacy(homes):
    connection, _path = database(homes)
    add_thread(connection, "legacy-only", v2=False)
    add_thread(connection, "both", v2=False)
    add_thread(connection, "both", path=str(homes / "child"))
    add_thread(connection, "deleted", v2=False)
    add_thread(connection, "deleted", deleted="yesterday")
    connection.close()
    assert inventory.inventory(homes)["tools"]["t3"] == {"state": "complete", "exact": 1, "descendant": 1}


@pytest.mark.parametrize("kind", ["unknown-schema", "corrupt", "symlink", "locked"])
def test_t3_errors_are_distinct_from_empty_success(homes, kind):
    connection, path = database(homes)
    if kind == "unknown-schema":
        connection.execute("DROP TABLE orchestration_v2_projection_threads")
        connection.execute("DROP TABLE projection_threads")
        connection.commit()
    elif kind == "corrupt":
        connection.close()
        path.write_text("not a sqlite database")
    elif kind == "symlink":
        connection.close()
        path.unlink()
        path.symlink_to(homes / "missing")
    else:
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("UPDATE projection_projects SET workspace_root='uncommitted'")
    try:
        result = inventory.inventory(homes)["tools"]["t3"]
        assert result["state"] == ("unsupported" if kind == "unknown-schema" else "unavailable")
        assert result["exact"] == result["descendant"] == 0
        assert str(path) not in json.dumps(result)
    finally:
        connection.close()


def test_inventory_cli_does_not_require_room_or_grant(homes, capsys):
    rollout(homes)
    assert native_agents_cli.main(["inventory", "--workspace", str(homes), "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["session_inventory"]["tools"]["codex"]["exact"] == 1
    assert "profile_ref" not in json.dumps(result)
