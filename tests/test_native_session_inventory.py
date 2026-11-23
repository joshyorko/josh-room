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


def test_workspace_and_session_aliases_use_same_canonical_identity(homes):
    alias = homes.parent / "alias"
    alias.symlink_to(homes, target_is_directory=True)
    rollout(alias)
    for workspace in (alias, homes):
        assert inventory.inventory(workspace)["tools"]["codex"] == {"state": "complete", "exact": 1, "descendant": 0}


@pytest.mark.parametrize("cwd", [None, "", "relative-folder", "/invalid/../folder", 12])
def test_unclassifiable_session_metadata_cannot_claim_complete_absence(homes, cwd):
    path = rollout(homes)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "one", "cwd": cwd}}) + "\n")
    assert inventory.inventory(homes)["tools"]["codex"]["state"] == "partial"


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


def test_legacy_only_database_is_unsupported_not_complete_empty(homes):
    connection, path = database(homes, v2=False)
    add_thread(connection, "exact", v2=False)
    add_thread(connection, "child", v2=False, path=str(homes / "child"))
    connection.close()
    legacy = path.with_name("state.sqlite")
    path.rename(legacy)
    before = legacy.read_bytes()
    assert inventory.inventory(homes)["tools"]["t3"] == {"state": "unsupported", "exact": 0, "descendant": 0, "reason": "unsupported-format"}
    assert legacy.read_bytes() == before
    assert not path.exists()


def test_legacy_rollback_file_does_not_double_count_current_database(homes):
    connection, path = database(homes)
    add_thread(connection, "current")
    connection.close()
    path.with_name("state.sqlite").write_bytes(path.read_bytes())
    assert inventory.inventory(homes)["tools"]["t3"] == {"state": "complete", "exact": 1, "descendant": 0}


@pytest.mark.parametrize("padding", [" ", "\t", "\n"])
def test_t3_home_matches_launcher_whitespace_normalization(homes, monkeypatch, padding):
    connection, _path = database(homes)
    add_thread(connection, "one")
    connection.close()
    monkeypatch.setenv("T3CODE_HOME", padding + os.environ["T3CODE_HOME"] + padding)
    assert inventory.inventory(homes)["tools"]["t3"] == {"state": "complete", "exact": 1, "descendant": 0}


def test_blank_t3_home_uses_default_home(homes, monkeypatch):
    connection, path = database(homes)
    add_thread(connection, "one")
    connection.close()
    default = homes.parent / ".t3"
    path.parent.parent.rename(default)
    monkeypatch.setenv("HOME", str(homes.parent))
    monkeypatch.setenv("T3CODE_HOME", " \t ")
    assert inventory.inventory(homes)["tools"]["t3"]["exact"] == 1


def test_mixed_t3_layouts_keep_distinct_histories_but_v2_overrides_legacy(homes):
    connection, _path = database(homes)
    add_thread(connection, "legacy-only", v2=False)
    add_thread(connection, "both", v2=False)
    add_thread(connection, "both", path=str(homes / "child"))
    add_thread(connection, "deleted", v2=False)
    add_thread(connection, "deleted", deleted="yesterday")
    connection.close()
    assert inventory.inventory(homes)["tools"]["t3"] == {"state": "complete", "exact": 1, "descendant": 1}


def test_live_wal_migration_uses_one_read_snapshot(homes, monkeypatch):
    writer, _path = database(homes, wal=True)
    add_thread(writer, "one", v2=False)
    connect = sqlite3.connect
    migrated = []

    def migrating_reader(*args, **kwargs):
        connection = connect(*args, **kwargs)

        def on_query(sql):
            if "FROM projection_threads" in sql and not migrated:
                migrated.append(True)
                add_thread(writer, "one")

        connection.set_trace_callback(on_query)
        return connection

    try:
        monkeypatch.setattr(sqlite3, "connect", migrating_reader)
        assert inventory.inventory(homes)["tools"]["t3"] == {"state": "complete", "exact": 1, "descendant": 0}
        assert migrated == [True]
    finally:
        writer.close()


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


def test_inventory_writes_private_managed_result_receipt(homes, capsys, monkeypatch):
    rollout(homes)
    target = homes.parent / "runtime/result.json"
    monkeypatch.setenv("JOSH_ROOM_RESULT_FILE", str(target))
    assert native_agents_cli.main(["inventory", "--workspace", str(homes), "--json"]) == 0
    assert json.loads(target.read_text()) == json.loads(capsys.readouterr().out)
    assert target.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("sidecar", "kind"),
    [
        ("statev2.sqlite-wal", "fifo-ro"),
        ("statev2.sqlite-shm", "fifo-ro"),
        ("statev2.sqlite-wal", "fifo-rw"),
        ("statev2.sqlite-wal", "dangling-symlink"),
        ("statev2.sqlite-journal", "dangling-symlink"),
        ("statev2.sqlite-wal", "hardlink"),
    ],
)
def test_t3_unsafe_sidecars_fail_closed_without_hanging(homes, sidecar, kind):
    connection, path = database(homes)
    add_thread(connection, "exact")
    connection.close()
    target = path.with_name(sidecar)
    if kind == "fifo-ro":
        os.mkfifo(target, 0o400)
    elif kind == "fifo-rw":
        os.mkfifo(target, 0o644)
    elif kind == "dangling-symlink":
        target.symlink_to(homes / "missing-sidecar")
    else:
        other = path.with_name("extra-hardlink")
        other.write_bytes(b"synthetic")
        os.link(other, target)
    result = inventory.inventory(homes)["tools"]["t3"]
    assert result == {"state": "unavailable", "exact": 0, "descendant": 0, "reason": "discovery-failed"}


def test_t3_limit_still_runs_post_read_fence_and_unlink_resets_counts(homes, monkeypatch):
    connection, path = database(homes)
    for idx in range(3):
        add_thread(connection, f"t-{idx}")
    connection.close()
    monkeypatch.setattr(inventory, "MAX_ENTRIES", 2)
    orig_match = inventory._match

    def mutate_during_limit(result, cwd, workspace):
        orig_match(result, cwd, workspace)
        if result["exact"] == 2:
            path.with_name("statev2.sqlite-journal").write_bytes(b"synthetic-journal")

    monkeypatch.setattr(inventory, "_match", mutate_during_limit)
    assert inventory.inventory(homes)["tools"]["t3"] == {
        "state": "unavailable",
        "exact": 0,
        "descendant": 0,
        "reason": "discovery-failed",
    }

    path.with_name("statev2.sqlite-journal").unlink()
    monkeypatch.setattr(inventory, "MAX_ENTRIES", 4096)

    def unlink_before_fence(result, cwd, workspace):
        orig_match(result, cwd, workspace)
        if result["exact"] == 3 and path.exists():
            path.unlink()

    monkeypatch.setattr(inventory, "_match", unlink_before_fence)
    assert inventory.inventory(homes)["tools"]["t3"] == {
        "state": "unavailable",
        "exact": 0,
        "descendant": 0,
        "reason": "source-changed",
    }


def test_t3_empty_worktree_falls_back_to_workspace_root_and_rejects_view(homes):
    connection, path = database(homes)
    add_thread(connection, "v2-empty", v2=True, path="")
    add_thread(connection, "legacy-empty", v2=False, path="")
    connection.close()
    assert inventory.inventory(homes)["tools"]["t3"] == {"state": "complete", "exact": 2, "descendant": 0}

    path.unlink()
    connection = sqlite3.connect(path)
    connection.executescript(
        f"""
        CREATE TABLE real_projects(project_id TEXT, workspace_root TEXT, deleted_at TEXT);
        CREATE VIEW projection_projects AS SELECT project_id, workspace_root, deleted_at FROM real_projects;
        CREATE TABLE orchestration_v2_projection_threads(thread_id TEXT, project_id TEXT, payload_json TEXT, deleted_at TEXT);
        INSERT INTO real_projects VALUES ('p', '{homes}', NULL);
        INSERT INTO orchestration_v2_projection_threads VALUES ('t1', 'p', '{{}}', NULL);
        """
    )
    connection.close()
    assert inventory.inventory(homes)["tools"]["t3"] == {
        "state": "unsupported",
        "exact": 0,
        "descendant": 0,
        "reason": "unsupported-format",
    }


def test_codex_per_entry_limits_errors_and_depth0_vanish_preserve_other_sessions(homes, monkeypatch):
    import errno
    import socket

    # 5a: depth-0 FileNotFoundError inside walk() must not be swallowed as absent root.
    sessions = Path(os.environ["CODEX_HOME"]) / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "z-vanish.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"id": "v", "cwd": str(homes)}}) + "\n")
    (sessions / "a-keep.jsonl").write_text(json.dumps({"type": "session_meta", "payload": {"id": "k", "cwd": str(homes)}}) + "\n")
    orig_scandir = os.scandir

    class _VanishingEntry:
        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.name = wrapped.name

        def is_symlink(self):
            if self.name == "z-vanish.jsonl":
                raise FileNotFoundError
            return self._wrapped.is_symlink()

        def is_dir(self, follow_symlinks=False):
            return self._wrapped.is_dir(follow_symlinks=follow_symlinks)

    class _WrappedScandir:
        def __init__(self, cm):
            self._cm = cm

        def __enter__(self):
            return [_VanishingEntry(e) for e in self._cm.__enter__()]

        def __exit__(self, *args):
            return self._cm.__exit__(*args)

    monkeypatch.setattr(os, "scandir", lambda fd: _WrappedScandir(orig_scandir(fd)))
    assert inventory.inventory(homes)["tools"]["codex"] == {
        "state": "partial",
        "exact": 1,
        "descendant": 0,
        "reason": "source-changed",
    }
    monkeypatch.setattr(os, "scandir", orig_scandir)

    # 5b1 & 5b2 & 5c1-5c4: oversized header, deep branch, unreadable file, unix socket,
    # ELOOP race, and symlinked archived_sessions all mark partial while preserving valid sessions.
    for p in list(sessions.iterdir()):
        p.unlink()
    rollout(homes, "valid-1")
    (Path(os.environ["CODEX_HOME"]) / "sessions/2026/11/23/z-huge.jsonl").write_bytes(b"x" * (inventory.MAX_HEADER_BYTES + 10))
    deep = Path(os.environ["CODEX_HOME"]) / "sessions/2026/11/23/d3/d4/d5"
    deep.mkdir(parents=True)
    unreadable = Path(os.environ["CODEX_HOME"]) / "sessions/2026/11/23/y-unreadable.jsonl"
    unreadable.write_text("{}\n")
    unreadable.chmod(0o000)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    monkeypatch.chdir(Path(os.environ["CODEX_HOME"]) / "sessions/2026/11/23")
    sock.bind("x-sock.jsonl")
    archived_link = Path(os.environ["CODEX_HOME"]) / "archived_sessions"
    archived_link.symlink_to(homes / "missing-archive")
    try:
        res = inventory.inventory(homes)["tools"]["codex"]
        assert res["state"] == "partial"
        assert res["exact"] == 1
        assert res["descendant"] == 0
    finally:
        sock.close()
        unreadable.chmod(0o600)

    # Symlink-swap ELOOP race between scandir and os.open preserves valid sessions.
    archived_link.unlink()
    orig_open = os.open

    def eloop_on_huge(p, *args, **kwargs):
        if p == "z-huge.jsonl":
            raise OSError(errno.ELOOP, "Too many levels of symbolic links")
        return orig_open(p, *args, **kwargs)

    monkeypatch.setattr(os, "open", eloop_on_huge)
    res = inventory.inventory(homes)["tools"]["codex"]
    assert res["state"] == "partial"
    assert res["exact"] == 1
