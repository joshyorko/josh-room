from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import private_paths

_USER_SID = "S-1-5-21-1000-2000-3000-1001"
_SYSTEM_SID = "S-1-5-18"


class _WindowsAPI:
    def __init__(
        self, *, directory: bool, owner: str = _USER_SID, aces=None, protected=True
    ):
        self.directory = directory
        self.owner = owner
        self.protected = protected
        self.aces = list(
            aces
            if aces is not None
            else private_paths._expected_aces(_USER_SID, directory=directory)
        )
        self.applied = []

    def current_user_sid(self):
        return _USER_SID

    def apply_private_acl(self, path, owner_sid, *, directory):
        self.applied.append((path, owner_sid, directory))
        self.owner = owner_sid
        self.directory = directory
        self.aces = list(private_paths._expected_aces(owner_sid, directory=directory))
        self.protected = True

    def read_security(self, _path):
        return self.owner, self.protected, tuple(self.aces)


def _windows(monkeypatch, api):
    monkeypatch.setattr(private_paths, "_is_windows", lambda: True)
    monkeypatch.setattr(private_paths, "_windows_api", lambda: api)


def test_posix_directory_and_file_are_protected_and_verified(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o755)
    file = directory / "password"
    file.write_text("synthetic", encoding="utf-8")
    file.chmod(0o644)

    private_paths.protect_private_directory(directory)
    private_paths.protect_private_file(file)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    private_paths.verify_private_path(directory, directory=True)
    private_paths.verify_private_path(file, directory=False)


def test_posix_verification_rejects_wrong_owner_permissions_and_symlinks(
    tmp_path, monkeypatch
):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    file = directory / "data"
    file.write_text("x", encoding="utf-8")
    file.chmod(0o600)

    monkeypatch.setattr(private_paths.os, "getuid", lambda: file.stat().st_uid + 1)
    with pytest.raises(private_paths.PrivatePathError, match="owner"):
        private_paths.verify_private_path(file, directory=False)
    monkeypatch.undo()
    file.chmod(0o644)
    with pytest.raises(private_paths.PrivatePathError, match="permissions"):
        private_paths.verify_private_path(file, directory=False)

    link = tmp_path / "link"
    link.symlink_to(file)
    with pytest.raises(private_paths.PrivatePathError, match="link"):
        private_paths.protect_private_file(link)


def test_windows_protection_applies_protected_user_and_system_acl(
    tmp_path, monkeypatch
):
    directory = tmp_path / "private"
    directory.mkdir()
    file = directory / "password"
    file.write_text("synthetic", encoding="utf-8")
    directory_api = _WindowsAPI(directory=True)
    file_api = _WindowsAPI(directory=False)

    _windows(monkeypatch, directory_api)
    private_paths.protect_private_directory(directory)
    _windows(monkeypatch, file_api)
    private_paths.protect_private_file(file)

    assert directory_api.applied == [(directory, _USER_SID, True)]
    assert file_api.applied == [(file, _USER_SID, False)]
    assert set(directory_api.aces) == {
        private_paths._Ace(_USER_SID, private_paths._FILE_ALL_ACCESS, 0x03),
        private_paths._Ace(_SYSTEM_SID, private_paths._FILE_ALL_ACCESS, 0x03),
    }
    assert set(file_api.aces) == {
        private_paths._Ace(_USER_SID, private_paths._FILE_ALL_ACCESS, 0),
        private_paths._Ace(_SYSTEM_SID, private_paths._FILE_ALL_ACCESS, 0),
    }


def test_windows_private_sddl_keeps_empty_file_ace_flags_field():
    assert private_paths._private_sddl(_USER_SID, directory=False) == (
        f"D:P(A;;FA;;;{_USER_SID})(A;;FA;;;SY)"
    )
    assert private_paths._private_sddl(_USER_SID, directory=True) == (
        f"D:P(A;OICI;FA;;;{_USER_SID})(A;OICI;FA;;;SY)"
    )


@pytest.mark.parametrize(
    ("owner", "aces", "protected"),
    [
        ("S-1-5-21-9", None, True),
        (_USER_SID, None, False),
        (
            _USER_SID,
            [private_paths._Ace("S-1-1-0", private_paths._FILE_ALL_ACCESS, 0)]
            + list(private_paths._expected_aces(_USER_SID, directory=False))[1:],
            True,
        ),
        (
            _USER_SID,
            list(private_paths._expected_aces(_USER_SID, directory=False))
            + [private_paths._Ace("S-1-5-32-545", private_paths._FILE_ALL_ACCESS, 0)],
            True,
        ),
        (
            _USER_SID,
            [
                private_paths._Ace(_USER_SID, private_paths._FILE_ALL_ACCESS, 0x10),
                private_paths._Ace(_SYSTEM_SID, private_paths._FILE_ALL_ACCESS, 0),
            ],
            True,
        ),
    ],
)
def test_windows_verification_rejects_wrong_owner_unprotected_or_extra_grants(
    tmp_path, monkeypatch, owner, aces, protected
):
    file = tmp_path / "private-file"
    file.write_text("synthetic", encoding="utf-8")
    api = _WindowsAPI(directory=False, owner=owner, aces=aces, protected=protected)
    _windows(monkeypatch, api)

    with pytest.raises(private_paths.PrivatePathError):
        private_paths.verify_private_path(file, directory=False)


def test_windows_verification_rejects_reparse_point_before_security_api(
    tmp_path, monkeypatch
):
    file = tmp_path / "private-file"
    file.write_text("synthetic", encoding="utf-8")
    api = _WindowsAPI(directory=False)
    _windows(monkeypatch, api)
    real_lstat = Path.lstat

    def fake_lstat(path):
        info = real_lstat(path)
        if path == file:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=private_paths._FILE_ATTRIBUTE_REPARSE_POINT,
            )
        return info

    monkeypatch.setattr(
        Path,
        "lstat",
        fake_lstat,
    )

    with pytest.raises(private_paths.PrivatePathError, match="reparse"):
        private_paths.verify_private_path(file, directory=False)
    assert api.applied == []


class _NativeFunction:
    argtypes = None
    restype = None


class _NativeLibrary:
    def __init__(self):
        self.functions = {}

    def __getattr__(self, name):
        function = self.functions.setdefault(name, _NativeFunction())
        return function


def test_windows_security_control_prototype_uses_dword_revision(monkeypatch):
    libraries = {}

    def load_library(name, **_kwargs):
        return libraries.setdefault(name, _NativeLibrary())

    monkeypatch.setattr(private_paths.ctypes, "WinDLL", load_library, raising=False)
    api = private_paths._WindowsSecurityAPI()

    prototype = api.advapi.GetSecurityDescriptorControl.argtypes
    assert prototype[1] == private_paths.ctypes.POINTER(private_paths.ctypes.c_uint16)
    assert prototype[2] == private_paths.ctypes.POINTER(private_paths.ctypes.c_uint32)
