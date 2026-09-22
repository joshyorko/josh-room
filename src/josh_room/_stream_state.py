"""Private weak identity storage for logical-source stream state."""

from __future__ import annotations

import threading
from typing import Any
from weakref import WeakKeyDictionary


class _StreamStateStore:
    __slots__ = ("_lock", "_states")

    def __init__(self) -> None:
        object.__setattr__(self, "_lock", threading.RLock())
        object.__setattr__(self, "_states", WeakKeyDictionary())

    def bind(self, stream: object, state: object) -> None:
        with self._lock:
            self._states[stream] = state

    def get(self, stream: object) -> object | None:
        with self._lock:
            return self._states.get(stream)

    def discard(self, stream: object) -> None:
        with self._lock:
            self._states.pop(stream, None)


_registry = _StreamStateStore()


def bind(stream: object, state: object, _store: Any = _registry) -> None:
    _store.bind(stream, state)


def get(stream: object, _store: Any = _registry) -> object | None:
    return _store.get(stream)


def discard(stream: object, _store: Any = _registry) -> None:
    _store.discard(stream)


del _registry
del _StreamStateStore
