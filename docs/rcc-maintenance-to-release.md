# RCC maintenance and release staging

The RCC maintenance workflow qualifies controller runtime artifacts on Linux
and Windows, then checks the latest published JAT runtime receipts against the
same RCC release and source commit. It only opens or updates a draft Josh Room
pull request when both runtime families have matching platform receipts. If the
JAT producer has not published artifacts for that RCC version, maintenance
reports the upstream dependency and does not rewrite the JAT pin.

The release workflow is a separate, manual two-step operation. First, an owner
stages the exact current `main` source SHA after the required CI checks are
green. This verifies the controller and JAT release metadata, checks the draft
VSIX, builds the Python distributions, records their digests and runtime
identities in `release-candidate.json`, and reconciles matching assets into the
draft release. The Git tag must resolve to the exact source SHA. Re-running a
stage is safe when the release already contains the same assets; conflicting or
unexpected assets stop the workflow.

To publish, Josh starts a separate `publish` dispatch for the same tag and SHA
and selects `APPROVE`. The workflow checks the source is still the current
`main`, confirms the same four CI checks are green for that SHA, validates the
staged receipt and release asset digests, then publishes the GitHub release and
Dev Container template. The template workflow that runs on template changes is
validation-only; it has no package publication permission.

JAT is maintained in `joshyorko/josh-all-the-things`. A new JAT artifact must
come from that repository with its own source, workflow, and platform receipts.
Josh Room maintenance must fail closed if those receipts do not match the RCC
candidate. It must not relabel or republish immutable JAT artifacts. Hosted
workflow approval behavior and live release publication still require owner
operation and have not been exercised by local synthetic tests.
