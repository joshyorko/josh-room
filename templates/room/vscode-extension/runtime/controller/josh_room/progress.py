import json
import os
from pathlib import Path

from robocorp import log


def _display(value: str, limit: int) -> str:
    return " ".join(str(value).split())[:limit]


def report_progress(
    stage: str,
    message: str,
    *,
    current: int | None = None,
    total: int | None = None,
) -> None:
    stage = _display(stage, 48)
    message = _display(message, 240)
    log.info(f"{stage}: {message}")
    destination = os.environ.get("JOSH_ROOM_PROGRESS_FILE")
    if not destination:
        return
    record = {"format_version": 1, "stage": stage, "message": message}
    if current is not None:
        record["current"] = int(current)
    if total is not None:
        record["total"] = int(total)
    body = (json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n").encode()
    try:
        fd = os.open(Path(destination), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.chmod(destination, 0o600)
    except OSError as error:
        log.warn(f"Unable to publish Josh Room progress: {error}")
