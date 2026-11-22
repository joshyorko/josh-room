# Disposable enrolled native-agent admission acceptance

Verified 2026-11-22 on Linux with a fresh loopback MinIO server and a separate
native Secret Service. No existing user/provider credentials, host keyring or
host D-Bus socket were used. All identities, sessions, workspace contents and
provider credentials were synthetic. No provider-native/model turn occurred.

The setup follows the nonsecret recipe and provider script at Josh Room commit
`ebc3e3b770ef5368dfd33c4cefa95d03cc6a3ed7`:
`docs/recovery-acceptance.md` and `scripts/synthetic_recovery_provider.py`.
Only those source files were copied; no previous lane's generated state was used.
The provider bootstrap completed normal bucket creation, encryption enrollment
and three real Room snapshots before native-agent qualification.

## Runtime and custody

| Boundary | Observed result |
| --- | --- |
| MinIO | Official `RELEASE.2025-09-07T16-13-09Z`, commit `07c3a429bfed433e49018cb0f78a52145d4bedeb`, Linux amd64; binary SHA-256 `7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f`. |
| Secret Service image | Task-built Debian image ID `sha256:e49b98b26badd9896fae13987eb7b8993e58e3f18c3bcdf240b8a5f8ebcc2b2f`; GNOME Keyring `42.1-1+b2`, D-Bus `1.14.10-1~deb12u1`, libsecret tools `0.20.5-3`. |
| Service isolation | Unique disposable container, network disabled, all capabilities dropped, no-new-privileges, UID/GID 1000; only the task directory was bind-mounted at the same path. No host home/socket mount. |
| Native custody probe | Production `keyring.backend_status()` returned `linux-secret-service`, available and unlocked through the private task socket. Provider access used production `secure_lookup`; no runtime credential handoff or mocked keyset reader. |
| Verification identity | Generated recovery identity stayed in the disposable native keyring. Each native `verify` used a mode-0600 transient handoff, removed in `finally`; no private identity or provider credential entered evidence/config/argv. |

The registry and explicit host grant are separate authorities. The grant binds
the exact Dimension, Room, profile, canonical workspace/profile/reference paths,
observed synthetic Git origin, scope, tool list, encryption domain, generation and
recipients. The actual current keyset was read from the enrolled MinIO bucket on
each admitted operation.

## Initial failure and normal enrollment correction

The first unmocked call failed closed with `native-admission-dimension-mismatch`.
Normal `encryption initialize` returned the enrolled domain but left the optional
Dimension registry domain field absent. Admission previously required that
optional cached field even though the explicit host grant and freshly read
keyset already supplied the required binding.

A preliminary fixture with an explicitly populated nonsecret registry domain
passed, but it is not the final normal-flow evidence. The final run restored the
ordinary absent/`None` registry field and exercised the narrow admission change:
an absent registry domain is permitted only while the live keyset still matches
the grant's domain, generation, recipients and exact physical bucket. Explicit
registry mismatches and disconnected state remain refusals.

The qualified admission source was worktree changes atop
`35673f9b8b9955a1853ca8995c392d8e27aa0165`; at qualification,
`src/josh_room/native_agent_admission.py` had SHA-256
`c3c080c7812dccc29d337d99ca6ee6b412f81e1c1ec0b0ad05b30712d9a81c58`.

## Passed observations

| Operation | Result |
| --- | --- |
| Normal enrolled Dimension, absent registry domain | PASS, production unmocked admission and dedicated-profile configure. |
| Codex `0.171.0` native history | PASS, actual age capture and decrypt/verify; exact synthetic native bytes unchanged; no native continuation claim. |
| Claude `2.1.310` narrow text history | PASS, actual age capture and decrypt/verify; exact synthetic native bytes unchanged; no native continuation claim. |
| Revoked tool grant | PASS, `native-admission-tool-not-authorized`. |
| Stale grant generation | PASS, `native-admission-keyset-mismatch` against the actual current served keyset. |
| Missing current keyset | PASS, the task's keyset was removed briefly; production admission refused, proving no stale success cache. Original bytes stayed only in process memory and were restored in `finally`; authority re-admission then passed. |
| Checked-in opt-in tests | PASS, `tests/test_native_agent_live_admission.py`: 3 passed, 1 explicitly skipped in 1.55s. These cover both providers and local grant/generation refusals; the separate exclusive keyset-mutation case was skipped in that run. The missing-keyset observation above came from the task probe before sharing the live fixture. |
| Normal opt-out | PASS, 4 tests skipped without explicit disposable-provider opt-in. |

## Reproduce

Prepare a fresh fixture with the referenced recipe, a distinct container/ports,
and task-private disk-backed storage. Keep its MinIO session and native Secret
Service alive. Set `JOSH_ROOM_TEST_LIVE_AGENT_ROOT` to that fresh fixture root and
run from the Linux worktree:

```sh
.venv/bin/pytest -q tests/test_native_agent_live_admission.py \
  --basetemp="$JOSH_ROOM_TEST_LIVE_AGENT_ROOT/admission-tests"
```

The test creates its own private policy/profile/workspace/reference paths and
uses the fixture's isolated socket. It requires the normal registry's domain
field to remain absent. Never point it at a real user/provider/keyring profile.

For an exclusively owned disposable server only, set
`JOSH_ROOM_TEST_LIVE_AGENT_KEYSET_REVOCATION=1` to run the current-keyset removal
case. Do not run that case concurrently with another operation using the same
fixture: it intentionally makes admission unavailable briefly.

This acceptance qualifies enrolled admission/custody and local encrypted native
history operations. It does not qualify durable Room sidecar lifecycle, native
provider continuation, existing user authentication, production storage,
Windows, installed VSIX actions or immutable RCC packaging. Save selectors stay
false until their separate durable lifecycle gate is proven. Owned services are
retained only while the parent lane needs them and must be cleaned up with the
task's data, image and private socket afterward.
