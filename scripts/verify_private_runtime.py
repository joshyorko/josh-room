"""Exercise private-path protection in the active installed Python runtime."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from josh_room import private_paths
from josh_room.windows_file_metadata import WindowsFileMetadataError, change_time_ns


class _ProbeFailure(Exception):
    def __init__(
        self,
        failed_check: str,
        error_type: str,
        winerror: int | None,
        checks_completed: list[str],
    ) -> None:
        self.failed_check = failed_check
        self.error_type = error_type
        self.winerror = winerror
        self.checks_completed = checks_completed
        super().__init__(failed_check)


def _check(checks: list[str], name: str) -> None:
    checks.append(name)


def _changetime_edit_predicates(
    *,
    before_size: int,
    after_size: int,
    expected_size: int,
    before_mtime_ns: int,
    after_mtime_ns: int,
    before_change: int,
    after_change: int,
) -> tuple[bool, bool, bool]:
    return (
        before_size == after_size == expected_size,
        before_mtime_ns // 100 == after_mtime_ns // 100,
        before_change != after_change,
    )


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
    failed_check = "create-private-runtime-root"
    try:
        failed_check = "protect-private-runtime-root"
        private_paths.protect_private_directory(root)
        if os.name == "nt":
            failed_check = "native-directory-owner-dacl-inheritance"
            _verify_windows_acl(root, directory=True)
            _check(checks, "native-directory-owner-dacl-inheritance")
        else:
            failed_check = "posix-directory-owner-mode"
            metadata = root.lstat()
            if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.getuid():
                raise RuntimeError
        _check(checks, "private-directory-owner-and-access")

        failed_check = "create-private-file-handoff"
        descriptor, filename = tempfile.mkstemp(prefix="handoff-", dir=root)
        handoff = Path(filename)
        file_handoff_in_progress = True
        try:
            failed_check = "protect-private-file-handoff"
            private_paths.secure_private_file(descriptor, handoff, 0o600)
            failed_check = "write-private-file-handoff"
            os.write(descriptor, b"synthetic ephemeral handoff\n")
            if os.name != "nt":
                os.fsync(descriptor)
            failed_check = "verify-private-file-handoff"
            private_paths.verify_private_path(handoff, directory=False)
            if os.name == "nt":
                failed_check = "native-file-owner-dacl"
                _verify_windows_acl(handoff, directory=False)
                _check(checks, "native-file-owner-dacl")
            else:
                metadata = handoff.lstat()
                if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.getuid():
                    raise RuntimeError
            _check(checks, "private-file-owner-and-access")
            if os.name == "nt":
                failed_check = "native-changetime-before-edit"
                before_stat = handoff.stat()
                before_change = change_time_ns(handoff, before_stat)
                failed_check = "same-size-edit-and-mtime-restore"
                handoff.write_bytes(b"different ephemeral handoff\n")
                os.utime(
                    handoff,
                    ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns),
                )
                failed_check = "native-changetime-after-edit"
                after_stat = handoff.stat()
                after_change = change_time_ns(handoff, after_stat)
                same_size, mtime_restored_100ns, change_time_changed = (
                    _changetime_edit_predicates(
                        before_size=before_stat.st_size,
                        after_size=after_stat.st_size,
                        expected_size=len(b"different ephemeral handoff\n"),
                        before_mtime_ns=before_stat.st_mtime_ns,
                        after_mtime_ns=after_stat.st_mtime_ns,
                        before_change=before_change,
                        after_change=after_change,
                    )
                )
                failed_check = (
                    "native-changetime-edit-detection"
                    f":same_size={int(same_size)}"
                    f":mtime_restored_100ns={int(mtime_restored_100ns)}"
                    f":change_time_changed={int(change_time_changed)}"
                )
                if not (same_size and mtime_restored_100ns and change_time_changed):
                    raise RuntimeError
                _check(checks, "same-size-edit-restored-mtime-changes-native-changetime")
            file_handoff_in_progress = False
        finally:
            if not file_handoff_in_progress:
                failed_check = "close-private-file-handoff"
            os.close(descriptor)

        failed_check = "create-symlink-probe"
        link = root / "link-probe"
        try:
            link.symlink_to(handoff)
        except (OSError, NotImplementedError):
            _check(checks, "symlink-rejection-skipped")
        else:
            failed_check = "reject-symlink-probe"
            try:
                private_paths.verify_private_path(link, directory=False)
            except private_paths.PrivatePathError:
                _check(checks, "symlink-rejection")
            else:
                raise RuntimeError

        if os.name == "nt":
            failed_check = "create-inherited-acl-probe"
            inherited = root / "inherited-probe"
            inherited.mkdir()
            failed_check = "reject-inherited-acl"
            try:
                private_paths.verify_private_path(inherited, directory=True)
            except private_paths.PrivatePathError:
                _check(checks, "inherited-acl-rejection")
            else:
                raise RuntimeError
            failed_check = "protect-inherited-acl-probe"
            private_paths.protect_private_directory(inherited)
            failed_check = "verify-protected-inherited-acl-probe"
            _verify_windows_acl(inherited, directory=True)
            _check(checks, "protected-directory-recheck")

        result = {"status": "passed", "platform": platform, "checks": checks}
    except Exception as error:  # noqa: BLE001 - only bounded metadata escapes.
        winerror = getattr(error, "winerror", None)
        if isinstance(winerror, bool) or not isinstance(winerror, int):
            winerror = None
        detail = (
            error.code
            if isinstance(error, (private_paths.PrivatePathError, WindowsFileMetadataError))
            else None
        )
        diagnostic_check = f"{failed_check}:{detail}" if detail else failed_check
        failure = _ProbeFailure(
            diagnostic_check,
            type(error).__name__,
            winerror,
            checks.copy(),
        )
        try:
            temporary.cleanup()
        except Exception:  # noqa: BLE001, S110 - preserve the original bounded failure.
            pass
        raise failure from None
    try:
        temporary.cleanup()
    except Exception as error:  # noqa: BLE001 - cleanup failure is still path-free.
        winerror = getattr(error, "winerror", None)
        if isinstance(winerror, bool) or not isinstance(winerror, int):
            winerror = None
        raise _ProbeFailure(
            "cleanup-private-runtime-root",
            type(error).__name__,
            winerror,
            checks.copy(),
        ) from None
    return result


def main() -> int:
    try:
        result = run_probe()
    except _ProbeFailure as error:
        result = {
            "status": "failed",
            "platform": sys.platform,
            "failed_check": error.failed_check,
            "error_type": error.error_type,
            "winerror": error.winerror,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "checks_completed": error.checks_completed,
        }
        print(json.dumps(result, separators=(",", ":")))
        return 1
    except Exception as error:  # noqa: BLE001 - keep diagnostics path- and identity-free.
        result = {
            "status": "failed",
            "platform": sys.platform,
            "failed_check": "probe-setup-or-cleanup",
            "error_type": type(error).__name__,
            "winerror": None,
            "python_version": ".".join(map(str, sys.version_info[:3])),
            "checks_completed": [],
        }
        print(json.dumps(result, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
