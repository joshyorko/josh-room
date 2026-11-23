"""Read one bounded historical file through an already authenticated recovery context."""
from __future__ import annotations

import hashlib

from .recovery import Recovery, fail
from .recovery_text import text_preview
from .room_store_lifecycle import RoomStoreLifecycleError

MAX_FILE_BYTES = 1024 * 1024


class _Observation(Recovery):
    def blob(self, digest):
        if self.calls >= 256:
            fail("recovery-read-budget")
        return super().blob(digest)


def observe(context, args):
    expected = {
        "ciphertext_sha256": args.expected_ciphertext,
        "tree_id": args.expected_tree,
        "workspace_snapshot_id": args.expected_workspace_snapshot,
        "encryption_domain_id": args.expected_domain,
    }
    if any(value is not None for value in expected.values()) and not all(
        value and context.selected_record.get(key) == value for key, value in expected.items()
    ):
        fail("recovery-exact-snapshot-required")
    path = args.path
    recovery = _Observation(context)
    if recovery.preview_policy(path):
        fail("recovery-observation-policy")
    try:
        node = recovery.selected(path)
    except RoomStoreLifecycleError as error:
        if error.code != "recovery-path-absent":
            raise
        file = None
    else:
        size = node.get("size")
        if (node.get("type") != "file" or node.get("links", 1) != 1
                or type(size) is not int or size < 0):
            fail("recovery-unsafe-type")
        digest = None
        preview = {"state": "oversized"}
        if size <= MAX_FILE_BYTES:
            if not isinstance(node.get("content"), list) or len(node["content"]) > 128:
                fail("recovery-read-budget")
            raw = recovery.content(node, budget=MAX_FILE_BYTES)
            digest = hashlib.sha256(raw).hexdigest()
            preview = text_preview(path, raw) if args.include_text else {"state": "omitted"}
        file = {"type": "file", "size": size, "sha256": digest, "text_preview": preview}
    return recovery.receipt(path=path, file=file, encryption_domain_id=recovery.body["encryption_domain_id"],
                            created_at=recovery.body["created_at"],
                            ciphertext_sha256=recovery.context.selected_record["ciphertext_sha256"])
