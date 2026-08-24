import os
import re
import shutil
import subprocess

import pytest


@pytest.mark.integration
def test_exact_secure_room_image_is_non_root_and_bootstrap_capable():
    if os.environ.get("JOSH_ROOM_SECURE_SMOKE") != "1":
        pytest.skip("exact secure Room acceptance is registry/tool-gated")
    image = os.environ.get("JOSH_ROOM_SECURE_IMAGE", "")
    if not shutil.which("podman") or not re.fullmatch(
        r"ghcr\.io/joshyorko/room-of-requirement@sha256:[0-9a-f]{64}", image
    ):
        pytest.skip("digest-pinned secure image or Podman is unavailable")
    command = (
        'test "$(id -u)" = 1000; '
        'test "$(id -un)" = vscode; '
        "command -v bash; command -v brew; command -v git; command -v zstd; "
        'test -w "$HOME"'
    )
    completed = subprocess.run(
        ["podman", "run", "--rm", "--pull=never", "--entrypoint", "/bin/sh", image, "-lc", command],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2048:]
