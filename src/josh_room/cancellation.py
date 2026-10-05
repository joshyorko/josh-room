"""Scoped CLI cancellation and owned subprocess cleanup."""

import contextlib
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass


class CLICancelled(Exception):
    """Raised inside the CLI when its foreground operation receives SIGTERM."""

    def __init__(self, result=None):
        self.result = result if isinstance(result, dict) else {}
        super().__init__("operation cancelled")


@contextlib.contextmanager
def sigterm_cancellation():
    """Turn SIGTERM into an unwind on the main thread, restoring its handler."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGTERM"):
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def cancel(_signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise CLICancelled

    signal.signal(signal.SIGTERM, cancel)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextlib.contextmanager
def defer_sigterm_cancellation():
    """Defer SIGTERM while an externally visible commit outcome is assigned."""
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGTERM"):
        yield
        return
    pthread_sigmask = getattr(signal, "pthread_sigmask", None)
    if pthread_sigmask is not None:
        previous_mask = pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            yield
        finally:
            pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        return

    previous = signal.getsignal(signal.SIGTERM)
    received = False

    def defer(_signum, _frame):
        nonlocal received
        received = True

    signal.signal(signal.SIGTERM, defer)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
        if received:
            raise CLICancelled


@dataclass
class _OwnedProcess:
    pid: int
    start_time: int
    pidfd: int | None


def _proc_start_time(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as source:
            raw = source.read()
        fields = raw[raw.rfind(")") + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _task_children(pid: int) -> list[int]:
    children = []
    try:
        tasks = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return children
    for task in tasks:
        try:
            with open(f"/proc/{pid}/task/{task}/children", encoding="ascii") as source:
                raw = source.read()
            children.extend(int(value) for value in raw.split())
        except (OSError, ValueError):
            continue
    return children


def _snapshot_descendants(root_pid: int) -> list[_OwnedProcess]:
    if os.name != "posix" or not os.path.isdir("/proc"):
        return []
    discovered = []
    seen = {root_pid}
    pending = [root_pid]
    pidfd_open = getattr(os, "pidfd_open", None)
    while pending:
        parent = pending.pop()
        for child in _task_children(parent):
            if child in seen:
                continue
            seen.add(child)
            start_time = _proc_start_time(child)
            if start_time is None:
                continue
            pidfd = None
            if pidfd_open is not None:
                try:
                    pidfd = pidfd_open(child)
                    if _proc_start_time(child) != start_time:
                        os.close(pidfd)
                        continue
                except OSError:
                    pidfd = None
                    if _proc_start_time(child) != start_time:
                        continue
            discovered.append(_OwnedProcess(child, start_time, pidfd))
            pending.append(child)
    return discovered


def _owned_alive(target: _OwnedProcess) -> bool:
    if target.pidfd is not None:
        try:
            readable, _, _ = select.select([target.pidfd], [], [], 0)
            return not readable
        except (OSError, ValueError):
            pass
    return _proc_start_time(target.pid) == target.start_time


def _signal_owned(target: _OwnedProcess, signum: int) -> None:
    if target.pidfd is not None and hasattr(signal, "pidfd_send_signal"):
        try:
            signal.pidfd_send_signal(target.pidfd, signum)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    if _proc_start_time(target.pid) == target.start_time:
        try:
            os.kill(target.pid, signum)
        except ProcessLookupError:
            pass


def _wait_owned(process, targets: list[_OwnedProcess], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None and not any(_owned_alive(target) for target in targets):
            return
        remaining = deadline - time.monotonic()
        descriptors = [target.pidfd for target in targets if target.pidfd is not None and _owned_alive(target)]
        if descriptors:
            try:
                select.select(descriptors, [], [], min(remaining, 0.02))
                continue
            except (OSError, ValueError):
                pass
        time.sleep(min(max(remaining, 0), 0.02))


def _close_pidfds(targets: list[_OwnedProcess]) -> None:
    for target in targets:
        if target.pidfd is not None:
            try:
                os.close(target.pidfd)
            except OSError:
                pass


def _terminate_windows_tree(process, grace_seconds: float) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=max(grace_seconds, 0.1),
        )
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=max(grace_seconds, 0.1),
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()


def _signal_process(process, signum: int) -> None:
    try:
        if os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, signum)
        elif signum == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
    except ProcessLookupError:
        pass


def terminate_owned_process(process, platform: str | None = None, grace_seconds: float = 0.4) -> None:
    """Terminate an owned subprocess tree, escalate, then drain its pipes boundedly."""
    platform = platform or os.name
    if platform == "nt":
        _terminate_windows_tree(process, grace_seconds)
    else:
        targets = _snapshot_descendants(process.pid)
        try:
            _signal_process(process, signal.SIGTERM)
            for target in targets:
                _signal_owned(target, signal.SIGTERM)
            _wait_owned(process, targets, grace_seconds)
            _signal_process(process, signal.SIGKILL)
            for target in targets:
                if _owned_alive(target):
                    _signal_owned(target, signal.SIGKILL)
            _wait_owned(process, targets, grace_seconds)
        finally:
            _close_pidfds(targets)
    try:
        process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        for stream in (getattr(process, "stdout", None), getattr(process, "stderr", None)):
            if stream is not None:
                stream.close()
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
