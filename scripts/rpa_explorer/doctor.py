"""Read-only doctor for a known task-owned target, never discover user editors."""
import argparse
import hashlib
import json
import urllib.request
from pathlib import Path


def diagnose(mission, vsix):
    target = json.loads((mission / "target.json").read_text())
    assert target["vsix_sha256"] == hashlib.sha256(vsix.read_bytes()).hexdigest()
    assert target["installed_files_verified"] > 0
    assert not target["cleanup"].get("container_removed"), "Target already cleaned; do not double-drive"
    with urllib.request.urlopen(f"http://127.0.0.1:{int(target['loopback_port'])}/healthz", timeout=2) as response:
        assert response.status == 200
    return {"ok": True, "session_id": target["session_id"], "extension": target["extension_version"],
            "installed_files_verified": target["installed_files_verified"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission", type=Path, required=True)
    parser.add_argument("--vsix", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(diagnose(args.mission, args.vsix)))
