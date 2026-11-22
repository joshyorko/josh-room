# Native agent checkpoint evidence

This #188 checkpoint is based on Josh Room
`23c80716b8a646cbf74f6eacb04eb639af87c9c2`. Its first draft commit is
`775d023c9151a9e67d3d984d1fc4bad890cabf69`; the next checkpoint adds
cancellation evidence, directory fsync, and the updated command contract.
The evidence below does not qualify a release.

## Earlier draft evidence

These results describe the earlier `dac132494d6b994c00fdc7c9b696aa6841cdb029`
checkpoint. Restore from that implementation is disabled by the follow-up below.

| Check | Observed result |
| --- | --- |
| `python -m pytest tests/test_native_agents.py tests/test_native_agents_cli.py tests/test_native_cli_contract.py tests/test_repository_boundary.py tests/test_controller_artifact.py -q` | PASS, 78 tests. Includes real age recovery and synthetic process cancellation; Linux only. |
| `python -m pytest tests/test_material_security.py tests/test_cancellation.py tests/test_session_evidence_contract.py -q` | PASS, 169 tests. Existing secret-scanning and PCC boundary coverage. |
| `node --test vscode-extension/*.test.js` | PASS, 375 passed, 2 existing skips. Node 26.13.1; CI pins a different Node version. |
| `ruff check src tests` and `git diff --check` | PASS. |
| Actual `python -m josh_room agents` entry point | PASS, configure, capture, verify and restore synthetic Codex native JSONL with two independently generated age identities. Exact restored bytes match. No model call. |
| Actual installed T3 `0.1.12-nightly.20261122.5085` | PASS, synthetic dedicated profile starts on explicit loopback, creates isolated SQLite database, holds owned lock, stops on SIGTERM and releases lock. No provider turn or auth copied. |
| `npx --yes @vscode/vsce@4.0.0 package --no-dependencies` and ZIP byte comparison | PASS. Final packaged controller modules match canonical sources. VSIX SHA256 `251d670999d44c192eb2d24ce88146ad6112ba20fef9a6e639fdc0f470d3af05`. |
| Initial full `python -m pytest -q` | FAIL, 14 failed, 2326 passed, 10 skipped. Includes stale command contract, mirrors/configure tests observed during edits, and host restic 0.20.0 mismatch with repository pin 0.19.1. Focused affected contracts fixed and passed; pinned full-suite rerun tracked separately. |
| Full suite with pinned restic 0.19.1 | FAIL, 1 failed, 2349 passed, 10 skipped, 47 subtests. Public safety rejected literal synthetic key headers in new tests. The fixtures now construct the rejection sentinel at runtime; the scanner itself is unchanged. Affected `tests/test_native_agents.py tests/test_public_storage_safety.py` rerun PASS, 26 tests. Full suite was not repeated after this test-only fix. |
| Windows native profile run | SKIPPED, Linux host. Windows rejection is tested; Windows support is not qualified. |
| Installed VSIX user flow | NOT RUN. Packaging is not installation evidence. |
| Real JAT/age workspace vertical and live R2 | NOT RUN for this agent-state checkpoint. No JAT executable or provider secrets used. Real age alone was exercised. |
| Native cold-restored new turn, T3/Codex/Claude | NOT RUN. No paid hosted calls authorized or made. Codex result is history-only. |
| Standalone gitleaks | SKIPPED, executable unavailable. Structured native credential rejection and existing content-scanner tests passed; neither is DLP. |

Independent architecture, upstream, security, and final code reviews were performed
by Codex agents. Accepted security fixes add closed version/row policies,
structured credential rejection, pinned recipients, descriptor-bound reads,
source inventory fences, and fsynced directory publication. The remaining
[release blockers](native-agent-recovery.md#draft-only-safety-limits) are unresolved.

Optional follow-ups are [OpenCode #211](https://github.com/joshyorko/josh-room/issues/211)
and [Antigravity #212](https://github.com/joshyorko/josh-room/issues/212).
No merge, publication, or release was performed. #205 and JAT were not modified.


## Admission, writer barrier, and restore-gate follow-up

This follow-up starts at `dac132494d6b994c00fdc7c9b696aa6841cdb029`.
It replaces manual recipient trust with exact host grants and current configured
MinIO/S3 keyset checks. It removes plaintext restore staging entirely. All native
materialization is unavailable until a crash-safe implementation is qualified.

| Check | Observed result |
| --- | --- |
| Focused native admission/core/CLI/lease, controller artifact, repository boundary, and public-storage safety tests | PASS, 123 tests. Command: `python -m pytest tests/test_native_agents.py tests/test_native_agents_cli.py tests/test_native_agent_admission.py tests/test_native_agent_quiescence.py tests/test_native_cli_contract.py tests/test_controller_artifact.py tests/test_repository_boundary.py tests/test_public_storage_safety.py -q`. |
| `node --test vscode-extension/*.test.js` | PASS, 375 passed, 2 existing skips. |
| `ruff check src tests`, `git diff --check` | PASS. |
| Public-artifact patterns applied directly to new untracked files | PASS; the existing scanner is unchanged. |
| Actual Linux unmanaged-writer experiments | PASS. Existing writer prevents lease acquisition; a later nonblocking write-open is rejected and invalidates capture; inode replacement/new-file/authority changes prevent publication. Retained FDs and the original inventory remain bound through publication. |
| Cancellation and SIGKILL | PASS. Kernel leases are released; SIGKILL helper creates no plaintext staging. Restore refusal is tested before decryption or filesystem mutation. |
| Personal/work admission integration | PASS, synthetic host policy, actual Git observation, mocked read-only keyset authority, real age encryption/decryption. Removing the host grant blocks the next capture. This is not live-provider evidence. |
| Actual CLI boundary | PASS, offline capabilities/chooser contract and default-denied configure without a host grant. No authentication/runtime writes. |
| VSIX packaging and ZIP byte parity | PASS, new admission/lease/core modules match canonical source. VSIX SHA256 `5d290ad89db44e98c884fd4f87eca316050369c9d1010a2f81743d790d299cff`. Installation NOT RUN. |
| Independent security and comment review | PASS after fixing an alternate-profile-root admission gap and retaining the pre-lease inventory instead of recomputing its baseline. No further concrete bypass found. |
| First added SIGKILL probe | FAIL, test fixture lacked imports; corrected fixture then PASS, six lease tests. No product failure was hidden. |
| Full Python suite, new Windows runtime qualification | NOT RUN for this follow-up; focused changed contracts ran. Earlier-head green CI is not attributed to this head. |
| Live MinIO/S3 keyset reads, R2 admission, actual provider cold-restored new turns | NOT RUN. R2 admission and native restore fail closed. No hosted model or paid calls made. |

The chooser interface was coordinated directly in
[issue #190](https://github.com/joshyorko/josh-room/issues/190#issuecomment-7080438208).
All provider rows remain unavailable for Save. Linux file leases qualify selected
Codex files, not unmanaged whole-server shutdown or singleton startup behavior.

Remaining gates are live-provider admission acceptance, R2 custody integration,
crash-safe native materialization, native cold-restored new turns for every
provider, and installed VSIX/Windows qualification. No merge, release staging,
deployment, or release publication was performed.

## Anonymous single-session materialization follow-up

This follow-up starts at `609c2f6bd59a5c29509e61f7c9dac089b1b807c8`.
An unprivileged Linux probe proved `O_TMPFILE` and `linkat(AT_EMPTY_PATH)` work on
the tested tmpfs. The new explicit `materialize-session` command applies that
single-file transaction to one validated Codex session selected from an admitted
encrypted reference. Whole-profile `restore` and every Save component remain
blocked. This is native-file history recovery, not provider continuation.

| Check | Observed result |
| --- | --- |
| Native admission/core/CLI/leases/materialization, controller artifact, repository boundary and public safety | PASS, 148 tests. Same focused command as above plus `tests/test_native_agent_materialization.py`. |
| Anonymous-inode primitive | PASS, 19 tests. No-overwrite targets, unsupported filesystems, short writes, callback denial, directory replacement, fsync uncertainty, cancellation and SIGKILL before/after publication. |
| Cold native history pipeline | PASS, real age capture/decrypt, fresh configured profile, one original version-pinned JSONL materialized at its native relative path, byte equality, changed-file refusal and idempotent completed-commit recovery. Host authority is synthetic/mocked; no provider turn. |
| Interrupted native pipeline | PASS, real encrypted native fixture in a new helper process killed before/after link. Before link: absent file and successful retry. After link: complete matching file and successful recovered-commit verification. |
| Personal/work gate integration | PASS, real Git observation, synthetic host grants/current keyset and real age plus anonymous materialization. No native auth material copied. |
| Independent security/comment review | PASS after fixing a post-check parent-rename race and the post-link cancellation/assignment gap. Uncertain committed outcomes now reach CLI receipts. No comment findings. |
| Extension tests | PASS, 375 passed, 2 existing skips. |
| Ruff, whitespace, controller/template parity, VSIX packaging | PASS. VSIX SHA256 `b2b3b4d6cdf3039f125e98faf240ceb3781766a76864e4fb5352ac2547b5d47c`. |
| Runtime | Linux 7.3.1 x86_64, uid 1000, tmpfs. Disk-backed filesystem qualification, physical power-loss testing and forensic erasure NOT RUN and NOT CLAIMED. |
| Native provider new-turn acceptance | NOT RUN for T3, Codex and Claude. No paid calls. History file materialization does not establish a resumable provider home. |

No named plaintext temporary file is created. Before link, the last FD close
reclaims the unreachable inode. After link, the complete final file may remain;
a receipt can report committed uncertainty. Retry verifies the exact expected
native bytes under a read lease and never overwrites different state. Parent
metadata is synced and post-link canonical identity is checked before success.

Remaining missing primitives are a credential-free, consistent multi-file native
home recovery transaction and supported provider continuation after that recovery.
T3 additionally needs a closed database projection excluding auth and runnable
effects. Claude still needs a qualified versioned source policy. Safe remaining
work is disk-backed transaction qualification, per-provider native import/selection
acceptance, and owner-approved real new-turn tests after native authentication
is established separately. R2 custody and live MinIO/S3 admission remain untested.
Release hold remains unchanged.

## Disk-backed and native Codex discovery follow-up

This follow-up starts at `0aa59c21fee62343b75508aeeff1dabdae5f0a77`.
The task directory was created on `/var/tmp` only after `stat -f` verified btrfs;
`/tmp` is tmpfs. Disk checks showed ample free data/metadata space. All new test
fixtures, logs, synthetic identities and protocol schemas stayed in that one
private disk task directory and were removed after qualification.

| Check | Observed result |
| --- | --- |
| Native admission/core/CLI/lease/materialization subset on btrfs, prior head | PASS, 100 tests, including actual SIGKILL-before/after-link and retry. |
| Final affected native/parser/controller/public-safety tests on btrfs | PASS, 159 tests. Command: `TMPDIR=<private-btrfs-task-dir> python -m pytest tests/test_native_agents.py tests/test_native_agents_cli.py tests/test_native_agent_admission.py tests/test_native_agent_quiescence.py tests/test_native_agent_materialization.py tests/test_native_codex_acceptance.py tests/test_native_cli_contract.py tests/test_controller_artifact.py tests/test_repository_boundary.py tests/test_public_storage_safety.py -q --basetemp <private-btrfs-task-dir>/pytest-disk-final`. |
| Unsupported-filesystem refusal | PASS, injected kernel unsupported-operation error; no named plaintext fallback or target file. This is denial-logic coverage, not qualification on another real filesystem. |
| Installed native Codex discovery/read-selection | PASS, Codex 0.171.0 app-server found the cold-materialized original UUID at its native path and returned both original synthetic user/assistant items. Wrong-cwd listing returned no thread. Source bytes unchanged; status `notLoaded`. |
| Final native case with strict configuration | PASS, repeated after forcing `cli_auth_credentials_store="file"` in an empty isolated home and enabling `--strict-config`. An owned loopback provider guard observed zero connections through process shutdown. No user auth, login/resume/turn method, or hosted model request. |
| Reproducibility | `tests/test_native_codex_acceptance.py` uses a real installed pinned binary and real age encryption/materialization; host admission is mocked synthetic authority. It skips if Codex/age is absent or Codex version differs. No user profile is opened. |
| Native format corrections | PASS, required native timestamp/UUID file names, filename/metadata ID binding, row and metadata timestamps, originator; optional nullable u64 ordinal accepted. Missing fields, ID/date mismatch, malformed ordinals and non-finite JSON rejected before publication. |
| Ruff, whitespace, source/template parity and VSIX package | PASS. Packaged controller byte parity verified; VSIX SHA256 `4e0e1722458474537319b31a269ad2b998e265243298f0558f0889fd0a912b32`. |
| Independent security/contract/comment review | PASS. Accepted improvements reject non-finite JSON and check the zero-connection guard after full process shutdown. No other blocker or comment finding. |
| Physical power-loss, runtime resume, real new turn, provider account compatibility | NOT RUN and NOT CLAIMED. SIGKILL/file-namespace consistency and persisted-history selection do not establish these gates. |
| Full suite, new Windows/installed VSIX/live-provider admission qualification | NOT RUN for this follow-up. Earlier-head CI/results are not attributed to this head. No paid calls. |

The current capture policy corrects earlier loose draft fixtures that were only
byte-recovery evidence. It does not rewrite malformed historical ciphertext or
invent a native session from generic transcript text. The accepted fixture uses
actual versioned metadata, native event rows and the provider's real filename.
The installed binary's generated protocol schemas and
[pinned official source](native-agent-upstream.md#qualified-codex-01710-persisted-history-selection)
confirm the list/read versus resume distinction. The binary build SHA is unverified.

The minimal next runtime gate is original-ID `thread/resume` with separately
established supported native auth and account compatibility, then an observed
new `turn/start` result under that same identity. Neither was invoked. Whole-home
atomicity, T3's auth/effect-free database projection, Claude's native policy,
R2 custody/live MinIO/S3 admission and native new turns remain blocked/unverified.
All #190 providers stay `selectable: false` and general `restore: "blocked"`.
The release hold is unchanged.
