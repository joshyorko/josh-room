# RCC maintenance and release staging

The RCC maintenance workflow qualifies controller runtime artifacts on Linux
and Windows and checks the latest published JAT runtime receipts against the
same RCC version and source. If JAT has not published a compatible artifact,
maintenance records the dependency and stops before promotion. It never
relabels or republishes immutable JAT artifacts.

The VSIX packager is pinned to `@vscode/vsce` 4.0.0. Promotion updates the
template version in `devcontainer-template.json` and both release locks in the
same candidate, and its report includes each qualified JAT platform digest for
the generated PR description.

## Patched Node 22 qualification

CI, capture, and release workflows select Node through one workflow value,
`NODE_VERSION: "22.23.4"`. Node 22.23.4 contains the 2026-10-21 Node 22
high-severity security fixes. Requesting the `22` major alone can accept an
unpatched runtime on an older run and does not record which runtime produced an
artifact.

Every `actions/setup-node` step is followed by
`node scripts/verify_node_version.js --minimum "$NODE_VERSION" --source-sha "$GITHUB_SHA" --report <path>`.
The verifier compares `process.versions.node` with the pinned minimum, rejects a
malformed source SHA, and fails the job when the observed runtime is older than
22.23.4. On success it writes a deterministic JSON receipt containing the
observed Node and npm versions, the minimum, the exact source SHA, and the
Actions runner image labels (`github_actions`, `runner_os`, `runner_arch`,
`image_os`, `image_version`). Evidence paths are `$RUNNER_TEMP/node-runtime.json`
for CI tests and both release jobs, `dist/managed-runtime-evidence/` for the
managed-runtime consumer matrix, `dist/release-candidate/` for the CI candidate,
`dist/capture-candidate/` before the capture identity inventory, and
`$RUNNER_TEMP/capture-native-ui/` for the native Windows UI receipt.

Three runtime layers stay distinct:

- **Project Node** (`node_version`, e.g. `22.23.4`) is the interpreter selected
  by `actions/setup-node` for the job's own commands. The minimum applies here.
- **JavaScript action runtime** (`actions_runtime.declared_runtime`, e.g.
  `node24`) is the runtime GitHub uses to execute the action implementation.
  The runner image is not the action runtime, and a job cannot inspect the
  executing action runtime directly. The CI `release-candidate` job therefore
  resolves the exact `actions/setup-node` `v7` tag commit with `git ls-remote`,
  fetches `action.yml` from that commit, parses its `runs.using` value, and
  writes `dist/release-candidate/actions-runtime.json` with the action, ref,
  commit SHA, declared runtime, and commit-bound raw source URL. That commit and
  its declared runtime are the reproducible action-runtime evidence.
- **Runner image** (`image_os`, `image_version`) describes the hosted runner
  image. It is recorded for context and does not identify either Node runtime.

Release staging copies the CI candidate's `node-runtime.json` and
`actions-runtime.json` to `dist/` and fails when either is absent.
`scripts/release_staging.py` requires both files, checks the project Node
schema, pinned minimum, observed version, satisfied flag, source SHA, and Actions
identity, and checks the action runtime schema, action, ref, exact 40-hex commit,
`node<N>` declared runtime, and commit-bound source URL. It embeds the normalized
identities as `node_runtime` and `actions_runtime` and covers both files with the
staged asset digests. A staged or published candidate without patched-Node or
action-runtime evidence is rejected. Rebuild an older staged candidate from a
new patched-Node CI run instead of publishing it without that evidence.
Publishing otherwise keeps the exact-source checks and Josh's explicit
`APPROVE` gate unchanged. See
[the 2026-10-24 qualification receipt](receipts/2026-10-24-node-22-qualification.md).

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
JAT source archive requests use that read-only workflow token only on the
GitHub API host.
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

The repository must allow GitHub Actions to create pull requests. If that
permission is disabled, maintenance leaves its generated branch and published
controller evidence intact and reports the PR-creation failure. It does not
change repository permissions. After the permission is corrected, the PR
handoff regenerates the candidate from the qualified current `main`, without
rebasing historical maintenance commits or copying their older manifests.
Missing generated changes come from the verified controller/JAT receipts.
Before replacing an existing branch, maintenance preserves its tip under
`maintenance/rcc-runtimes-backup-<sha>` and pushes with an exact lease. Manual
changes remain recoverable there for separate review. An existing attempt is
reused only if its complete tree matches fresh generation and it contains the
qualified main commit. The full CI dispatch names the resulting SHA.
Branch dispatches run qualification; promotion still requires current `main`.
An unchanged branch with no diff from `main` does not create an empty PR.
The handoff retains the detailed preflight report when the branch already
contains the generated update, so a retry does not erase source or artifact
provenance from the PR description.

## 0.1.35 runtime adoption

The 0.1.35 extension candidate and 0.1.5 Dev Container template adopt RCC
18.19.7 at `758c97808bef8ae8bc5779d82c23ee11abfa47b3`. Native Room Store
capture requires that version, and encrypted snapshot metadata accepts its
artifact receipts while retaining support for previously supported versions.
Both extension copies, bootstrap pins, runtime manifests, and release locks
advance together. RCC 18.19.7 fixes pip installation targeting the wrong Python
interpreter during legacy Conda/micromamba environment builds.

Compatible immutable JAT runtime `v0.1.13-jat-runtime` uses source
`3a3d897c31264308f1c1721d8fc3c9ebca4c8293`. Its Linux and Windows qualification,
publication, and postpublication checks passed in
[run 38891273008](https://github.com/joshyorko/josh-all-the-things/actions/runs/38891273008).
Room controller qualification and JAT compatibility passed in
[maintenance run 38891973950](https://github.com/joshyorko/josh-room/actions/runs/38891973950).
The first promotion attempt hit a stale already-merged maintenance branch;
after resetting only that branch to current main with an exact lease, the
failed promotion job resumed using the same qualified artifacts and immutable
controller prerelease. No platform rebuild or artifact replacement was needed.

The release workflow reports the staged SHA256SUMS in its log and summary for
owner review. Josh must still supply the exact current source SHA, staging run
ID, and `APPROVE`; leave both empty-draft recovery fields blank for a new
release. Staging does not publish the extension or template.

## 0.1.36 filtered-capture runtime adoption

The subsequent 0.1.36 extension candidate and 0.1.6 template retain the 0.1.35
controller and RCC 18.19.7 pins. Their JAT source and both platform artifact pins
advance together to immutable
[`v0.1.14-jat-runtime`](https://github.com/joshyorko/josh-all-the-things/releases/tag/v0.1.14-jat-runtime),
source `fe43065a654d1649ba9ebe5a6f202eac612e4385`. This runtime embeds JAT's
explicit filtered-capture implementation; the previous dependency-only runtime
does not establish that capability.

The Linux and Windows source-embedded archives passed filtered restore and native
schema-v3 promotion qualification in
[run 38934211098](https://github.com/joshyorko/josh-all-the-things/actions/runs/38934211098).
After separate approval, native publishers pushed those unchanged archives in
[run 38938335140](https://github.com/joshyorko/josh-all-the-things/actions/runs/38938335140).
[Run 38938737552](https://github.com/joshyorko/josh-all-the-things/actions/runs/38938737552)
verified registry layers, uploaded the eight hash-verified assets to the known
draft ID, and published the immutable prerelease from that exact source. The
driver commits are publication tooling, not runtime source. Resolved freezes
were recovered from the qualified archives without rebuilding them.

Room adoption verifies the published source archive, actual release asset
metadata, original receipts, and RCC provenance before updating the four JAT
manifest/lock objects. The still-unpublished Room candidate remains 0.1.36;
published 0.1.35 artifacts are untouched. Final source qualification and a fresh
stage must follow adoption. Josh still supplies that exact source SHA, stage run
ID, and `APPROVE` for final Room publication.

Managed consumer acceptance uses a separate synthetic Python RCC project to
verify user environment capture and restored artifact anchors. Its environment
must not copy JAT's producer freeze: that freeze contains JAT source-relative
installation hooks and belongs to the producer checkout. The user-project
capture, larger workspace capture and required RCC anchor assertions remain
part of the native consumer gate.

## Resume an interrupted empty draft

The publish workflow retains the release ID returned by the create API and
uses it for subsequent reads and asset uploads. Reads retry at most five times;
creation is never retried automatically. If reads remain unavailable, the run
reports the retained ID and stops without uploading or publishing. This handles
read-after-create failures without assuming whether their cause is API timing
or token visibility.

For a known draft, supply `resume_empty_draft_id` and
`expected_draft_source_sha`. The workflow loads that exact ID directly, without
requiring tag or listing discovery first. Before retargeting an empty draft to
a newly qualified main source, it verifies the expected old source, exact tag,
draft state, non-prerelease state, absence of assets and tag, and original
source-bound body. Existing matching assets and published retries continue
through the unchanged reconciliation gates. A failed lookup never authorizes
creating a replacement draft.

After a workflow repair changes main, wait for that new source's CI and staging
run, then use the new `source_sha` and `stage_run_id` with the preserved draft ID
and its original source SHA. Do not reuse the older source's staging input for
the repaired workflow. Final publication still requires Josh's `APPROVE`.
