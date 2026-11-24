# First bounded exploratory run — 2026-11-24

Candidate source: `87c51089c2dccf6b03663544a761fee714541299`, clean at packaging.
Exact VSIX, image and controller digests are in `identity.json`.
Josh Room 1.0.2; code-server 4.146.0 / Code 1.146.0; bundled Node 24.22.1;
Chrome 158.0.8127.36; Playwright 1.58.0; RCC 18.19.11;
Robocorp Tasks 4.1.1 / Python 3.12.12 for the RCC scenarios.

Mission: establish an actual installed Node workspace host, then walk discovery,
Catalog selection and the hydration precondition using disposable synthetic data.
Each scenario is bounded to 60 UI actions and 600 seconds, excluding bounded
cleanup. Initial activation used a smaller valid five-entry fixture; discovery
and Catalog used identical fresh 82-directory fixtures with a name omission.
No real Work JAT, provider credentials or other Catalog lane was used.

## Results and coverage ledger

| Workflow | Result | Evidence |
| --- | --- | --- |
| VSIX installation and manifest-declared action inventory | PASS: 165 installed files verified; editor metadata addition only | identity.json, declared-actions.json |
| Real Node host activation and visible controller-backed Check Workspace | PASS: valid, 5 entries; normal pinned RCC/controller download, verification, import and execution | activation.json, activation-controller.log, workspace-check.png, running-extensions.png |
| New Room and storage choices; cancellation before setup | PASS for visible navigation only | discovery.json |
| Split preview, partial 82-directory index, empty suggestions, browse and pagination | PASS | discovery.json, rcc-discovery.json |
| 22-level no-Git search, explicit selection, read-only review | PASS; RCC exit 0, 29 UI actions, 26.54 seconds | deep-search.png, preview-review.png, rcc-discovery-result.json |
| Catalog source selection, empty suggestions, browse all, Select All | CONFIRMED PRODUCT FAILURE | catalog-initial.json, catalog-replay.json, select-all-failure.png |
| Same Catalog sequence in fresh profile/fixture via RCC | FAIL as expected for the defect; RCC exit 10, 12 actions, 25.56 seconds | rcc-catalog.json, rcc-catalog-result.json, rcc-catalog.log |
| Enter Child without restored Catalog parent | PASS: explicit precondition refusal | catalog-replay.json, rcc-catalog.json |
| Actual Save, confirmed-save cancellation, provider audit and successful-write control | NOT RUN: no provider enrolled | separate #240 / #242 boundary |
| Actual pinned hydration, restored bytes and Git fidelity | NOT RUN: no saved parent/provider fixture | separate #240 boundary |
| Full 54-action inventory, cross-page Select All, 32-child atomicity, exact child plan | NOT RUN | declared-actions.json is an inventory, not a qualification claim |

Confirmed failure `catalog-select-all-overlap`: Save Catalog → choose synthetic
workspace → Browse all folders → Select All on This Page (32). The UI refuses
its own bulk selection: “Select either deep/level-00 or its enclosing folder.
The previous selection was kept.” No folders were selected beforehand. This is
the known failure motivating #242, independently reproduced here; not a new
Catalog implementation change or a claim that #242 is complete. The deterministic
RCC regression mapping is `scripts/rpa_pilot/robot.yaml` task `catalog_select_all`.
No product files were edited.

## Harness failures kept separate

The first Split attempt waited for an off-screen virtualized row. Its timeout
is `harness-virtualization-failure.json`, not a product failure. Filtering the
visible picker input fixed discovery of that row. A subsequent transition race
is `harness-transition-failure.json`; explicit input-state waits fixed it. The
successful fresh RCC walk validates the corrected interaction path.

The first RCC solve failed before task collection because relative `-r
requirements.txt` was resolved under `/tmp`. `rcc-environment-failure.log`
retains the diagnostic. Inlining the lock's resolved package versions corrected
the environment; actual RCC task collection and verdicts are retained separately.

## Environment choice and limits

The pinned [code-server release](https://github.com/coder/code-server/releases/tag/v4.146.0)
is an MIT-licensed Code-OSS browser-hosted editor with an actual Node workspace
extension host; its installed VSIX/controller compatibility was executed here.
The upstream license was inspected at this tag. Nothing was redistributed.
OpenVSCode Server is an untested alternative, not a second qualified host.
The Windows WebView2 embedding concept does not itself supply a Linux Node
workspace host. A browser-only vscode.dev host cannot supply this extension's
filesystem/subprocess contract. Native Windows/WebView2 was not run.

No `node_repl` or code-mode tool was exposed by tool inventory/search. The run
used an explicitly labelled local batched Playwright fallback. The shared T3
preview was also opened on the task editor and closed; UI evidence here comes
from the driver's synthetic editor session, not a private desktop capture.

The user-supplied Oligarchy disposable-environment and Bluefin testsuite
discovery/replay concepts were applied as design constraints. Exact external
testsuite #1302/#1303 links were not resolved: the same numbers in ublue-os/bluefin
refer to unrelated work. No claim is made that those implementations ran here.

The driver used its own container/profile and synthetic storage. No provider
write or actual hydration is claimed from a missing-parent refusal or absent
audit events. Cancellation covered navigation only, not the actual Save approval.
No destructive production action, host configuration change, security exploit
test, paid model request, merge or release occurred.

Documentation impact: new maintainer-only pilot recipe and first-run evidence;
no product behavior or canonical user workflow changes. This is a first pilot,
not the complete acceptance/CI lane specified by #239, #240 or #242.

Reproduce with the commands in `scripts/rpa_pilot/README.md`. Raw scratch profiles,
runtime installations and Robocorp logs were discarded after extracting sanitized
evidence. Sanitized task stdout is retained; raw Robocorp HTML is not published.
`cleanup.json` records final resource cleanup; `evidence-sha256.json` binds retained
files. Dedicated secret-scanning tools were unavailable; the recorded narrow
credential-pattern check is not equivalent to a full secret scan.
