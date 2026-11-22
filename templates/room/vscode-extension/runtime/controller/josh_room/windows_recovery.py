"""Conservative local-drive comparison with no-reparse, deny-delete handles."""

from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path

from .windows_file_metadata import _BY_HANDLE_FILE_INFORMATION, _WindowsFileAPI

_REPARSE = 0x400
_DIRECTORY = 0x10
_OPEN_REPARSE = 0x00200000
_BACKUP_SEMANTICS = 0x02000000
_READ_ATTRIBUTES = 0x80
_GENERIC_READ = 0x80000000
_SHARE_READ = 1
_SHARE_WRITE = 2


def current_fingerprint_windows(workspace, relative, *, include_text=False):
    """Hold every ancestor against rename and the file against write/delete.

    UNC/device paths, reparse points, hardlinks and unavailable native guarantees
    fail closed. No historical content is displayed by this comparison.
    """
    from .recovery import MAX_BYTES, fail
    from .room_store_operations import _relative_parts
    from .workspace_policy import load_capture_policy

    if os.name != "nt" or not workspace:
        fail("recovery-current-unsupported")
    root = Path(workspace).absolute()
    parts = _relative_parts(relative)
    if len(root.drive) != 2 or root.drive[1] != ":":
        fail("recovery-current-unsupported")
    api = _WindowsFileAPI()
    handles = []

    def open_locked(path, directory):
        # Ancestors may receive new files but cannot be renamed/deleted while
        # held. The final file additionally denies all writers, including ones
        # already open: Windows checks share compatibility in both directions.
        handle = api.kernel.CreateFileW(
            str(path),
            _READ_ATTRIBUTES if directory else _GENERIC_READ,
            _SHARE_READ | (_SHARE_WRITE if directory else 0),
            None,
            3,
            _OPEN_REPARSE | (_BACKUP_SEMANTICS if directory else 0),
            None,
        )
        if handle is None or handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                raise FileNotFoundError
            fail("recovery-current-unsafe")
        handles.append(handle)
        information = _BY_HANDLE_FILE_INFORMATION()
        if not api.kernel.GetFileInformationByHandle(handle, ctypes.byref(information)):
            fail("recovery-current-unsafe")
        attributes = information.dwFileAttributes
        if attributes & _REPARSE or bool(attributes & _DIRECTORY) != directory:
            fail("recovery-current-unsafe")
        if not directory and information.nNumberOfLinks != 1:
            fail("recovery-current-unsafe")
        return handle, information

    try:
        # No parent is resolved through a reparse point, and earlier ancestors
        # remain locked while each later component is opened by its stable path.
        for parent in (*reversed(root.parents), root):
            open_locked(parent, True)
        ignore = root / ".josh-roomignore"
        try:
            open_locked(ignore, False)
        except FileNotFoundError:
            pass
        if load_capture_policy(root).is_excluded(relative):
            fail("recovery-current-excluded")
        current = root
        for part in parts[:-1]:
            current = current / part
            try:
                open_locked(current, True)
            except FileNotFoundError:
                return None
        try:
            handle, information = open_locked(current / parts[-1], False)
        except FileNotFoundError:
            return None
        size = (information.nFileSizeHigh << 32) | information.nFileSizeLow
        if size > MAX_BYTES:
            fail("recovery-budget")
        api.kernel.ReadFile.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        api.kernel.ReadFile.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(65536)
        digest = hashlib.sha256()
        consumed = 0
        captured = bytearray()
        while True:
            count = ctypes.c_uint32()
            if not api.kernel.ReadFile(
                handle, buffer, len(buffer), ctypes.byref(count), None
            ):
                fail("recovery-current-unsafe")
            if not count.value:
                break
            consumed += count.value
            if consumed > size:
                fail("recovery-current-changed")
            chunk = buffer.raw[: count.value]
            digest.update(chunk)
            if include_text and size <= 2048:
                captured.extend(chunk)
        if consumed != size:
            fail("recovery-current-changed")
        result = {"type": "file", "size": consumed, "sha256": digest.hexdigest()}
        if include_text:
            from .recovery_text import text_preview
            result["text_preview"] = text_preview(relative, bytes(captured)) if size <= 2048 else {"state": "oversized"}
        return result
    except OSError:
        fail("recovery-current-unsafe")
    finally:
        for handle in reversed(handles):
            api.close(handle)
