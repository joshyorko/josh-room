# 2026-10-24 patched Node 22 qualification

Issue: [joshyorko/josh-room#154](https://github.com/joshyorko/josh-room/issues/154).
Synthetic-only evidence; no release, tag, or published asset was changed.

## Security minimum and source

- Minimum: Node **22.23.4**, released **2026-10-21** for high-severity fixes.
- Primary source: <https://nodejs.org/en/blog/vulnerability/october-2026-security-releases>.
- Single workflow value: `NODE_VERSION: "22.23.4"` in `ci.yml`,
  `capture-candidate.yml`, `capture-windows-ui.yml`, and `release.yml`.
- Matching constants: `DEFAULT_MINIMUM` in `scripts/verify_node_version.js` and
  `MINIMUM_NODE_VERSION` in `scripts/release_staging.py`.
- Observed runtime for this local evidence: Node `v24.21.0`, npm `11.19.0`.
  A major newer than 22 satisfies the `>=22.23.4` gate; the workflow pin still
  installs exactly 22.23.4.
- JavaScript action runtime evidence: `actions/setup-node` `v7` resolved live to
  commit `949feb2413d6458794dcd2491c4babbbce0c15c1`, whose `action.yml`
  declares `runs.using: node24`.

## Runtime layers

The receipt keeps three distinct identities:

1. **Project Node** — `node_version` (for example `22.23.4`) is the interpreter
   selected by `actions/setup-node` for the job and measured by
   `scripts/verify_node_version.js`. The security minimum applies here.
2. **JavaScript action runtime** — `actions_runtime.declared_runtime` (for
   example `node24`) is the runtime declared in the action's `runs.using` block.
   `actions/setup-node@v7` is implemented as a node24 action; that is separate
   from the Node version it installs for the project. The hosted runner does not
   expose the executing action runtime to the job, and the runner image labels
   do not identify it. The CI `release-candidate` job therefore resolves the
   exact `v7` tag commit with `git ls-remote`, fetches `action.yml` from that
   commit over HTTPS, parses `runs.using`, and writes
   `dist/release-candidate/actions-runtime.json` with `action`, `ref`,
   `commit_sha`, `declared_runtime`, and a `runtime_source` URL bound to that
   exact commit. This commit plus its declared runtime is the reproducible
   action-runtime evidence.
3. **Runner image** — `node-runtime.json` records `image_os` and
   `image_version` (and the runner OS/architecture) for context. These labels
   describe the hosted runner image; they are neither the project Node runtime
   nor the JavaScript action runtime.

## Before and after inventory

| File | Before | After |
| --- | --- | --- |
| `.github/workflows/ci.yml` | `test` job had no `setup-node`; `managed-runtime-consumer` and `release-candidate` used `node-version: "22"`. | Top-level `NODE_VERSION`; `setup-node@v7` and a runtime verifier in all three jobs; `release-candidate` writes `dist/release-candidate/node-runtime.json`, resolves the exact `setup-node` `v7` commit, and writes `dist/release-candidate/actions-runtime.json`; Node tests include the new verifier tests. |
| `.github/workflows/capture-candidate.yml` | `setup-node@v4` with `node-version: '22'`. | Top-level `NODE_VERSION`; expression pin; verifier writes `dist/capture-candidate/node-runtime.json` before the identity inventory. |
| `.github/workflows/capture-windows-ui.yml` | `setup-node@v7` with `node-version: '22'`. | Top-level `NODE_VERSION`; expression pin; `pwsh` verifier writes `$RUNNER_TEMP/capture-native-ui/node-runtime.json`, which is uploaded with the receipt. Existing directory creation is now `-Force` so the earlier evidence step does not conflict. |
| `.github/workflows/release.yml` | `stage-candidate` and `publish` used `node-version: "22"` and had no Node evidence. | Top-level `NODE_VERSION`; expression pin; verifier in both jobs; stage copies the CI candidate's `node-runtime.json` and `actions-runtime.json` to `dist/` and fails when either is absent. |
| `scripts/verify_node_version.js` | Absent. | CommonJS verifier with `DEFAULT_MINIMUM = "22.23.4"`, CLI `--minimum/--source-sha/--report`, pure exported helpers, and a deterministic receipt. |
| `scripts/verify_node_version.test.js` | Absent. | `node:test` coverage for at/above minimum, rejection, malformed inputs, receipt fields, and the real CLI. |
| `scripts/release_staging.py` | Receipt schema 1 with runtime identities only. | Schema 2 adds `node_runtime` and `actions_runtime`; reads and validates `node-runtime.json` and `actions-runtime.json`; requires the pinned minimum, a satisfying version, `satisfied: true`, matching source SHA, Actions image identity, and an exact setup-node commit with a `node<N>` declared runtime and commit-bound source URL. Both files are digest-covered staged assets. |
| `tests/test_release_pipeline_contract.py` | No Node pin assertions. | Asserts every `setup-node` workflow has the pin and a later verifier, constants agree, CI runs the verifier tests, the action-runtime resolution writes the receipt, staging copies it, and the Python staging module validates it. |
| `tests/test_release_staging.py` | No Node evidence fixture. | Valid `node-runtime.json` and `actions-runtime.json` fixtures, embedded identity assertions, and rejection of missing, below-minimum, mismatched-source, unsatisfied, malformed action-commit, bad declared runtime, and unbound action-source receipts. |
| `docs/rcc-maintenance-to-release.md` | No Node runtime rule. | Documents the pin, verifier, evidence paths, rebuild rule, and unchanged owner approval. |

## Rebuild determination

Inspected published releases and the package script; **no published VSIX needs
rebuilding for this change**.

- `v0.1.36-standalone-vsix` targets `19723c71d64f81bd664cf34491d13014c213773d`
  and contains `josh-room-0.1.36.vsix` (509620 bytes, SHA256
  `0d7a4ea8577aacd7067321f5b364bb811234a3210977e653d9c97078cda46eaa`),
  `SHA256SUMS`, `ci-readiness.json`, `release-candidate.json`,
  `vsix-candidate.json`, and the Python wheel/source archive.
- `v0.1.35-standalone-vsix` targets
  `e773963106829668c9bca8bf94c045c99f8267f1` and contains
  `josh-room-0.1.35.vsix` (500173 bytes, SHA256
  `21d3b5d6abd77e040fa4a23efa3f50041d1326c775a90720b6c0cbcfffead0fa`)
  with the equivalent receipt assets.
- Downloaded both VSIX files. Their SHA256 values match the release asset
  digests. `v0.1.36` has 105 archive members and `v0.1.35` has 103; neither
  contains a Node executable, `.node` native addon, or bundled Node runtime.
  They contain extension JavaScript, Python controller sources, manifests, and
  the vendored YAML module.
- The extension package script is
  `npx --yes @vscode/vsce@4.0.0 package --no-dependencies`, so dependencies and
  Node itself are not embedded. Node is a build-host toolchain.
- A published VSIX is therefore not invalidated by the patched-Node minimum.
  Re-running CI or staging needs the new evidence, but no tag or published asset
  was retagged, reuploaded, or rewritten here.

Remaining limit: this inspection establishes archive contents and digests for
the two published standalone VSIX assets. It does not establish behavior under
Node 22.23.4 on a Windows host, because those Actions matrix runs have not
executed for this branch. The action-runtime receipt records the tag's resolved
commit and that commit's declared `runs.using` runtime; it cannot observe the
runtime GitHub actually used to execute the action, because the runner does not
expose that value.

## Verification

Local host: Linux x64, Node `v24.21.0`, npm `11.19.0`, Python 3.13.17 via uv
0.13.5. The environment's system glibc lacks `librt.so.1`; a temporary
`LD_LIBRARY_PATH` compatibility symlink was used only to start the local Python
runtime. No project dependency was installed from a private source.

| Command or workflow | Scope | Result |
| --- | --- | --- |
| `uv sync --group dev` | Repository dev dependencies | PASS |
| `uv run pytest tests/test_release_pipeline_contract.py tests/test_release_staging.py tests/test_release_candidate.py -q` | Contract, staging, candidate verifier tests | PASS — 36 passed |
| `uv run ruff check src tests` | Python lint | PASS — all checks passed |
| `node --test scripts/verify_node_version.test.js` | Verifier unit and CLI tests | PASS — 6 passed |
| `git ls-remote` + `curl` + `awk` resolution of `actions/setup-node` `v7` | Action-runtime evidence simulation | PASS — resolved `949feb2413d6458794dcd2491c4babbbce0c15c1`, parsed `runs.using` as `node24`, and wrote deterministic JSON |
| `actions_runtime_identity('/tmp/actions-runtime-check')` on the simulated receipt | Python staging validation of generated evidence | PASS — accepted action, ref, commit, declared runtime, and commit-bound source |
| `node --test vscode-extension/*.test.js` | Extension suite on host Node 24.21.0 | FAIL — 303 passed, 1 failed, 2 skipped. The failure is `HTTPS download aborts a redirected active response...` and reproduces unchanged on `origin/main` at `19723c7` (66 passed, 1 failed in `runtime.test.js`). It is not caused by this change; CI pins 22.23.4. |
| `node scripts/verify_node_version.js --minimum 22.23.4 --source-sha $(git rev-parse HEAD) --report /tmp/node-runtime.json` | Real gate, accepted path | PASS — exit 0; receipt records Node 24.21.0, npm 11.19.0, minimum 22.23.4, source SHA, and actions identity |
| `node scripts/verify_node_version.js --minimum 99.0.0 ...` | Real gate, rejection path | PASS (expected rejection) — exit 1, stderr reports the runtime below the minimum, no report written |
| `git diff --check` | Whitespace and conflict markers | PASS |
| Linux and Windows GitHub Actions matrices | CI, capture, release stages on the pinned runtime | NOT RUN — the pull request has not run them; this receipt does not claim they passed |

Release controls and the manual owner `APPROVE` gate are unchanged. Staging and
publishing now also reject candidates without patched project-Node evidence or
exact setup-node action-runtime evidence.

## Release preparation

The owner requested a new release from this change on 2026-10-24. Since
`v0.1.36-standalone-vsix` is already published, release preparation advances the
extension to 0.1.37 and the template patch version using the existing
`promote_rcc_candidate._version_update` helper. Both package manifests, runtime
manifests, candidate locks, and bootstrap extension pins move together. Runtime
dependency pins and existing published artifacts remain unchanged. Publication
still requires fresh exact-source CI and staging evidence and the existing
owner-approved release workflow.
