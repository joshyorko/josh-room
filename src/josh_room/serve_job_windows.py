"""Bind a Windows foreground Serve command and its children to the CLI's lifetime."""

import ctypes
import sys
from ctypes import wintypes

_serve_job = None


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def ensure_serve_job() -> None:
    """Call in the one-command CLI before starting RCC. Fail closed on assignment failure."""
    global _serve_job
    if sys.platform != "win32" or _serve_job is not None:
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(_ExtendedLimitInformation), wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    # NULL security attributes make this unnamed job handle non-inheritable.
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise RuntimeError(f"Serve could not create its Windows Job Object (WinError {ctypes.get_last_error()})")
    limits = _ExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise RuntimeError(f"Serve could not configure kill-on-close (WinError {error})")
    if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        error = ctypes.get_last_error()
        kernel.CloseHandle(handle)
        raise RuntimeError(f"Serve could not join its Windows Job Object (WinError {error})")
    # Never close this in Python. The kernel closes it when this CLI exits,
    # including TerminateProcess. Closing a populated job here would kill us.
    _serve_job = handle
