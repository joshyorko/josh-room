# Native history Room-reference acceptance

This Linux receipt qualifies the explicit history-reference lifecycle and the
registered installed commands. It does **not** qualify native continuation,
original T3 app-thread recovery, automatic Room Save inclusion, an immutable new
controller artifact, Windows, or multi-provider home restoration.

The trial uses the real disposable MinIO/native Secret Service recipe described
in [native-agent-live-admission.md](native-agent-live-admission.md). All workspaces,
native messages, identities, buckets and keyring entries are synthetic and
task-owned. No existing user/provider authentication or employer data is used.
No hosted/model/paid call is made.

## Actual provider lifecycle

Normal CLI operations, not injected provider/catalog/keyring results, established
the following:

| Operation | Observed result |
| --- | --- |
| Explicit capture | Real Codex `0.171.0` and narrow Claude Code `2.1.310` files captured into encrypted immutable objects and an encrypted exact-point control sidecar. Logical JAT/catalog component sets remain unchanged. |
| Status and verification | Metadata-only status and original native UUID discovery pass; plaintext message data is not exposed in the receipt. The selected historical point remains pinned after a newer point is saved. |
| Cold native restoration | Both providers restore byte-identically into separately configured fresh, explicitly authorized profiles; identical-file retries are verified. No profile freshness or collision guard is bypassed. |
| Cold Room restoration | Normal `hydrate` restores the selected Room into the absent **same canonical cwd**, after the task-owned prior workspace is quarantined. Native history restoration then succeeds against the normal hydration marker. |
| Same-Dimension Copy | Both archives are newly encrypted, have distinct ciphertext object identities, bind the new Room/JAT and preserve original provenance. Source removal does not invalidate the copied reference. Agent ciphertext contributes to `data_uploaded`. |
| Copy/removal/reachability | Normal Room removal closes references, updates the catalog and removes only validated unreachable candidates; the surviving copy remains verifiable. Normal reconciliation reports agent reference reachability. No automatic object/snapshot garbage collection is claimed. |
| Normal portable export | The regular JAT export completes with an additional separately versioned encrypted `.agents.age` companion. Agent state is not placed in a Hauler/PCC component slot. Both native formats verify and restore from the companion. |
| Cross-Dimension Copy | A fresh disposable bucket is explicitly enrolled with fresh recovery/operational identities in the isolated Secret Service. Normal Copy re-encrypts both native archives under the destination domain; physical binding/domain/ciphertext differ and original provenance remains distinct. Source identities or credential values are not copied. |

The reproducible tests are in
[`tests/test_native_agent_reference_live.py`](../tests/test_native_agent_reference_live.py).
Recorded runs:

- Lifecycle/cold Room/copy/removal: initial **1 passed in 62.76s**; the final
  live-catalog deletion guard was requalified with **1 passed in 61.85s**.
- Normal export/companion verification/restoration: **1 passed in 36.89s**.
- Cross-Dimension rewrap/cold Room/native restore/source-removal independence:
  **1 passed in 50.91s**.

Each native archive retains synthetic UUID
`11111111-1111-4111-8111-111111111111`. Restoration writes native file bytes, not
an imported T3 ID or a transcript summary. No provider process is resumed.

Historical identity is authenticated by the remote logical record and sidecar.
The restored workspace uses its current path-bound hydration marker and fresh
stat baseline; the trial does not assert that the old filesystem stat signature
can be reproduced on a new materialization. It never forges or rewrites a marker
to satisfy recovery.

## Installed registered command trial

A locally packaged VSIX was installed into disposable `--user-data-dir` and
`--extensions-dir` directories. All canonical Python modules and extension
source were byte-identical in source, package and installed extension. Package
JSON was compared semantically with only installation-generated `__metadata`
excluded.

Final VSIX SHA-256:
`29a6985a862354252d649391ff4297e2bb7d513ac27498a05dfadd70c3c366a9`.

This final package includes bounded metadata-error redaction, live-catalog
deletion protection and the exact physical-context admission-digest check. After
each guard landed, package/installed/source parity and all three registered
commands were repeated against a new owned capture point and fresh restore
profile. The final digest-check trial used actual MinIO and native Secret Service
for capture, verification and byte-identical restoration. The lifecycle/removal
case was additionally repeated for the deletion guard. Earlier export and
cross-Dimension trials were retained; their valid-data behavior was unchanged
and was not repeated.

The trial executed `vscode.commands.executeCommand` against actual registered
commands with exact logical-JAT row identities obtained from real CLI snapshots:

| Registered command | Result |
| --- | --- |
| `joshRoom.agentRecoveryStatus` | `inspected`; rendered a pinned historical-point dialog showing two encrypted native-history references and the unverified-continuation/Save exclusion labels. |
| `joshRoom.captureAgentRecovery` | `captured`; real native format picker and exact-point confirmation, followed by real sidecar publication. |
| `joshRoom.restoreAgentRecovery` | `restored`; actual verification, UUID-only picker and one-file/no-overwrite/no-model confirmation, followed by a byte-identical Claude native file in a fresh explicitly authorized profile. |

Status deliberately selects the original historical point containing **two**
references. Capture and restore select a separate new point containing the
single newly captured Claude reference. The repeated final trials retain this
distinction; the two-reference status is not attributed to the new capture point.

The installed extension's existing `__test__.setRuntimeForTests` hook selected
the project Python and **installed controller source** in existing
`local-build-fallback` mode. No new production hook was added. This is a disclosed
source-controller runtime override, **not** qualification of a newly built
immutable RCC controller artifact. Public age executables were supplied in the
disposable Python runtime prefix, with verified source/destination bytes.

The native workbench ran on a task-private Xvfb display with a fresh local X
authority, no TCP X server, and an isolated VS Code profile. Built-in custom
dialog style made the native workbench dialogs observable; a local CDP endpoint
was used only to inspect/click this task's window. Real dialogs, pickers and
confirmations were observed, not replaced with mocked prompt results. Native
success notifications were dismissed through the built-in notification command
so the awaited registered callbacks could finish. The fixture setup updated only
the explicit nonsecret host grant and called the normal `agents configure`
command before the fresh restore.

The owned VS Code and Xvfb processes were stopped after the trial. Screens,
profiles, drivers, logs and fixture state stay in task scratch until final task
cleanup; no raw keyring, identity, provider credential or native transcript is
included in this document.

## Runtime provenance and initial failures

The portable-export trial uses the published JAT `v0.1.18` dependency with a
privately acquired runtime and source-controller override:

- RCC `v18.19.11`, binary SHA-256
  `4d68609044d3a39a1728d571d54c77a6174215adb40eb9acd69ce5ad34a6db1f`.
- JAT archive SHA-256
  `d812f17c2579120361bf6c73b81656095aab59e90c86a159c40c950149a9aa13`;
  acquired artifact
  `sha256:c4ba60ca02c0cfc3e73c07e420039c713d0e6a9d1d42a8bfaeac14388f5c5424`.
- JAT source commit `5437ce5fff658dc2f74d2db6bc3a72079b0cad37`, source archive
  SHA-256 `8cf047cbdd7d41b8778f14997805e737fab057261c21be1617350b3c3630e75d`.
- age `v1.3.2`, SHA-256
  `56acaf7aabcf0f750f2be90a540f0e3e83293ecbff6c390676cba7910b9ce463`;
  age-keygen `v1.3.2`, SHA-256
  `db258d0533b010c3325e1271b8aba7260ad886c34742607d5668812f0becc859`.

Only verified **public** RCC/JAT/source inputs were copied or downloaded into
fresh task storage. No existing RCC home, provider state, keyring data or JAT
checkout was reused or modified.

Initial failures were retained rather than counted as passes:

1. Outer restore admission omitted the encrypted source destination and was
   refused before writing a native file. The source was corrected and re-tested.
2. A profile containing another provider's files was refused as non-fresh. The
   trial retained that safety boundary and used separately explicit fresh
   targets; it does not claim multi-file/home hydration.
3. Normal cold Room hydration produced a new stat baseline. The initial restore
   refused it; the corrected exact-record/current-hydration-baseline path passed
   without marker manipulation.
4. Plain fallback export had no runnable JAT robot at its default path and
   failed composition. The final trial explicitly acquired the pinned managed
   JAT dependency in private task storage; it does not promote that fallback
   failure to a pass. That first attempt created an empty default JAT directory
   and empty `output` child outside task scratch. Birth timestamps corroborated
   creation by this trial; only those two proven empty directories were removed
   with `rmdir`. Existing parent directories and user files were preserved.

## Reproduce

Create a fresh isolated provider/keyring fixture using the nonsecret recipe;
never point these opt-in tests at an existing bucket or keyring. Set
`JOSH_ROOM_TEST_REFERENCE_ROOT` to that fixture. Run on the Linux host from the
source checkout, with a fresh private disk-backed pytest base:

```sh
.venv/bin/pytest -q tests/test_native_agent_reference_live.py \
  -k cold_room --basetemp="$TASK_ROOT/pytest-lifecycle"
```

For normal export, set `JOSH_ROOM_TEST_REFERENCE_JAT_RUNTIME` to the private
JSON receipt returned by the existing runtime acquisition helpers, then select
`-k normal_export`. The receipt names the verified JAT artifact, source and
private RCC home; it contains no provider credential values.

Cross-Dimension qualification additionally requires the explicit
`JOSH_ROOM_TEST_REFERENCE_CROSS_DIMENSION=1` flag and selection
`-k cross_dimension`. It creates only fresh disposable synthetic storage.
Without fixture/runtime opt-in, these cases skip rather than touch a provider.

Every Save selector remains false. Publication, release, merge, existing
provider login and paid/model calls remain outside this acceptance.
