"""Kernel-enforced read barriers for bounded native history capture on Linux."""

import contextlib
import os
import signal
import threading


class QuiescenceError(RuntimeError):
    pass


class ReadBarrier:
    def __init__(self):
        self.descriptors = []
        self.broken = False
        self.sources = {}

    def check(self):
        import fcntl

        if self.broken or any(fcntl.fcntl(fd, fcntl.F_GETLEASE) != fcntl.F_RDLCK for fd in self.descriptors):
            raise QuiescenceError("native-writer-active")


    def read(self, path, maximum):
        self.check()
        descriptor = self.sources.get(path)
        if descriptor is None:
            raise QuiescenceError("native-source-changed")
        data = os.pread(descriptor, maximum + 1, 0)
        if len(data) > maximum or len(data) != os.fstat(descriptor).st_size:
            raise QuiescenceError("native-source-changed")
        self.check()
        return data


@contextlib.contextmanager
def read_barrier(paths):
    """Refuse current writers and detect later write opens without trusting a PID scan."""
    import fcntl

    if threading.current_thread() is not threading.main_thread():
        raise QuiescenceError("native-quiescence-unavailable")
    previous = signal.getsignal(signal.SIGIO)
    if previous not in (signal.SIG_DFL, signal.SIG_IGN):
        raise QuiescenceError("native-quiescence-signal-busy")
    barrier = ReadBarrier()

    def lease_break(_signal, _frame):
        barrier.broken = True

    signal.signal(signal.SIGIO, lease_break)
    try:
        for path, expected in paths:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            barrier.descriptors.append(descriptor)
            barrier.sources[path] = descriptor
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != expected:
                raise QuiescenceError("native-source-changed")
            try:
                fcntl.fcntl(descriptor, fcntl.F_SETOWN, os.getpid())
                fcntl.fcntl(descriptor, fcntl.F_SETLEASE, fcntl.F_RDLCK)
            except OSError as error:
                raise QuiescenceError("native-quiescence-unavailable-or-writer-active") from error
        barrier.check()
        yield barrier
    finally:
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGIO})
        try:
            for descriptor in reversed(barrier.descriptors):
                os.close(descriptor)
            while signal.SIGIO in signal.sigpending():
                signal.sigtimedwait({signal.SIGIO}, 0)
            signal.signal(signal.SIGIO, previous)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
