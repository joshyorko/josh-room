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
