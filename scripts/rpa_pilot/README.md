# Installed-extension exploratory pilot

This is a bounded Linux acceptance pilot, separate from Catalog implementation
and the full qualification work in Josh Room #239, #240 and #242. It adds no
shipped controller dependencies. See [first-run evidence](../../docs/receipts/rpa-pilot/README.md).

Prerequisites on the Linux workstation or development container: Docker access,
installed Google Chrome and its system libraries, RCC 18.19.11, and network
access to the pinned editor image and the candidate's runtime artifact URLs.
The driver uses Playwright, **not Codex code mode**. No browser download or host
root configuration change is required. The first run used existing host Chrome.

Package the exact candidate using `npm run package -- --out "$TASK_ROOT/candidate.vsix"`
from `vscode-extension/`. Create TASK_ROOT with `mktemp -d` under `/tmp` first;
check disk space before packaging or resolving environments. Run from repo root:

```sh
export PILOT_VSIX="$TASK_ROOT/candidate.vsix"
export ROBOCORP_HOME="$TASK_ROOT/rcc-home"
export PILOT_OUTPUT="$TASK_ROOT/discovery"
rcc run -r scripts/rpa_pilot/robot.yaml -t discovery_walk --silent
export PILOT_OUTPUT="$TASK_ROOT/catalog"
rcc run -r scripts/rpa_pilot/robot.yaml -t catalog_select_all --silent
```

Use a new PILOT_OUTPUT for every run. `PILOT_CHROME` optionally names an existing
Chrome executable. RCC resolves only the robot dependencies; each scenario
installs the candidate VSIX into a fresh code-server profile in its own container.
The image digest is fixed in `run.py`. All 165 installed package files are
verified; only the editor's added package.json `__metadata` is permitted.
The extension acquires its own managed controller normally. No test runtime,
provider stubs, registered-command replacements or chooser injections are used.

`discovery_walk` creates 82 admissible directories, a 22-level project without
Git, 55 pagination folders, two sibling projects and a nonportable-name omission.
It exercises New Room, storage choices, cancel, Split preview, suggestions,
browse, next page, deep search, selection and read-only review. The 82-entry
assertion is specific to this fixture.

`catalog_select_all` checks the missing-parent hydration guard, invokes Save
Catalog through the command palette, accepts the browser-hosted folder chooser,
switches from suggestions to all folders and selects the first page. It fails
on the confirmed self-overlap warning. A nonzero selected count without that
warning passes this narrow regression; this does **not** verify every resulting
path, the 32-child limit, or a Save plan. It is intentionally red on the tested
candidate. This task does not implement a product fix.

Each walk has a 60-action budget; the owning runner has a 600-second deadline,
90-second startup bound and 120-second subprocess bounds. SIGTERM raises through
cleanup. SIGKILL cannot guarantee Python cleanup. Container resources are bounded
to 2 CPUs, 2 GiB and 512 processes with no capabilities. Only task scratch is
mounted; there is no Docker socket, workstation HOME or credential mount.
The unauthenticated editor publishes only on host loopback; its internal
container listener binds 0.0.0.0. No login, model call or Copilot interaction is
part of the mission. This is operational isolation, not security qualification.

The runner removes its own container, verifies listener closure, and removes its
private workspace/profile/runtime directory on success or failure. It preserves
synthetic UI text/screenshots, product output and `result.json` in PILOT_OUTPUT;
cleanup errors fail the task. Robocorp writes its logs there too. RCC additionally
writes `scripts/rpa_pilot/output/`; retain only sanitized evidence and delete that
generated directory, TASK_ROOT and task-owned images when finished. Never run
broad container/image/cache pruning against unrelated resources.

The hashed pip lock supports the direct fallback driver; `conda.yaml` inlines
the same resolved package versions because RCC's generated temporary requirements
file does not resolve robot-relative `-r requirements.txt`. Keep both aligned.
The retained RCC environment freeze is evidence of the first solve, not an
independent claim of an immutable Environment Artifact for this test robot.

Actual Save, confirmed-save cancellation with audit barriers, enrollment,
provider mutation, pinned parent/child restore, content/Git fidelity and native
desktop/Windows remain separate unqualified boundaries. This pilot does not
replace those gates or add CI/publication work.
