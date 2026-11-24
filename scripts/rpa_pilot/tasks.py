"""RCC task mapping for the bounded pilot; never accepts a live workspace."""
import os
import subprocess
import sys
from pathlib import Path

from robocorp.tasks import get_output_dir, task

from run import run


def scenario(phase):
    run(os.environ["PILOT_VSIX"], get_output_dir() / "pilot", phase,
        os.environ.get("PILOT_CHROME", "/usr/bin/google-chrome"))


@task
def catalog_select_all():
    """Fails if installed Select All rejects its own overlapping bulk selection."""
    scenario("catalog")


@task
def discovery_walk():
    """Walk deep/no-Git discovery and preview review in a fresh installed profile."""
    scenario("discovery")


if __name__ == "__main__":
    # Keep Robocorp logs and every artifact outside the checkout.
    output = Path(os.environ["PILOT_OUTPUT"]).resolve()
    raise SystemExit(subprocess.call([sys.executable, "-m", "robocorp.tasks", "run",
        str(Path(__file__).resolve()), "-t", sys.argv[1], "--output-dir", str(output)]))
