# Issue #202 synthetic review evidence

All visual captures use deterministic synthetic providers and an offline HTML
snapshot. No real workspace, identity, provider or private data is included.
The machine-readable [browser receipt](browser-verification-receipt.json)
records the tested browser, counts, timings, security checks and individual
pass/fail results. These are software-rendered Chrome checks, not physical GPU
qualification or human acceptance.

| Capture | Review target |
| --- | --- |
| [Night root](01-night-unix-overview.png) | Full-screen synthetic UNIX hierarchy and low forward camera. |
| [Night directory](02-night-unix-directory.png) | Navigation inside `/src/core`. |
| [Night selected file](03-night-spotlight-v2.png) | Overhead beam and floor pool without a lamp. |
| [Night baseline](04-night-spotlight-baseline.png) | Same selected target for spotlight A/B review. |
| [Source/diff](04b-night-inspect-source-diff.png) | Typed code preview and highlighted diff. |
| [Execution jump](04c-night-inspect-run-jump.png) | Clickable producer relationship, run steps and error. |
| [JSON](04d-night-inspect-json-tree.png) | Collapsible structured content. |
| [Grass root](05-grass-unix-overview.png) | Alternate FSN landscape with the same hierarchy. |
| [Grass directory](06-grass-unix-directory.png) | Directory focus in Grass. |
| [Grass selected file](07-grass-spotlight-v2.png) | Selected beam in Grass. |
| [Grass baseline](08-grass-spotlight-baseline.png) | Matching baseline for spotlight comparison. |
| [Reduced motion](09-night-reduced-motion.png) | Camera cut and selection with motion disabled. |
| [SQL](10-sql-console.png) | Read-only linked-object queries. |
| [10 items](11-scale-10-items.png), [300 items](01b-night-pipeline-300-overview.png), [540 items](12-scale-540-items.png) | Pipeline scale, aggregation and constant geometry size. |
| [PDF](03b-night-spotlight-v2-evidence-pdf.png) | Bundled lazy PDF evidence view. |
| [WebGL off](13-webgl-off-grid-fallback.png) | Automatic grid fallback and functional inspector. |

The inherited second-pass browser receipt initially recorded a failed UNIX
inspector gate: the test counted the default diff slot while expecting source
lines. The test now explicitly selects Source before checking it; retain the
rerun's observed outcome rather than treating the old failure as a pass.

[Native Hauler receipt](hauler-fileserver-spike-receipt.json) is a separate real
Hauler 2.1.1 experiment confined to a Linux network namespace. It proves
projection behavior and exposure limits, not safe automatic binding or a
released JAT/extension runtime. [ADR 0012](../../adr/0012-room-view-native-serving-and-hauler-spike.md)
records the decision and remaining gates.

Pending acceptance: five-second recognition with 8–10 unbriefed participants
(target ≥70% filesystem recognition), three identical lookup tasks compared
against Grid, physical low-end GPU timing, installed VSIX and qualified managed JAT runtime integration using the
unchanged Hauler pin. The optional Hauler bind-host feature is not required. No merge, release or live deployment is implied.

[Artifact listener-scope correction](artifact-scope-correction.json) supersedes the
earlier blanket Hauler bind-host dependency: real unchanged 2.1.1 artifact
serving and current Room parser/probes pass across Files/Registry/Both/Auto.
