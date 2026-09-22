"""Isolated executable used by the Codex command-hook configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# A Codex event cwd may contain sitecustomize.py, .env files, PATH shims, or a
# PYTHONPATH import hijack.  The installer invokes this file with isolated -I;
# keep only operator-selected Josh/Codex state paths.
_ALLOWED_ENV = {
    "HOME",
    "CODEX_HOME",
    "XDG_STATE_HOME",
    "JOSH_ROOM_HOOK_OUTBOX",
    "JOSH_ROOM_OUTBOX_ROOT",
    "JOSH_ROOM_HOOK_RECEIPT",
    "JOSH_ROOM_CODEX_ACTIVE_ROOT",
    "JOSH_ROOM_CODEX_ARCHIVED_ROOT",
}
for _key in tuple(os.environ):
    if _key not in _ALLOWED_ENV:
        del os.environ[_key]

try:
    pwd = __import__("pwd")
    _safe_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
except (ImportError, KeyError, AttributeError, OSError):
    _safe_home = Path.home()
os.chdir(_safe_home)

# Executing a module file does not put its package parent on sys.path.  This is
# an absolute path to the installed Josh Room package, never the event cwd.
_PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)
sys.path[:] = [_PACKAGE_PARENT, *[item for item in sys.path[1:] if item and Path(item).is_absolute()]]
codex_hook_main = __import__("josh_room.hook_runtime", fromlist=["main"]).main


if __name__ == "__main__":
    raise SystemExit(codex_hook_main())
