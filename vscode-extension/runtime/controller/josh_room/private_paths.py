"""Small, fail-closed helpers for private Room Store handoff paths."""

from __future__ import annotations

import ctypes
import errno as errno_module
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PrivatePathError(RuntimeError):
    """Path-free private-path failure with an optional stable errno."""

    def __init__(
        self,
        message: str,
        *,
        error_number: int | None = None,
        winerror: int | None = None,
    ) -> None:
        self.code = message.replace(" ", "_")
        self.errno = error_number
        self.winerror = winerror
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _Ace:
    sid: str
    mask: int
    flags: int


_FILE_ALL_ACCESS = 0x001F01FF
_DIRECTORY_INHERITANCE = 0x03  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_SECURITY_INFORMATION_OWNER = 0x00000001
_SECURITY_INFORMATION_DACL = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_FILE_OBJECT = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_ACCESS_ALLOWED_ACE_TYPE = 0
_INHERITED_ACE = 0x10


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_api_error(code: int, message: str) -> OSError:
    error = OSError(code or errno_module.EIO, message)
    if code:
        error.winerror = int(code)
    return error


def _raise_path_error(message: str, error: OSError | None = None) -> None:
    raise PrivatePathError(
        message,
        error_number=error.errno if error else None,
        winerror=getattr(error, "winerror", None) if error else None,
    ) from None


def _metadata(path: Path, *, directory: bool | None) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as error:
        _raise_path_error("private path is unavailable", error)
    if stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        _raise_path_error("private path is a link or reparse point")
    is_directory = stat.S_ISDIR(info.st_mode)
    if directory is True and not is_directory:
        _raise_path_error("private directory is invalid")
    if directory is False and not stat.S_ISREG(info.st_mode):
        _raise_path_error("private file is invalid")
    if directory is None and not (is_directory or stat.S_ISREG(info.st_mode)):
        _raise_path_error("private path type is invalid")
    return info


class _WindowsSecurityAPI:
    """Thin ctypes wrapper; higher-level ACL policy stays testable off Windows."""

    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError(errno_module.ENOSYS, "Windows security API unavailable")
        self.advapi = loader("advapi32", use_last_error=True)
        self.kernel = loader("kernel32", use_last_error=True)
        self.advapi.ConvertStringSidToSidW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi.ConvertStringSidToSidW.restype = ctypes.c_int
        self.advapi.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        self.advapi.ConvertSidToStringSidW.restype = ctypes.c_int
        self.advapi.OpenProcessToken.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi.OpenProcessToken.restype = ctypes.c_int
        self.advapi.GetTokenInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.advapi.GetTokenInformation.restype = ctypes.c_int
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
            ctypes.c_int
        )
        self.advapi.GetSecurityDescriptorDacl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_int),
        ]
        self.advapi.GetSecurityDescriptorDacl.restype = ctypes.c_int
        self.advapi.SetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.advapi.SetNamedSecurityInfoW.restype = ctypes.c_uint32
        self.advapi.GetNamedSecurityInfoW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi.GetNamedSecurityInfoW.restype = ctypes.c_uint32
        self.advapi.GetSecurityDescriptorControl.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self.advapi.GetSecurityDescriptorControl.restype = ctypes.c_int
        self.advapi.GetAclInformation.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        self.advapi.GetAclInformation.restype = ctypes.c_int
        self.advapi.GetAce.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.advapi.GetAce.restype = ctypes.c_int
        self.kernel.GetCurrentProcess.restype = ctypes.c_void_p
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int

    @staticmethod
    def _checked(result: Any) -> Any:
        if not result:
            code = ctypes.get_last_error()
            raise _windows_api_error(code, "Windows security operation failed")
        return result

    def _string_to_sid(self, sid_text: str) -> ctypes.c_void_p:
        pointer = ctypes.c_void_p()
        self._checked(
            self.advapi.ConvertStringSidToSidW(sid_text, ctypes.byref(pointer))
        )
        return pointer

    def _sid_to_string(self, sid: ctypes.c_void_p) -> str:
        pointer = ctypes.c_wchar_p()
        self._checked(self.advapi.ConvertSidToStringSidW(sid, ctypes.byref(pointer)))
        try:
            return pointer.value or ""
        finally:
            self.kernel.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))

    def current_user_sid(self) -> str:
        process = self.kernel.GetCurrentProcess()
        token = ctypes.c_void_p()
        self._checked(
            self.advapi.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token))
        )
        try:
            required = ctypes.c_uint32()
            self.advapi.GetTokenInformation(
                token, _TOKEN_USER, None, 0, ctypes.byref(required)
            )
            if (
                ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER
                or not required.value
            ):
                raise OSError(errno_module.EACCES, "Windows security operation failed")
            buffer = ctypes.create_string_buffer(required.value)
            self._checked(
                self.advapi.GetTokenInformation(
                    token, _TOKEN_USER, buffer, required.value, ctypes.byref(required)
                )
            )

            class TOKEN_USER(ctypes.Structure):
                _fields_ = [("User", ctypes.c_void_p), ("Attributes", ctypes.c_uint32)]

            user = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents
            return self._sid_to_string(ctypes.c_void_p(user.User))
        finally:
            self.kernel.CloseHandle(token)

    def apply_private_acl(self, path: Path, owner_sid: str, *, directory: bool) -> None:
        flags = ";OICI" if directory else ""
        sddl = f"D:P(A{flags};FA;;;{owner_sid})(A{flags};FA;;;SY)"
        descriptor = ctypes.c_void_p()
        self._checked(
            self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None
            )
        )
        owner = self._string_to_sid(owner_sid)
        dacl = ctypes.c_void_p()
        present = ctypes.c_int()
        defaulted = ctypes.c_int()
        try:
            self._checked(
                self.advapi.GetSecurityDescriptorDacl(
                    descriptor,
                    ctypes.byref(present),
                    ctypes.byref(dacl),
                    ctypes.byref(defaulted),
                )
            )
            if not present.value or not dacl.value:
                raise OSError(errno_module.EACCES, "Windows security operation failed")
            result = self.advapi.SetNamedSecurityInfoW(
                str(path),
                _SE_FILE_OBJECT,
                _SECURITY_INFORMATION_OWNER
                | _SECURITY_INFORMATION_DACL
                | _PROTECTED_DACL_SECURITY_INFORMATION,
                owner,
                None,
                dacl,
                None,
            )
            if result:
                raise _windows_api_error(int(result), "Windows security operation failed")
        finally:
            self.kernel.LocalFree(owner)
            self.kernel.LocalFree(descriptor)

    def read_security(self, path: Path) -> tuple[str, bool, tuple[_Ace, ...]]:
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        result = self.advapi.GetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _SECURITY_INFORMATION_OWNER | _SECURITY_INFORMATION_DACL,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        )
        if result:
            raise _windows_api_error(int(result), "Windows security operation failed")
        try:
            protected = ctypes.c_uint16()
            revision = ctypes.c_uint32()
            if not self.advapi.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(protected), ctypes.byref(revision)
            ):
                self._checked(False)
            # SE_DACL_PROTECTED is 0x1000 in SECURITY_DESCRIPTOR_CONTROL.
            dacl_protected = bool(protected.value & 0x1000)
            if not dacl.value:
                return self._sid_to_string(owner), dacl_protected, ()

            class ACL_SIZE_INFORMATION(ctypes.Structure):
                _fields_ = [
                    ("AceCount", ctypes.c_uint32),
                    ("AclBytesInUse", ctypes.c_uint32),
                    ("AclBytesFree", ctypes.c_uint32),
                ]

            size = ACL_SIZE_INFORMATION()
            if not self.advapi.GetAclInformation(
                dacl, ctypes.byref(size), ctypes.sizeof(size), 2
            ):
                self._checked(False)
            aces: list[_Ace] = []
            for index in range(size.AceCount):
                ace_pointer = ctypes.c_void_p()
                if not self.advapi.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                    self._checked(False)

                class ACE_HEADER(ctypes.Structure):
                    _fields_ = [
                        ("AceType", ctypes.c_ubyte),
                        ("AceFlags", ctypes.c_ubyte),
                        ("AceSize", ctypes.c_uint16),
                    ]

                header = ctypes.cast(ace_pointer, ctypes.POINTER(ACE_HEADER)).contents
                if header.AceType != _ACCESS_ALLOWED_ACE_TYPE or header.AceSize < 16:
                    aces.append(_Ace("", -1, int(header.AceFlags)))
                    continue
                mask = ctypes.cast(
                    ctypes.c_void_p(ace_pointer.value + 4),
                    ctypes.POINTER(ctypes.c_uint32),
                ).contents.value
                sid_pointer = ctypes.c_void_p(ace_pointer.value + 8)
                aces.append(
                    _Ace(
                        self._sid_to_string(sid_pointer),
                        int(mask),
                        int(header.AceFlags),
                    )
                )
            return self._sid_to_string(owner), dacl_protected, tuple(aces)
        finally:
            self.kernel.LocalFree(descriptor)


def _windows_api() -> _WindowsSecurityAPI:
    return _WindowsSecurityAPI()


def _expected_aces(user_sid: str, *, directory: bool) -> tuple[_Ace, _Ace]:
    flags = _DIRECTORY_INHERITANCE if directory else 0
    return (
        _Ace(user_sid, _FILE_ALL_ACCESS, flags),
        _Ace("S-1-5-18", _FILE_ALL_ACCESS, flags),
    )


def _verify_windows(path: Path, *, directory: bool) -> None:
    try:
        api = _windows_api()
        user_sid = api.current_user_sid()
        owner_sid, dacl_protected, aces = api.read_security(path)
        if (
            owner_sid != user_sid
            or not dacl_protected
            or len(aces) != 2
            or any(ace.flags & _INHERITED_ACE for ace in aces)
            or set(aces) != set(_expected_aces(user_sid, directory=directory))
        ):
            _raise_path_error("private path security is unsafe")
    except PrivatePathError:
        raise
    except OSError as error:
        _raise_path_error("private path security could not be verified", error)
    except Exception:  # noqa: BLE001 - native API errors are sanitized here.
        _raise_path_error("private path security could not be verified")


def verify_private_path(path: Path, *, directory: bool | None = None) -> None:
    """Verify private ownership and ACL/mode without following the final path."""
    path = Path(path)
    info = _metadata(path, directory=directory)
    is_directory = stat.S_ISDIR(info.st_mode)
    if _is_windows():
        _verify_windows(path, directory=is_directory)
        return
    try:
        if info.st_uid != os.getuid():
            _raise_path_error("private path owner is unsafe")
        mode = stat.S_IMODE(info.st_mode)
        if mode != (0o700 if is_directory else 0o600):
            _raise_path_error("private path permissions are unsafe")
    except PrivatePathError:
        raise
    except OSError as error:
        _raise_path_error("private path could not be verified", error)


def _protect(path: Path, *, directory: bool) -> None:
    path = Path(path)
    _metadata(path, directory=directory)
    if _is_windows():
        try:
            api = _windows_api()
            user_sid = api.current_user_sid()
            api.apply_private_acl(path, user_sid, directory=directory)
        except OSError as error:
            _raise_path_error("private path security could not be applied", error)
        except Exception:  # noqa: BLE001 - native API errors are sanitized here.
            _raise_path_error("private path security could not be applied")
    else:
        try:
            os.chmod(path, 0o700 if directory else 0o600, follow_symlinks=False)
        except OSError as error:
            _raise_path_error("private path permissions could not be applied", error)
    verify_private_path(path, directory=directory)


def protect_private_directory(path: Path) -> None:
    """Apply and verify owner-only directory access for an existing directory."""
    _protect(path, directory=True)


def protect_private_file(path: Path) -> None:
    """Apply and verify owner-only file access for an existing regular file."""
    _protect(path, directory=False)


def secure_private_file(descriptor: int, path: Path, mode: int = 0o600) -> None:
    """RoomStoreOperations callback for securing its newly-created temp file."""
    if mode != 0o600:
        _raise_path_error("private file mode is unsupported")
    try:
        info = os.fstat(descriptor)
    except OSError as error:
        _raise_path_error("private file descriptor is unavailable", error)
    if not stat.S_ISREG(info.st_mode):
        _raise_path_error("private file descriptor is invalid")
    if _is_windows():
        _protect(Path(path), directory=False)
    else:
        try:
            os.fchmod(descriptor, 0o600)
            current = os.stat(path, follow_symlinks=False)
        except OSError as error:
            _raise_path_error("private file could not be protected", error)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            _raise_path_error("private file identity changed")
        verify_private_path(Path(path), directory=False)


def validate_private_directory(path: Path) -> None:
    """RoomStoreOperations callback for preparing its caller-owned private root."""
    protect_private_directory(path)
