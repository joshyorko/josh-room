"""Child history over real age/Restic and a synthetic S3 transport."""
import base64
from contextlib import contextmanager

import pytest
from test_composition_live_tools import NAMESPACE
from test_composition_updates_live import entered, update

from josh_room import cli, composition_lifecycle
from josh_room.composition_authority import CompositionAuthority
from josh_room.room_store_lifecycle import RoomStoreLifecycleError

pytest_plugins = ("test_composition_live_tools",)
pytestmark = pytest.mark.integration


def test_recover_child_history(runtime, monkeypatch):
    r = runtime
    parent_pin, target, child = entered(r)
    root = target / "a"
    (root / "file.txt").write_text("updated child\n")
    plan = update(r, target, child)
    updated = update(r, target, child, approval=plan["approval_sha256"])["snapshot_id"]
    baseline = child["snapshot_id"]
    marker = (root / ".josh-room.json").read_bytes()
    guard = (root / ".josh-room-split.json").read_bytes()
    writes = r.transport.writes
    opened = []

    def factory(secret):
        opened.append(True)
        return r.factory(secret)

    @contextmanager
    def provider(_, dimension, _material, namespace):
        authority = CompositionAuthority(r.authority, dimension, namespace, r.authority.material, r.private)
        yield authority, factory, r.private

    monkeypatch.setattr(composition_lifecycle, "provider_runtime", provider)
    monkeypatch.setattr(cli, "_effective_dimension", lambda _: r.dimension)
    parser = cli.build_parser()

    def invoke(action, *options, point=baseline, namespace=NAMESPACE, room=child["room_id"]):
        args = parser.parse_args(["recovery", action, room, "--dimension", "dimension", "--snapshot", point,
                                  "--namespace", namespace, *options])
        return cli.dispatch(args, r.tmp / "instance")

    for room, point in [(child["room_id"], parent_pin), ("parent", parent_pin), (child["room_id"], "latest")]:
        with pytest.raises(RoomStoreLifecycleError) as error:
            invoke("tree", room=room, point=point)
        assert error.value.code == "recovery-uncommitted-child-pin"
    with pytest.raises(ValueError, match="namespace is unavailable"):
        invoke("tree", namespace="2" * 32)
    with pytest.raises(RoomStoreLifecycleError):
        invoke("diff", "--path", "file.txt", "--compare-snapshot", parent_pin)
    with pytest.raises(RoomStoreLifecycleError):
        invoke("tree", "--expected-domain", "another-domain")
    assert not opened

    listing = invoke("tree")
    assert {entry["name"] for entry in listing["entries"]} >= {"file.txt"}
    assert listing["namespace"] == NAMESPACE and listing["snapshot_id"] == baseline
    inspected = invoke("inspect", "--path", "file.txt", "--include-text")
    assert base64.b64decode(inspected["text_preview"]["data"]) == b"child a original\n"
    diff = invoke("diff", "--path", "file.txt", "--compare-snapshot", updated, "--include-text")
    assert diff["change"] == "modified"
    assert base64.b64decode(diff["after_text_preview"]["data"]) == b"updated child\n"
    assert diff["compare_to"]["snapshot_id"] == updated
    assert diff["compare_to"]["namespace"] == NAMESPACE
    assert diff["encryption_domain_id"] == diff["compare_to"]["encryption_domain_id"] == r.dimension.encryption_domain_id
    recovered = r.tmp / "extracted-file.txt"
    invoke("extract", "--path", "file.txt", "--destination", str(recovered))
    assert recovered.read_text() == "child a original\n"
    with pytest.raises((RoomStoreLifecycleError, FileExistsError)):
        invoke("extract", "--path", "file.txt", "--destination", str(root / "file.txt"))
    assert (root / "file.txt").read_text() == "updated child\n"
    assert (root / ".josh-room.json").read_bytes() == marker
    assert (root / ".josh-room-split.json").read_bytes() == guard
    assert writes == r.transport.writes
