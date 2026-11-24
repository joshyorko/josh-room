"""Submit one typed batch to an already-owned bounded RCC mission; no UI logic."""
import argparse
import json
from pathlib import Path


def latest(log):
    states = [json.loads(line[12:]) for line in log.read_text().splitlines() if line.startswith("PILOT_STATE ")]
    if not states or states[-1].get("complete"):
        raise ValueError("No active mission response")
    return states[-1]


def submit(log, inbox, actions, stop=False):
    response = latest(log)
    if not response.get("ok", True):
        raise ValueError("Previous batch failed; verify/reproduce before another mission")
    number = response.get("batch", 0) + 1
    request = {"schema_version": 1, "binding": response["binding"], "actions": actions}
    if stop:
        request["stop"] = True
    destination = inbox / f"{number:04}.json"
    if destination.exists():
        raise ValueError("Batch is already submitted; inspect its response rather than repeat")
    temporary = inbox / f"{number:04}.pending"
    with temporary.open("x") as stream:
        json.dump(request, stream)
    temporary.rename(destination)
    return {"submitted": number, "request": destination.name}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--inbox", type=Path, required=True)
    parser.add_argument("--actions", default="[]", help="JSON typed action array; never credentials")
    parser.add_argument("--stop", action="store_true")
    args = parser.parse_args()
    print(json.dumps(submit(args.log, args.inbox, json.loads(args.actions), args.stop)))
