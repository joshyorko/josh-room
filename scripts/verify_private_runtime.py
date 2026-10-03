"""Exercise private-path protection in the active installed Python runtime."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from josh_room import private_paths


def _check(checks: list[str], name: str) -> None:
    checks.append(name)


def _verify_windows_acl(path: Path, *, directory: bool) -> None:
    # Read the real ACL through the native API; this proves owner, protected
    # inheritance state, and exact grants independently of POSIX mode bits.
    api = private_paths._windows_api()
    user_sid = api.current_user_sid()
    owner_sid, protected, aces = api.read_security(path)
    if (
        owner_sid != user_sid
        or not protected
        or set(aces) != set(private_paths._expected_aces(user_sid, directory=directory))
    ):
        raise RuntimeError
    private_paths.verify_private_path(path, directory=directory)


def run_probe() -> dict[str, object]:
    platform = sys.platform
    checks: list[str] = []
    temporary = tempfile.TemporaryDirectory(prefix="josh-room-private-probe-")
    root = Path(temporary.name)
    try:
        private_paths.protect_private_directory(root)
        if os.name == "nt":
            _verify_windows_acl(root, directory=True)
            _check(checks, "native-directory-owner-dacl-inheritance")
        else:
            metadata = root.lstat()
            if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
                raise RuntimeError
        _check(checks, "private-directory-owner-and-access")

        descriptor, filename = tempfile.mkstemp(prefix="handoff-", dir=root)
        handoff = Path(filename)
        try:
            private_paths.secure_private_file(descriptor, handoff, 0o600)
            os.write(descriptor, b"synthetic ephemeral handoff\n")
            if os.name != "nt":
                os.fsync(descriptor)
            private_paths.verify_private_path(handoff, directory=False)
            if os.name == "nt":
                _verify_windows_acl(handoff, directory=False)
                _check(checks, "native-file-owner-dacl")
            else:
                metadata = handoff.lstat()
                if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
                    raise RuntimeError
            _check(checks, "private-file-owner-and-access")
        finally:
            os.close(descriptor)

        link = root / "link-probe"
        try:
            link.symlink_to(handoff)
        except (OSError, NotImplementedError):
            _check(checks, "symlink-rejection-skipped")
        else:
            try:
                private_paths.verify_private_path(link, directory=False)
            except private_paths.PrivatePathError:
                _check(checks, "symlink-rejection")
            else:
                raise RuntimeError

        if os.name == "nt":
            inherited = root / "inherited-probe"
            inherited.mkdir()
            try:
                private_paths.verify_private_path(inherited, directory=True)
            except private_paths.PrivatePathError:
                _check(checks, "inherited-acl-rejection")
            else:
                raise RuntimeError
            private_paths.protect_private_directory(inherited)
            _verify_windows_acl(inherited, directory=True)
            _check(checks, "protected-directory-recheck")

        return {"status": "passed", "platform": platform, "checks": checks}
    finally:
        temporary.cleanup()


def main() -> int:
    try:
        result = run_probe()
    except Exception:  # noqa: BLE001 - keep runtime diagnostics path- and identity-free.
        result = {"status": "failed", "platform": sys.platform, "checks": []}
        print(json.dumps(result, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
