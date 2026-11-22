"""Source-runtime acceptance guard; delegates to the actual installed module CLI.

Recovery must receive an existing private handoff created by the installed
extension. No provider/controller functions or successful results are mocked.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from josh_room.private_paths import verify_private_path

arguments = sys.argv[1:]
action = arguments[1] if arguments[:1] == ["recovery"] else None
if action:
    if action not in {"tree", "inspect", "diff", "extract"}:
        raise RuntimeError("unsupported acceptance recovery action")
    material = os.environ.get("JOSH_ROOM_ENCRYPTION_MATERIAL")
    if not material:
        raise RuntimeError("installed recovery requires an existing private material handoff")
    handoff = Path(material)
    verify_private_path(handoff, directory=False)
    verify_private_path(handoff.parent, directory=True)
    if handoff.name != "material.identity" or not handoff.parent.name.startswith("josh-room-encryption-"):
        raise RuntimeError("existing handoff did not originate in the installed extension")
    if handoff.parent.parent.resolve() != (Path(os.environ["JOSH_ROOM_ACCEPTANCE_ROOT"]) / "provider-runtime").resolve():
        raise RuntimeError("existing handoff is outside the owned fixture runtime")
result = subprocess.run([sys.executable, "-m", "josh_room", *arguments], check=False)
if action:
    probe = Path(os.environ["JOSH_ROOM_ACCEPTANCE_ROOT"]) / "existing-handoff-probes.jsonl"
    with probe.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"action": action, "native_private_existing_handoff": True,
                                 "cli_exit_status": result.returncode}) + "\n")
raise SystemExit(result.returncode)
