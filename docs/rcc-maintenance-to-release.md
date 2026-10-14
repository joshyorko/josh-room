# RCC maintenance and release staging

The RCC maintenance workflow qualifies controller runtime artifacts on Linux
and Windows and checks the latest published JAT runtime receipts against the
same RCC version and source. If JAT has not published a compatible artifact,
maintenance records the dependency and stops before promotion. It never
relabels or republishes immutable JAT artifacts.

When a verified JAT release advances its producer source, Josh Room records the
exact release tag, commit, source archive digest, workflow receipt, and platform
receipts in a draft consumer promotion PR. The PR describes the previous-to-new
JAT source handoff and waits for consumer review and merge. The handoff does not
edit `joshyorko/josh-all-the-things`; any upstream JAT source change still
requires separate authorization. A successful runtime promotion also advances
the extension patch version consistently in both extension copies, runtime
manifests, candidate locks, and template bootstrap pins.

GitHub does not start pull-request workflows for a PR created with
`GITHUB_TOKEN`. After opening or updating the draft promotion PR, maintenance
uses the supported `workflow_dispatch` event to start `ci.yml` on the exact PR
branch SHA. The run verifies that checkout SHA against the supplied SHA and
executes the full Linux/Windows CI matrix.
This dispatch path becomes available after the updated `ci.yml` reaches the
default branch; its first main-branch CI run also builds and stages the exact
main candidate.

On `main`, the trusted `ci.yml` run builds a versioned VSIX only after the
required tests and managed-runtime checks pass. After that workflow completes
successfully, the release workflow validates its exact workflow ID/path, run,
jobs, GitHub Actions check app, repository, branch, and source SHA. It then
downloads the VSIX candidate from that exact CI run, verifies its packaged
source bytes and receipt, builds the Python distributions, and uploads a
90-day Actions workflow artifact containing the candidate, checksums, runtime
identities, and provenance receipt. Staging does not create a Git tag or
release.

To publish, Josh dispatches the release workflow with the exact current source
SHA and staging run ID, then selects `APPROVE`. The workflow revalidates the
trusted CI run, stage run, candidate bytes, controller/JAT release assets and
digests, and current `main`. Only after that gate does it create or safely
resume the exact versioned draft release/tag, reconcile the staged assets, and
publish the GitHub release and Dev Container template. Before creating the
draft release/tag, it installs the pinned Dev Container CLI 0.89.0 and checks
the template manifest, configuration, bootstrap syntax, and required publish
CLI options. Template publication passes `ghcr.io` as the registry and
`$GITHUB_REPOSITORY/templates` as the namespace. Retries reject changed
sources, candidate bytes, runtime pins, tag targets, or unexpected release
assets. The template validation workflow remains validation-only.

The current published JAT runtime `v0.1.11-jat-runtime` carries RCC 18.19.5
receipts, while the verified controller candidate is RCC 18.19.6. The in-repo
pipeline therefore continues to block runtime promotion until an authorized
JAT owner publishes compatible verified receipts. No upstream JAT edits or
release, tag, package, template publication, or deployment are part of local
validation. Hosted approval and publication behavior is guarded by workflow
contracts but has not been exercised as a live release.
