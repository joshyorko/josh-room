"""Native Windows file metadata needed for conservative workspace scans."""

from __future__ import annotations

import ctypes
import errno as errno_module
import os
import stat
from pathlib import Path

_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_FILE_INFO_CLASS_BASIC = 0
_FILE_INFO_CLASS_ID = 18


class WindowsFileMetadataError(RuntimeError):
    """Path-free failure reading a trustworthy native Windows timestamp."""

    def __init__(
        self,
        code: str,
        *,
        error_number: int | None = None,
        winerror: int | None = None,
    ) -> None:
        self.code = code
        self.errno = error_number
        self.winerror = winerror
        super().__init__(code.replace("-", " "))


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", _FILETIME),
        ("ftLastAccessTime", _FILETIME),
        ("ftLastWriteTime", _FILETIME),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


class _FILE_ID_128_STRUCT(ctypes.Structure):
    _fields_ = [("Identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO_STRUCT(ctypes.Structure):
    _fields_ = [
        ("VolumeSerialNumber", ctypes.c_uint64),
        ("FileId", _FILE_ID_128_STRUCT),
    ]


class _FILE_BASIC_INFO_STRUCT(ctypes.Structure):
    _fields_ = [
        ("CreationTime", ctypes.c_int64),
        ("LastAccessTime", ctypes.c_int64),
        ("LastWriteTime", ctypes.c_int64),
        ("ChangeTime", ctypes.c_int64),
        ("FileAttributes", ctypes.c_uint32),
    ]


def _is_windows() -> bool:
    return os.name == "nt"


def _failure(code: str, error: OSError | None = None) -> None:
    raise WindowsFileMetadataError(
        code,
        error_number=error.errno if error is not None else None,
        winerror=getattr(error, "winerror", None) if error is not None else None,
    ) from None


class _WindowsFileAPI:
    """ctypes wrapper around handle-based metadata calls."""

    def __init__(self) -> None:
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise OSError(errno_module.ENOSYS, "Windows file metadata API unavailable")
        self.kernel = loader("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        self.kernel.CreateFileW.restype = ctypes.c_void_p
        self.kernel.GetFileInformationByHandle.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
        ]
        self.kernel.GetFileInformationByHandle.restype = ctypes.c_int
        self.kernel.GetFileInformationByHandleEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        self.kernel.GetFileInformationByHandleEx.restype = ctypes.c_int
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.CloseHandle.restype = ctypes.c_int

    @staticmethod
    def _last_error(code: str) -> OSError:
        error = ctypes.get_last_error()
        failure = OSError(error or errno_module.EIO, code)
        if error:
            failure.winerror = int(error)
        return failure

    def open_file(self, path: Path, flags: int) -> ctypes.c_void_p:
        handle = self.kernel.CreateFileW(
            str(path),
            _FILE_READ_ATTRIBUTES,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            flags,
            None,
        )
        if handle == ctypes.c_void_p(-1).value or handle is None:
            raise self._last_error("open-failed")
        return handle

    def file_identity(self, handle: ctypes.c_void_p) -> tuple[int, int, int]:
        identity = _FILE_ID_INFO_STRUCT()
        if not self.kernel.GetFileInformationByHandleEx(
            handle,
            _FILE_INFO_CLASS_ID,
            ctypes.byref(identity),
            ctypes.sizeof(identity),
        ):
            raise self._last_error("identity-failed")
        attributes = _BY_HANDLE_FILE_INFORMATION()
        if not self.kernel.GetFileInformationByHandle(handle, ctypes.byref(attributes)):
            raise self._last_error("identity-failed")
        file_id = int.from_bytes(bytes(identity.FileId.Identifier), byteorder="little")
        return (
            int(identity.VolumeSerialNumber),
            file_id,
            int(attributes.dwFileAttributes),
        )

    def change_time(self, handle: ctypes.c_void_p) -> tuple[int, int]:
        information = _FILE_BASIC_INFO_STRUCT()
        if not self.kernel.GetFileInformationByHandleEx(
            handle,
            _FILE_INFO_CLASS_BASIC,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise self._last_error("change-time-failed")
        return int(information.ChangeTime), int(information.FileAttributes)

    def close(self, handle: ctypes.c_void_p) -> None:
        if not self.kernel.CloseHandle(handle):
            raise self._last_error("close-failed")


def _windows_api() -> _WindowsFileAPI:
    return _WindowsFileAPI()


def change_time_ns(path: Path, expected_stat: os.stat_result) -> int:
    """Return native FileBasicInfo.ChangeTime normalized to integer nanoseconds.

    `expected_stat` must be the no-follow stat result observed by the caller.
    The native volume and file ID are compared while a reparse-point-safe file
    handle is held, preventing a path replacement from supplying another file's
    metadata. Windows timestamps are native 100 ns ticks, scaled to nanoseconds,
    not Python ctime.
    """
    if not _is_windows():
        _failure("unsupported-platform")
    path = Path(path)
    try:
        expected_mode = expected_stat.st_mode
    except (AttributeError, TypeError):
        _failure("file-identity-unavailable")
    if not stat.S_ISREG(expected_mode):
        _failure("not-regular-file")
    if (
        getattr(expected_stat, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        _failure("reparse-point")
    try:
        nofollow = path.lstat()
    except OSError as error:
        _failure("path-unavailable", error)
    if stat.S_ISLNK(nofollow.st_mode) or (
        getattr(nofollow, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    ):
        _failure("reparse-point")
    if not stat.S_ISREG(nofollow.st_mode):
        _failure("not-regular-file")
    try:
        api = _windows_api()
        handle = api.open_file(path, _FILE_FLAG_OPEN_REPARSE_POINT)
    except OSError as error:
        _failure("open-failed", error)
    except Exception:  # noqa: BLE001 - hide native API diagnostics and paths.
        _failure("api-unavailable")

    try:
        volume, file_id, attributes = api.file_identity(handle)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            _failure("reparse-point")
        if attributes & _FILE_ATTRIBUTE_DIRECTORY:
            _failure("not-regular-file")
        expected_volume = getattr(expected_stat, "st_dev", None)
        expected_file_id = getattr(expected_stat, "st_ino", None)
        if (
            not isinstance(expected_volume, int)
            or not isinstance(expected_file_id, int)
            or expected_volume < 0
            or expected_file_id <= 0
        ):
            _failure("file-identity-unavailable")
        if volume != expected_volume or file_id != expected_file_id:
            _failure("file-identity-changed")
        change_time, basic_attributes = api.change_time(handle)
        if basic_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            _failure("reparse-point")
        if basic_attributes & _FILE_ATTRIBUTE_DIRECTORY:
            _failure("not-regular-file")
        if not isinstance(change_time, int) or change_time <= 0:
            _failure("change-time-unavailable")
        return change_time * 100
    except WindowsFileMetadataError:
        raise
    except OSError as error:
        _failure("metadata-query-failed", error)
    except Exception:  # noqa: BLE001 - hide native API diagnostics and paths.
        _failure("metadata-query-failed")
    finally:
        try:
            api.close(handle)
        except OSError as error:
            _failure("handle-close-failed", error)
        except Exception:  # noqa: BLE001 - sanitize native close diagnostics.
            _failure("handle-close-failed")
