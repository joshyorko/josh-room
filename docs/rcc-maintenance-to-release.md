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
handoff can resume without another promotion commit. A rebased branch is
pushed with an exact lease, and the full CI dispatch names its resulting SHA.
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
