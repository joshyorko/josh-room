"""Fast negative admission tests; never contact providers or read live data."""

import os
from dataclasses import replace

import pytest

from josh_room.composition_lifecycle import _child_view, _copy_paths, capture_plan
from josh_room.config import DimensionConfig


def fixture(tmp_path):
    source = tmp_path / "source"
    (source / "child/sub").mkdir(parents=True)
    (source / "child/sub/file.txt").write_text("approved synthetic bytes")
    dimension = DimensionConfig(
        "dimension",
        "Synthetic",
        "minio",
        "https://storage.example.invalid",
        "synthetic-bucket",
        "synthetic-profile",
        connection_id="connection",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
    )
    assignments = [
        {"path": ".", "outcome": "local-only"},
        {"path": "child", "outcome": "child"},
    ]
    preview = capture_plan(source, dimension, "1" * 32, "parent", assignments)
    return source, dimension, assignments, preview


def test_catalog_save_refuses_insufficient_staging_before_authority_access(tmp_path, monkeypatch):
    from josh_room import composition_lifecycle as lifecycle

    source, dimension, assignments, preview = fixture(tmp_path)
    monkeypatch.setattr(lifecycle, "_staging_free", lambda _: 0)
    with pytest.raises(lifecycle.CompositionAdmissionError) as error:
        lifecycle.save_composition(source, dimension, "1" * 32, "parent", assignments,
                                   preview["approval_sha256"], None, None, tmp_path)
    assert error.value.result["error_code"] == "composition-staging-space"


def test_late_child_file_never_enters_private_allowlist(tmp_path):
    source, _, _, preview = fixture(tmp_path)
    (source / "child/late.txt").write_text("not approved")
    with pytest.raises(ValueError, match="approved child inventory"):
        _child_view(source, "child", tmp_path / "view", preview["request"])
    assert not (tmp_path / "view").exists()


def test_payload_copy_refuses_substituted_ancestor_and_policy_symlink(tmp_path):
    source, _, _, _ = fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "file.txt").write_text("must not be copied")
    (source / "child/sub/file.txt").unlink()
    (source / "child/sub").rmdir()
    (source / "child/sub").symlink_to(outside, target_is_directory=True)
    target = tmp_path / "view"
    target.mkdir()
    with pytest.raises(OSError):
        _copy_paths(source / "child", target, {"sub/file.txt"})
    assert list(target.iterdir()) == []
    (source / "child/.josh-roomignore").symlink_to(outside / "file.txt")
    with pytest.raises(ValueError, match="regular file"):
        _copy_paths(source / "child", target, set())


def test_local_policy_at_child_root_is_supported_and_bound(tmp_path):
    source, dimension, assignments, _ = fixture(tmp_path)
    (source / "child/.josh-roomignore").write_text("generated/**\n")
    preview = capture_plan(source, dimension, "1" * 32, "parent", assignments)
    assert (
        preview["request"]["child_evidence"]["child"]["capture_mode"] == "full-repository"
    )
    changed = capture_plan(
        source,
        replace(dimension, bucket="other-bucket"),
        "1" * 32,
        "parent",
        assignments,
    )
    assert preview["approval_sha256"] != changed["approval_sha256"]


def test_copy_keeps_admitted_directory_when_original_path_is_replaced(tmp_path, monkeypatch):
    from josh_room import composition_lifecycle
    source, _, _, preview = fixture(tmp_path)
    original_copy = composition_lifecycle._copy_paths
    original = tmp_path / 'admitted-original'
    substituted = tmp_path / 'substituted'
    def replaced_path(*args, **kwargs):
        source.rename(original)
        (source / 'child/sub').mkdir(parents=True)
        (source / 'child/sub/file.txt').write_text('unapproved replacement payload')
        try:
            return original_copy(*args, **kwargs)
        finally:
            source.rename(substituted)
            original.rename(source)
    monkeypatch.setattr(composition_lifecycle, '_copy_paths', replaced_path)
    view = tmp_path / 'view'
    _child_view(source, 'child', view, preview['request'])
    assert (view / 'sub/file.txt').read_text() == 'approved synthetic bytes'


def test_capture_plan_runtime_refusal_is_bounded_json(tmp_path, monkeypatch, capsys):
    from josh_room import cli

    monkeypatch.setattr(
        cli,
        "dispatch",
        lambda *_: (_ for _ in ()).throw(RuntimeError("private-error-sentinel")),
    )
    code = cli.main(
        [
            "composition",
            "capture-plan",
            "--workspace",
            str(tmp_path),
            "--dimension",
            "dimension",
            "--namespace",
            "1" * 32,
            "--parent-room",
            "parent",
        ]
    )
    assert code != 0
    assert "private-error-sentinel" not in capsys.readouterr().out


def test_namespace_marker_fences_ordinary_save_without_guard_and_when_moved(tmp_path):
    import shutil

    from josh_room.composition import require_uncomposed_workspace
    from josh_room.room_store_operations import scan_workspace_for_status
    from josh_room.workspace_state import context_status, write_stat_workspace_marker

    root = tmp_path / "child"
    root.mkdir()
    (root / "file.txt").write_text("synthetic child")
    scan = scan_workspace_for_status(root)
    marker = write_stat_workspace_marker(
        root,
        dimension_id="dimension",
        project_id="child",
        snapshot_id="jat-child",
        encryption_domain_id="00000000-0000-4000-8000-000000000001",
        display_name="Child Room",
        workspace_signature=scan.signature,
        signature_algorithm=scan.signature_algorithm,
        capture_policy_sha256=scan.capture_policy_sha256,
        composition_namespace="1" * 32,
    )
    assert marker["format_version"] == 4
    assert context_status(root)["composition_namespace"] == "1" * 32
    with pytest.raises(ValueError, match="composed Save"):
        require_uncomposed_workspace(root)
    moved = tmp_path / "moved"
    shutil.move(root, moved)
    assert not context_status(moved)["ok"]
    with pytest.raises(ValueError, match="composed Save"):
        require_uncomposed_workspace(moved)


@pytest.mark.parametrize('rule, excluded, included', [
    ('child/reports/**', 'reports/new.txt', 'other/reports/new.txt'),
    ('*/reports/**', 'reports/new.txt', 'other/reports/new.txt'),
    ('**/reports/**', 'other/reports/new.txt', 'safe/new.txt'),
    ('child/report?.txt', 'report1.txt', 'other/report1.txt'),
])
def test_child_view_preserves_inherited_rules_for_future_paths(tmp_path, rule, excluded, included):
    from josh_room.workspace_policy import load_capture_policy
    source, dimension, assignments, _ = fixture(tmp_path)
    (source / '.josh-roomignore').write_text(rule + '\n')
    preview = capture_plan(source, dimension, '1' * 32, 'parent', assignments)
    view = tmp_path / 'view'
    _child_view(source, 'child', view, preview['request'])
    policy = load_capture_policy(view)
    assert policy.is_excluded(excluded)
    assert not policy.is_excluded(included)


def test_content_digest_distinguishes_entry_types_and_framed_boundaries(tmp_path):
    from josh_room.composition_lifecycle import _content_digest
    root = tmp_path / 'payload'
    root.mkdir()
    (root / 'entry').mkdir(mode=0o700)
    directory = _content_digest(root)
    (root / 'entry').rmdir()
    (root / 'entry').touch(mode=0o700)
    assert _content_digest(root) != directory
    # The old unframed stream could absorb an entire subsequent entry header.
    (root / 'entry').unlink()
    (root / 'a').write_bytes(b'b\x00420\x00')
    before = _content_digest(root)
    (root / 'a').write_bytes(b'')
    (root / 'b').touch()
    assert _content_digest(root) != before


@pytest.mark.parametrize('recovered', [False, True])
def test_capture_refuses_unexpected_actual_parent_before_descriptor_publication(tmp_path, recovered):
    import shutil
    from types import SimpleNamespace

    from josh_room.composition_lifecycle import _capture
    from josh_room.restic_store import SnapshotEntry
    source, dimension, _, _ = fixture(tmp_path)
    source = source / 'child'
    snapshot = SimpleNamespace(snapshot_id='a' * 64, tree_id='b' * 64,
                               parent_snapshot_id='f' * 64, time='2026-11-22T00:00:00Z')
    def restore(_snapshot, target):
        shutil.copytree(source, target)
    store = SimpleNamespace(
        snapshots=lambda **_: [snapshot] if recovered else [], snapshot=lambda _: snapshot,
        snapshot_backup_summary=lambda _: SimpleNamespace(data_added=123, data_added_packed=99),
        backup=lambda *_, **__: SimpleNamespace(snapshot_id=snapshot.snapshot_id),
        entries=lambda _: [SnapshotEntry('.', 'dir', None, 0o700, None),
                           SnapshotEntry('sub', 'dir', None, 0o700, None),
                           SnapshotEntry('sub/file.txt', 'file', 24, 0o644, None)], restore=restore)
    with pytest.raises(ValueError, match='ancestry'):
        _capture(source, store, tmp_path, {'logical_jat_id': 'jat-output', 'room_id': 'child'},
                 dimension, dimension.encryption_domain_id, claim=lambda: None)


@pytest.mark.parametrize("compact", [False, True])
def test_rebased_rule_language_matches_parent_at_nested_child_boundaries(compact):
    from itertools import product

    from josh_room.composition_lifecycle import _inherited_exclusions
    from josh_room.workspace_policy import CapturePolicy, _parts
    rules = ['a/reports/**', '*/reports/**', '**/reports/**', 'projects/*/reports/**',
             'projects/**/a/*.txt', '**/cache', 'a/**/cache', 'report?.txt', '**/*.tmp']
    paths = ['/'.join(parts) for n in (1, 2, 3) for parts in product(('a', 'reports', 'cache', 'new.txt', 'new.tmp'), repeat=n)]
    for rule, child in product(rules, ('a', 'projects/a', 'projects/b/a')):
        parent = CapturePolicy('0' * 64, (_parts(rule),), None, 'source-only')
        if parent.is_excluded(child):
            continue
        derived = CapturePolicy('0' * 64, tuple(_parts(p) for p in _inherited_exclusions(parent, child, compact=compact)), None, 'source-only')
        for path in paths:
            assert derived.is_excluded(path) == parent.is_excluded(child + '/' + path), (rule, child, path)


def test_catalog_can_stage_child_with_many_valid_inherited_ignores(tmp_path):
    from josh_room.workspace_policy import load_capture_policy

    source, dimension, _, _ = fixture(tmp_path)
    (source / '.josh-roomignore').write_text(
        '\n'.join(f'**/excluded-{n:03}/**' for n in range(97)) + '\n**/child/private/**\n'
    )
    (source / 'child/sub/excluded-096').mkdir()
    (source / 'child/sub/excluded-096/unwanted.txt').write_text('synthetic excluded content')
    assignments = [{'path': 'child', 'outcome': 'child'}]
    preview = capture_plan(source, dimension, '1' * 32, 'parent', assignments, root_files_only=True)
    target = tmp_path / 'child-view'
    _child_view(source, 'child', target, preview['request'])
    policy = load_capture_policy(target)
    assert policy.is_excluded('sub/excluded-096/future.txt')
    assert policy.is_excluded('private/future.txt')
    assert not policy.is_excluded('sub/private/future.txt')
    assert not (target / 'sub/excluded-096').exists()
    assert (target / 'sub/file.txt').read_text() == 'approved synthetic bytes'


@pytest.mark.parametrize('present', [False, True])
def test_inherited_leading_hash_is_serialized_as_rule_and_reloaded(tmp_path, present):
    from josh_room.workspace_policy import load_capture_policy
    source, dimension, assignments, _ = fixture(tmp_path)
    (source / '.josh-roomignore').write_text('child/#reports/**\n')
    if present:
        (source / 'child/#reports').mkdir()
        (source / 'child/#reports/private.txt').write_text('synthetic excluded report')
    preview = capture_plan(source, dimension, '1' * 32, 'parent', assignments)
    view = tmp_path / 'view'
    _child_view(source, 'child', view, preview['request'])
    assert '\\#reports/**' in (view / '.josh-roomignore').read_text().splitlines()
    reloaded = load_capture_policy(view)
    assert reloaded.is_excluded('#reports/future.txt')
    assert not reloaded.is_excluded('nested/#reports/future.txt')
    assert not (view / '#reports').exists()


def test_child_captures_owned_git_and_parent_does_not_scan_its_contents(tmp_path):
    import subprocess

    source, dimension, assignments, _ = fixture(tmp_path)
    child = source / "child"
    (child / "link").symlink_to("sub/file.txt")
    subprocess.run(["git", "init", "-q", child], check=True)
    subprocess.run(["git", "-C", child, "add", "."], check=True)
    subprocess.run(["git", "-C", child, "-c", "user.name=Synthetic",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "initial"], check=True)
    subprocess.run(["git", "-C", child, "branch", "venv"], check=True)
    preview = capture_plan(source, dimension, "1" * 32, "parent", assignments)
    assert preview["request"]["child_evidence"]["child"]["capture_mode"] == "full-repository"
    view = _child_view(source, "child", tmp_path / "view", preview["request"])
    assert (view / "link").is_symlink()
    assert (view / ".git/refs/heads/venv").read_bytes() == (child / ".git/refs/heads/venv").read_bytes()
    subprocess.run(["git", "-C", view, "fsck", "--full"], check=True)
    (child / ".git/index.lock").touch()
    with pytest.raises(ValueError, match="lock"):
        capture_plan(source, dimension, "1" * 32, "parent", assignments)
    (child / ".git/index.lock").unlink()
    assignments[1]["capture_mode"] = "source-only"
    source_plan = capture_plan(source, dimension, "1" * 32, "parent", assignments)
    source_view = _child_view(source, "child", tmp_path / "source-view", source_plan["request"])
    assert not (source_view / ".git").exists()
    subprocess.run(["git", "init", "-q", source], check=True)
    with pytest.raises(ValueError, match="one Room"):
        capture_plan(source, dimension, "1" * 32, "parent", assignments)



def test_local_only_repository_cannot_lend_one_parent_file(tmp_path):
    import subprocess

    source, dimension, assignments, _ = fixture(tmp_path)
    repository = source / "other"
    subprocess.run(["git", "init", "-q", repository], check=True)
    (repository / "readme").write_text("parent selection")
    assignments.append({"path": "other/readme", "outcome": "parent"})
    with pytest.raises(ValueError, match="one Room"):
        capture_plan(source, dimension, "1" * 32, "parent", assignments)


def test_initial_component_approval_keeps_local_options_out_of_request(tmp_path, monkeypatch):
    import json

    from josh_room import composition_components

    source, dimension, assignments, _ = fixture(tmp_path)
    selection = {"format_version": 1, "components": {
        "rcc_environment": "omit", "homebrew_recovery": "refresh", "hauler_content": "omit"
    }}
    options = {"homebrew_directory": "/synthetic/private-recovery-input", "component_selection": selection}
    approved = {"selection": selection, "inputs_sha256": "1" * 64, "artifacts_sha256": "2" * 64}

    def preview(workspace, selected_dimension, room, policy_sha256, component_options, *, admitted_paths):
        assert "sub/file.txt" in admitted_paths
        assert (workspace, selected_dimension, component_options) == (source / "child", dimension, options)
        return approved

    monkeypatch.setattr(composition_components, "component_plan", preview)
    assignments[1]["component_options"] = options
    request = capture_plan(source, dimension, "1" * 32, "parent", assignments)["request"]
    assert request["plan_version"] == 4
    assert request["child_evidence"]["child"]["component_plan"] == approved
    assert options["homebrew_directory"] not in json.dumps(request)
    assert "component_options" not in request["assignments"][1]
    _child_view(source, "child", tmp_path / "view", request)
    assignments[0]["component_options"] = options
    with pytest.raises(ValueError, match="invalid capture assignment"):
        capture_plan(source, dimension, "1" * 32, "parent", assignments)


@pytest.mark.parametrize("kind", ["tracked-policy", "deleted-working-copy", "gitlink-without-gitmodules"])
def test_child_index_conflicts_are_refused_before_approval(tmp_path, kind):
    import subprocess

    source, dimension, assignments, _ = fixture(tmp_path)
    child = source / "child"
    subprocess.run(["git", "init", "-q", "--template=", child], check=True)
    policy = child / ".josh-roomignore"
    policy.write_text("# tracked child policy\n")
    if kind == "gitlink-without-gitmodules":
        subprocess.run(["git", "-C", child, "update-index", "--add", "--cacheinfo",
                        "160000," + "a" * 40 + ",dependency"], check=True)
        expected = "gitlinks"
    else:
        subprocess.run(["git", "-C", child, "add", ".josh-roomignore"], check=True)
        if kind == "deleted-working-copy":
            policy.unlink()
        expected = "tracks generated Room metadata"
    with pytest.raises(ValueError, match=expected):
        capture_plan(source, dimension, "1" * 32, "parent", assignments)


def test_direct_save_refuses_different_binding_before_creating_envelope(tmp_path):
    from types import SimpleNamespace

    from josh_room.composition_lifecycle import save_composition

    source, dimension, assignments, preview = fixture(tmp_path)
    authority = SimpleNamespace(
        namespace="1" * 32,
        binding={**preview["request"]["destination_binding"], "connection_id": "other"},
        read=lambda: (None, None),
        put=lambda *_: pytest.fail("mismatched authority created a namespace"),
    )
    with pytest.raises(ValueError, match="binding"):
        save_composition(source, dimension, authority.namespace, "parent", assignments,
                         preview["approval_sha256"], authority,
                         lambda *_: pytest.fail("opened Restic"), tmp_path)


def test_root_files_only_stages_root_files_and_whole_selected_child(tmp_path):
    from josh_room.composition_lifecycle import _parent_view

    source = tmp_path / "catalog-source"
    (source / "selected/grandchild").mkdir(parents=True)
    (source / "selected/grandchild/payload.txt").write_text("child payload")
    (source / "root.txt").write_text("parent payload")
    (source / "excluded.txt").write_text("excluded payload")
    (source / "local/unselected.txt").parent.mkdir()
    (source / "local/unselected.txt").write_text("local payload")
    (source / ".josh-roomignore").write_text("excluded.txt\n")
    dimension = fixture(tmp_path)[1]
    assignments = [{"path": "selected", "outcome": "child"}]

    preview = capture_plan(
        source, dimension, "2" * 32, "parent", assignments, root_files_only=True
    )
    assert preview["request"]["root_files_only"] is True
    assert preview["request"]["assignments"] == assignments
    assert preview["local_only_acknowledgement_required"] is True

    parent = tmp_path / "parent-view"
    manifest = {"children": [{"path": "selected"}]}
    _parent_view(source, parent, assignments, manifest, preview["request"])
    assert (parent / "root.txt").read_text() == "parent payload"
    assert not (parent / "excluded.txt").exists()
    assert not (parent / "local").exists()
    assert not (parent / "selected").exists()
    child = tmp_path / "child-view"
    _child_view(source, "selected", child, preview["request"])
    assert (child / "grandchild/payload.txt").read_text() == "child payload"
    assert not (child / "root.txt").exists()


def test_catalog_deselection_leaves_nonportable_directory_local(tmp_path):
    from josh_room.composition_lifecycle import _parent_view

    source, dimension, _, _ = fixture(tmp_path)
    (source / "nonportable:name").mkdir()
    (source / "nonportable:name/local.txt").write_text("local only")
    assignments = [{"path": "child", "outcome": "child"}]
    with pytest.raises(ValueError, match="invalid composition child path"):
        capture_plan(source, dimension, "1" * 32, "parent",
                     [*assignments, {"path": "nonportable:name", "outcome": "child"}],
                     root_files_only=True)

    preview = capture_plan(source, dimension, "1" * 32, "parent", assignments, root_files_only=True)
    assert list(preview["request"]["child_evidence"]) == ["child"]
    parent = tmp_path / "parent-view"
    _parent_view(source, parent, assignments, {"children": [{"path": "child"}]}, preview["request"])
    child = tmp_path / "child-view"
    _child_view(source, "child", child, preview["request"])
    assert not (parent / "nonportable:name").exists()
    assert (source / "nonportable:name/local.txt").read_text() == "local only"
    assert (child / "sub/file.txt").read_text() == "approved synthetic bytes"

    (source / "nonportable:file").write_text("unsafe root payload")
    with pytest.raises(ValueError, match="workspace path cannot be restored safely"):
        capture_plan(source, dimension, "1" * 32, "parent", assignments, root_files_only=True)


def test_root_files_only_handles_more_than_64_candidates_with_one_child(tmp_path):
    source = tmp_path / "many"
    source.mkdir()
    for index in range(70):
        (source / f"candidate-{index:02}").mkdir()
    dimension = fixture(tmp_path)[1]

    preview = capture_plan(
        source, dimension, "3" * 32, "parent",
        [{"path": "candidate-00", "outcome": "child"}],
        root_files_only=True,
    )
    assert preview["child_count"] == 1
    assert preview["request"]["assignments"] == [
        {"path": "candidate-00", "outcome": "child"}
    ]


def test_root_files_only_accepts_32_children_and_rejects_33(tmp_path):
    source = tmp_path / "limits"
    source.mkdir()
    for index in range(33):
        (source / f"child-{index:02}").mkdir()
    dimension = fixture(tmp_path)[1]
    assignments = [
        {"path": f"child-{index:02}", "outcome": "child"}
        for index in range(32)
    ]

    assert capture_plan(
        source, dimension, "4" * 32, "parent", assignments,
        root_files_only=True,
    )["child_count"] == 32
    with pytest.raises(ValueError, match="invalid capture assignments"):
        capture_plan(
            source, dimension, "4" * 32, "parent", assignments + [
                {"path": "child-32", "outcome": "child"}
            ], root_files_only=True,
        )


def test_root_files_only_binds_mode_into_stale_approval(tmp_path):
    from types import SimpleNamespace

    from josh_room.composition_lifecycle import save_composition

    source, dimension, _assignments, _ = fixture(tmp_path)
    assignments = [{"path": "child", "outcome": "child"}]
    legacy = capture_plan(source, dimension, "5" * 32, "parent", assignments)
    assert "root_files_only" not in legacy["request"]
    authority = SimpleNamespace(
        namespace="5" * 32,
        binding=legacy["request"]["destination_binding"],
        read=lambda: pytest.fail("stale mode opened the namespace"),
    )
    with pytest.raises(ValueError, match="approval is stale"):
        save_composition(
            source, dimension, authority.namespace, "parent", assignments,
            legacy["approval_sha256"], authority,
            lambda *_: pytest.fail("stale mode opened Restic"), tmp_path,
            root_files_only=True,
        )


def test_catalog_admission_checks_same_device_mount_identity(tmp_path, monkeypatch):
    from josh_room import composition_lifecycle

    source = tmp_path / "mounts"
    (source / "child").mkdir(parents=True)
    root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(
            composition_lifecycle, "_mount_id",
            lambda fd: 11 if fd == root_fd else 12,
        )
        with pytest.raises(ValueError, match="mount boundary"), composition_lifecycle._catalog_admission(root_fd, ["child"]):
            pytest.fail("same-device bind mount was admitted")
    finally:
        os.close(root_fd)


def test_root_files_only_keeps_unchecked_and_late_directories_local(tmp_path):
    from josh_room.composition_lifecycle import _parent_view

    source = tmp_path / "late"
    (source / "selected").mkdir(parents=True)
    (source / "formerly-file").write_text("old file")
    dimension = fixture(tmp_path)[1]
    assignments = [{"path": "selected", "outcome": "child"}]
    preview = capture_plan(
        source, dimension, "6" * 32, "parent", assignments,
        root_files_only=True,
    )
    (source / "formerly-file").unlink()
    (source / "formerly-file/nested").mkdir(parents=True)
    (source / "formerly-file/nested/secret.txt").write_text("local only")

    with pytest.raises(ValueError, match="changed"):
        _parent_view(
            source, tmp_path / "late-parent", assignments,
            {"children": [{"path": "selected"}]}, preview["request"],
        )

    fresh = capture_plan(
        source, dimension, "7" * 32, "parent", assignments,
        root_files_only=True,
    )
    parent = tmp_path / "fresh-parent"
    _parent_view(
        source, parent, assignments,
        {"children": [{"path": "selected"}]}, fresh["request"],
    )
    assert not (parent / "formerly-file").exists()
    (source / ".josh-room-split.json").write_text("late guard")
    with pytest.raises(ValueError, match="composed Save"):
        _parent_view(
            source, tmp_path / "guarded-parent", assignments,
            {"children": [{"path": "selected"}]}, fresh["request"],
        )


def test_root_files_only_rejects_immediate_case_aliases_and_selected_symlinks(tmp_path):
    source = tmp_path / "aliases"
    (source / "Selected").mkdir(parents=True)
    (source / "selected").mkdir()
    dimension = fixture(tmp_path)[1]
    with pytest.raises(ValueError, match="aliases|conflict"):
        capture_plan(
            source, dimension, "8" * 32, "parent",
            [{"path": "Selected", "outcome": "child"}],
            root_files_only=True,
        )

    (source / "Selected").rmdir()
    (source / "selected").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (source / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        capture_plan(
            source, dimension, "8" * 32, "parent",
            [{"path": "linked", "outcome": "child"}],
            root_files_only=True,
        )


def test_capture_plan_cli_routes_explicit_root_files_only_mode(tmp_path, monkeypatch):
    from josh_room import cli, composition_lifecycle

    source = tmp_path / "cli-source"
    (source / "selected").mkdir(parents=True)
    dimension = fixture(tmp_path)[1]
    observed = []
    expected = {"ok": True, "approval_sha256": "a" * 64}

    def plan(*args, **kwargs):
        observed.append(kwargs.get("root_files_only", False))
        return expected

    monkeypatch.setattr(cli, "_effective_dimension", lambda _args: dimension)
    monkeypatch.setattr(composition_lifecycle, "resolve_capture_destination",
                        lambda dimension, material, **kwargs: (dimension, material, None))
    monkeypatch.setattr(composition_lifecycle, "capture_plan", plan)
    args = cli.build_parser().parse_args([
        "composition", "capture-plan", "--dimension", "dimension",
        "--namespace", "9" * 32, "--parent-room", "parent",
        "--workspace", str(source), "--assign", "selected=child",
        "--root-files-only",
    ])
    assert cli.dispatch(args, tmp_path / "instance") == expected
    assert observed == [True]
    assert args.root_files_only is True
