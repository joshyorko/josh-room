"""Exercise scoped metadata admission and selected reads against synthetic files."""
import os

import pytest

from josh_room.view import filesystem
from josh_room.view.contract import ViewValidationError
from josh_room.view.room_provider import RoomWorkspaceProvider


def pages(adapter, object_id="root", generation=None):
    page = adapter.listing(object_id, generation)
    yield page
    while page["cursor"]:
        page = adapter.listing(object_id, page["generation"], page["cursor"])
        yield page


def entries(adapter, object_id="root", generation=None):
    results = list(pages(adapter, object_id, generation))
    assert results[-1]["coverage"] == "complete"
    return results[-1]["generation"], {n["label"]: n for p in results for n in p["entries"]}


def test_alias_admission_and_resume_after_exclusions(tmp_path):
    (tmp_path / ".josh-roomignore").write_text("noise*\n")
    for i in range(257):
        (tmp_path / f"noise{i}").touch()
    for name in ("Src", "src", "actual"):
        (tmp_path / name).mkdir()
    adapter = RoomWorkspaceProvider(tmp_path).open_filesystem()
    results = list(pages(adapter))
    assert results[0]["coverage"] == "partial"
    assert results[0]["entries"] == []
    assert results[-1]["coverage"] == "complete"
    assert results[-1]["omitted"] >= 259
    labels = [n["label"] for p in results for n in p["entries"]]
    assert "actual" in labels and "Src" not in labels and "src" not in labels
    with pytest.raises(ViewValidationError, match="unavailable"):
        adapter.listing("root", results[0]["generation"], results[0]["cursor"])


def test_selected_preview_independent_of_overview_and_page_budget(tmp_path, monkeypatch):
    for i in range(34):
        (tmp_path / f"a{i:02}.py").write_text("a" * 16384)
    directory = tmp_path / "src" / "josh_room"
    directory.mkdir(parents=True)
    (directory / "late.py").write_text("value = 'ready'\n")
    provider = RoomWorkspaceProvider(tmp_path, max_nodes=2, max_total_preview_bytes=0)
    reads = []
    read = provider._read_regular_file_bytes

    def counted(fd, parent, name, info, limit):
        reads.append(name)
        return read(fd, parent, name, info, limit)
    monkeypatch.setattr(provider, "_read_regular_file_bytes", counted)
    adapter = provider.open_filesystem()
    gen, nodes = entries(adapter)
    _, dirs = entries(adapter, nodes["src"]["id"], gen)
    _, files = entries(adapter, dirs["josh_room"]["id"], gen)
    item = files["late.py"]["id"]
    inspected = adapter.inspect(item, gen)
    assert [a["label"] for a in inspected["ancestors"]] == ["/", "src", "josh_room"]
    assert reads == []
    assert adapter.preview(item, gen)["node"]["previews"][0]["body"] == "value = 'ready'\n"
    assert reads == ["late.py"]
    with pytest.raises(ViewValidationError):
        adapter.inspect("../late.py", gen)
    with pytest.raises(ViewValidationError):
        provider.open_filesystem().inspect(item, gen)


@pytest.mark.parametrize("change", ["alias", "marker", "hardlink", "symlink", "content", "policy"])
def test_revoked_handles_cannot_read_after_mutation(tmp_path, change):
    src = tmp_path / "src"
    src.mkdir()
    file = src / "safe.py"
    file.write_text("one")
    policy = tmp_path / ".josh-roomignore"
    policy.write_text("# start\n")
    adapter = RoomWorkspaceProvider(tmp_path).open_filesystem()
    gen, nodes = entries(adapter)
    _, files = entries(adapter, nodes["src"]["id"], gen)
    if change == "alias":
        (src / "SAFE.py").touch()
    elif change == "marker":
        (src / ".josh-room.json").touch()
    elif change == "hardlink":
        os.link(file, src / "second")
    elif change == "symlink":
        file.unlink()
        file.symlink_to(policy)
    elif change == "policy":
        policy.write_text("src/**\n")
    else:
        file.write_text("changed")
    with pytest.raises(ViewValidationError) as failure:
        adapter.preview(files["safe.py"]["id"], gen)
    assert failure.value.code == "filesystem-stale"


def test_boundaries_unsupported_entries_and_redacted_content(tmp_path):
    for name in ("nested", "pointer"):
        (tmp_path / name).mkdir()
    (tmp_path / "nested/.josh-room.json").touch()
    (tmp_path / "pointer/.git").touch()
    (tmp_path / "unsafe.txt").write_text("inspect /home/example/")
    (tmp_path / "data").touch()
    os.link(tmp_path / "data", tmp_path / "alias")
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "link").symlink_to("unsafe.txt")
    adapter = RoomWorkspaceProvider(tmp_path).open_filesystem()
    gen, nodes = entries(adapter)
    assert set(nodes) == {"nested", "pointer", "unsafe.txt"}
    assert adapter.listing(nodes["nested"]["id"], gen)["coverage"] == "reserved-boundary"
    assert adapter.listing(nodes["pointer"]["id"], gen)["coverage"] == "git-pointer-unsupported"
    assert adapter.preview(nodes["unsafe.txt"]["id"], gen)["node"]["previews"][0]["status"] == "redacted"


def test_attachment_digest_and_magic(tmp_path):
    (tmp_path / "picture.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    (tmp_path / "wrong.png").write_text("plain text")
    adapter = RoomWorkspaceProvider(tmp_path).open_filesystem()
    gen, nodes = entries(adapter)
    assert not adapter.preview(nodes["wrong.png"]["id"], gen)["node"]["attachments"]
    item = nodes["picture.png"]["id"]
    ref, = adapter.preview(item, gen)["node"]["attachments"]
    assert adapter.attachment(item, gen, ref["name"], "0" * 64) is None
    assert adapter.attachment(item, gen, ref["name"], ref["digest"]).data.endswith(b"fixture")
    (tmp_path / "picture.png").write_bytes(b"changed")
    with pytest.raises(ViewValidationError):
        adapter.attachment(item, gen, ref["name"], ref["digest"])


def test_root_survives_cache_eviction(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem, "_MAX_HANDLES", 4)
    for i in range(71):
        (tmp_path / f"entry-{i}").touch()
    adapter = RoomWorkspaceProvider(tmp_path).open_filesystem()
    results = list(pages(adapter))
    assert sum(len(p["entries"]) for p in results) == 71
    assert adapter.inspect("root", results[-1]["generation"])["node"]["id"] == "root"


def test_foreign_cursor_and_mutation_during_scan(tmp_path):
    (tmp_path / "child").mkdir()
    for i in range(150):
        (tmp_path / f"n{i}").touch()
    adapter = RoomWorkspaceProvider(tmp_path).open_filesystem()
    gen, nodes = entries(adapter)
    scan = adapter.listing("root", gen)
    with pytest.raises(ViewValidationError):
        adapter.listing(nodes["child"]["id"], gen, scan["cursor"])
    (tmp_path / "N5").touch()
    with pytest.raises(ViewValidationError) as failure:
        adapter.listing("root", gen, scan["cursor"])
    assert failure.value.code == "filesystem-stale"


def test_relisting_parent_cannot_rebind_old_sibling_cursor(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    for i in range(150):
        (folder / f"f{i}.txt").touch()
    adapter = RoomWorkspaceProvider(tmp_path).open_filesystem()
    gen, dirs = entries(adapter)
    target = dirs["folder"]["id"]
    partial = adapter.listing(target, gen)
    assert partial["cursor"] and not partial["entries"]
    (folder / "F1.txt").touch()
    # Root itself has not changed. Its new observation sees the same subfolder inode.
    _, fresh = entries(adapter, "root", gen)
    assert fresh["folder"]["id"] == target
    with pytest.raises(ViewValidationError) as failure:
        adapter.listing(target, gen, partial["cursor"])
    assert failure.value.code == "filesystem-stale"


def test_selected_file_revalidated_after_read(tmp_path, monkeypatch):
    target = tmp_path / "sample.py"
    target.write_text("original")
    provider = RoomWorkspaceProvider(tmp_path)
    adapter = provider.open_filesystem()
    gen, nodes = entries(adapter)
    read = provider._read_regular_file_bytes

    def change_after_read(*args):
        raw = read(*args)
        target.write_text("replacement")
        return raw

    monkeypatch.setattr(provider, "_read_regular_file_bytes", change_after_read)
    with pytest.raises(ViewValidationError) as failure:
        adapter.preview(nodes["sample.py"]["id"], gen)
    assert failure.value.code == "filesystem-stale"


def test_scene_sample_preserves_interactive_handles_and_continuations(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem, "_MAX_HANDLES", 6)
    monkeypatch.setattr(filesystem, "_MAX_SAMPLE_HANDLES", 8)
    for folder in ("active", "first", "second"):
        (tmp_path / folder).mkdir()
        for i in range(170 if folder == "active" else 8):
            (tmp_path / folder / f"entry-{i}").touch()
    (tmp_path / "selected.txt").touch()
    provider = RoomWorkspaceProvider(tmp_path)

    def unexpected_read(*args):
        pytest.fail("directory samples must not read file content")
    monkeypatch.setattr(provider, "_read_regular_file_bytes", unexpected_read)
    adapter = provider.open_filesystem()
    gen, top = entries(adapter)
    active = top["active"]["id"]
    start = adapter.listing(active, gen)
    wide = adapter.listing(active, gen, sample=True)
    assert not wide["entries"] and wide["coverage"] == "partial" and wide["cursor"] is None

    first = adapter.listing(top["first"]["id"], gen, sample=True)
    selected = first["entries"][0]["id"]
    assert adapter.inspect(selected, gen)["node"]["id"] == selected
    adapter.listing(top["second"]["id"], gen, sample=True)
    assert adapter.inspect(selected, gen)["node"]["id"] == selected
    assert adapter.inspect(top["selected.txt"]["id"], gen)["node"]["label"] == "selected.txt"
    # An interactive continuation retains authority even across a changed sampled file.
    (tmp_path / "first" / first["entries"][0]["label"]).write_text("changed")
    with pytest.raises(ViewValidationError) as failure:
        adapter.listing(top["first"]["id"], gen, sample=True)
    assert failure.value.code == "filesystem-stale"
    assert adapter.listing(active, gen, start["cursor"])["entries"]
    adapter.close()


def test_scene_sample_requires_known_scope_and_full_alias_admission(tmp_path):
    (tmp_path / "visible").touch()
    (tmp_path / "ambiguous").touch()
    (tmp_path / "Ambiguous").touch()
    adapter = RoomWorkspaceProvider(tmp_path).open_filesystem()
    gen, _ = entries(adapter)
    assert [node["label"] for node in adapter.listing("root", gen, sample=True)["entries"]] == ["visible"]
    for args in (("root",), ("root", "other"), ("unknown", gen), ("root", gen, "cursor")):
        with pytest.raises(ViewValidationError):
            adapter.listing(*args, sample=True)
    adapter.close()
