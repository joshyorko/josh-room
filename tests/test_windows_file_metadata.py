from __future__ import annotations

import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from josh_room import windows_file_metadata as metadata


class _NativeAPIMock:
    def __init__(
        self,
        expected,
        *,
        change_time=123456,
        attributes=0,
        volume=None,
        file_id=None,
        open_error=None,
        close_error=None,
    ):
        self.expected = expected
        self.change_time_value = change_time
        self.attributes = attributes
        self.volume = expected.st_dev if volume is None else volume
        self.file_id = expected.st_ino if file_id is None else file_id
        self.open_error = open_error
        self.close_error = close_error
        self.handle = object()
        self.opened = None
        self.closed = []

    def open_file(self, path, flags):
        self.opened = (path, flags)
        if self.open_error is not None:
            raise self.open_error
        return self.handle

    def file_identity(self, handle):
        assert handle is self.handle
        return self.volume, self.file_id, self.attributes

    def change_time(self, handle):
        assert handle is self.handle
        return self.change_time_value, self.attributes

    def close(self, handle):
        self.closed.append(handle)
        if self.close_error is not None:
            raise self.close_error


def _expected(path: Path):
    info = path.lstat()
    return SimpleNamespace(
        st_mode=info.st_mode,
        st_dev=info.st_dev,
        st_ino=info.st_ino,
        st_file_attributes=0,
    )


def _use_api(monkeypatch, api):
    monkeypatch.setattr(metadata, "_is_windows", lambda: True)
    monkeypatch.setattr(metadata, "_windows_api", lambda: api)


def test_change_time_uses_native_ticks_and_closes_handle(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(b"synthetic")
    expected = _expected(path)
    api = _NativeAPIMock(expected, change_time=87654321)
    _use_api(monkeypatch, api)

    value = metadata.change_time_ns(path, expected)

    assert value == 87654321 * 100
    assert api.opened == (path, metadata._FILE_FLAG_OPEN_REPARSE_POINT)
    assert api.closed == [api.handle]


def test_file_identity_matches_128_bit_ids_and_64_bit_volume_serials(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(b"synthetic")
    expected = SimpleNamespace(
        st_mode=path.lstat().st_mode,
        st_dev=0x1_0000_0002,
        st_ino=(0xFEDCBA9876543210 << 64) | 0x123456789ABCDEF0,
        st_file_attributes=0,
    )
    api = _NativeAPIMock(
        expected,
        volume=expected.st_dev,
        file_id=expected.st_ino,
        change_time=987654,
    )
    _use_api(monkeypatch, api)

    assert metadata.change_time_ns(path, expected) == 987654 * 100
    assert api.closed == [api.handle]


def test_high_file_id_word_mismatch_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(b"synthetic")
    expected = SimpleNamespace(
        st_mode=path.lstat().st_mode,
        st_dev=0x1_0000_0002,
        st_ino=(0xFEDCBA9876543210 << 64) | 0x123456789ABCDEF0,
        st_file_attributes=0,
    )
    api = _NativeAPIMock(
        expected,
        volume=expected.st_dev,
        file_id=((0xFEDCBA9876543211) << 64) | 0x123456789ABCDEF0,
    )
    _use_api(monkeypatch, api)

    with pytest.raises(metadata.WindowsFileMetadataError) as failure:
        metadata.change_time_ns(path, expected)
    assert failure.value.code == "file-identity-changed"
    assert api.closed == [api.handle]


@pytest.mark.parametrize(
    ("options", "code"),
    [
        ({"file_id": 0}, "file-identity-changed"),
        ({"attributes": metadata._FILE_ATTRIBUTE_REPARSE_POINT}, "reparse-point"),
        ({"attributes": metadata._FILE_ATTRIBUTE_DIRECTORY}, "not-regular-file"),
        ({"change_time": 0}, "change-time-unavailable"),
    ],
)
def test_untrusted_metadata_fails_closed_and_closes_handle(tmp_path, monkeypatch, options, code):
    path = tmp_path / "data.bin"
    path.write_bytes(b"synthetic")
    expected = _expected(path)
    api = _NativeAPIMock(expected, **options)
    _use_api(monkeypatch, api)

    with pytest.raises(metadata.WindowsFileMetadataError) as failure:
        metadata.change_time_ns(path, expected)

    assert failure.value.code == code
    assert api.closed == [api.handle]


def test_file_replacement_between_stat_and_open_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(b"synthetic")
    expected = _expected(path)
    api = _NativeAPIMock(expected, volume=expected.st_dev + 1)
    _use_api(monkeypatch, api)

    with pytest.raises(metadata.WindowsFileMetadataError, match="identity changed"):
        metadata.change_time_ns(path, expected)
    assert api.closed == [api.handle]


def test_handle_close_failure_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(b"synthetic")
    expected = _expected(path)
    api = _NativeAPIMock(expected, close_error=OSError("native close failure"))
    _use_api(monkeypatch, api)

    with pytest.raises(metadata.WindowsFileMetadataError) as failure:
        metadata.change_time_ns(path, expected)
    assert failure.value.code == "handle-close-failed"
    assert api.closed == [api.handle]


def test_non_windows_path_never_loads_native_api(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(b"synthetic")
    monkeypatch.setattr(metadata, "_is_windows", lambda: False)
    monkeypatch.setattr(
        metadata,
        "_windows_api",
        lambda: pytest.fail("POSIX must not load the Windows API"),
    )

    with pytest.raises(metadata.WindowsFileMetadataError) as failure:
        metadata.change_time_ns(path, path.lstat())
    assert failure.value.code == "unsupported-platform"


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows ChangeTime semantics")
def test_same_size_edit_with_restored_mtime_changes_native_change_time(tmp_path):
    path = tmp_path / "data.bin"
    original = b"first synthetic value\n"
    changed = b"other synthetic value\n"
    assert len(original) == len(changed)
    path.write_bytes(original)
    before_stat = path.stat()
    before_change = metadata.change_time_ns(path, before_stat)

    path.write_bytes(changed)
    os.utime(path, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    after_stat = path.stat()
    after_change = metadata.change_time_ns(path, after_stat)

    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_change != before_change


class _FunctionMock:
    def __init__(self, function):
        self.function = function
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.function(*args)


def test_ctypes_wrapper_uses_handle_basic_info_and_closes(tmp_path, monkeypatch):
    path = tmp_path / "data.bin"
    path.write_bytes(b"synthetic")
    calls = []

    def create_file(path_value, access, share, security, creation, flags, template):
        calls.append(("open", path_value, access, share, creation, flags))
        return 91

    volume = 0x1_0000_0002
    file_id = (0xFEDCBA9876543210 << 64) | 0x123456789ABCDEF0

    def get_classic_attributes(handle, pointer):
        assert handle == 91
        value = ctypes.cast(
            pointer, ctypes.POINTER(metadata._BY_HANDLE_FILE_INFORMATION)
        ).contents
        value.dwFileAttributes = 0
        calls.append(("classic-attributes",))
        return 1

    def get_file_id(handle, info_class, pointer, size):
        assert handle == 91
        assert info_class == metadata._FILE_INFO_CLASS_ID
        assert size == ctypes.sizeof(metadata._FILE_ID_INFO_STRUCT)
        value = ctypes.cast(pointer, ctypes.POINTER(metadata._FILE_ID_INFO_STRUCT)).contents
        value.VolumeSerialNumber = volume
        value.FileId.Identifier[:] = file_id.to_bytes(16, byteorder="little")
        calls.append(("file-id", info_class))
        return 1

    def get_basic(handle, info_class, pointer, size):
        assert handle == 91
        assert info_class == metadata._FILE_INFO_CLASS_BASIC
        assert size == ctypes.sizeof(metadata._FILE_BASIC_INFO_STRUCT)
        value = ctypes.cast(pointer, ctypes.POINTER(metadata._FILE_BASIC_INFO_STRUCT)).contents
        value.ChangeTime = 7654321
        value.FileAttributes = 0
        calls.append(("basic", info_class))
        return 1

    system_time = 0x01DC000000123456

    def get_system_time(pointer):
        value = ctypes.cast(pointer, ctypes.POINTER(metadata._FILETIME)).contents
        value.dwLowDateTime = system_time & 0xFFFFFFFF
        value.dwHighDateTime = system_time >> 32
        calls.append(("system-time",))

    kernel = SimpleNamespace(
        CreateFileW=_FunctionMock(create_file),
        GetFileInformationByHandle=_FunctionMock(get_classic_attributes),
        GetFileInformationByHandleEx=_FunctionMock(get_basic),
        GetSystemTimeAsFileTime=_FunctionMock(get_system_time),
        CloseHandle=_FunctionMock(lambda handle: calls.append(("close", handle)) or 1),
    )
    native_file_id = _FunctionMock(get_file_id)
    original_get_ex = kernel.GetFileInformationByHandleEx

    def get_ex(handle, info_class, pointer, size):
        if info_class == metadata._FILE_INFO_CLASS_ID:
            return native_file_id(handle, info_class, pointer, size)
        return original_get_ex(handle, info_class, pointer, size)

    kernel.GetFileInformationByHandleEx = _FunctionMock(get_ex)
    monkeypatch.setattr(metadata.ctypes, "WinDLL", lambda *_args, **_kwargs: kernel, raising=False)
    api = metadata._WindowsFileAPI()
    handle = api.open_file(path, metadata._FILE_FLAG_OPEN_REPARSE_POINT)

    assert api.file_identity(handle) == (volume, file_id, 0)
    assert api.change_time(handle) == (7654321, 0)
    assert api.system_time_100ns() == system_time
    api.close(handle)
    assert calls[0][0] == "open"
    assert calls[0][-1] == metadata._FILE_FLAG_OPEN_REPARSE_POINT
    assert ("file-id", metadata._FILE_INFO_CLASS_ID) in calls
    assert ("basic", metadata._FILE_INFO_CLASS_BASIC) in calls
    assert ("system-time",) in calls
    assert calls[-1] == ("close", 91)
