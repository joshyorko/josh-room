# Save failure stage evidence — draft review only

Base and live main: `032f9129c1d66b1cb402b84c0ad19968bf1aa9b2`.
Branch: `fix/save-controller-stage-diagnostics`.
One code-writing owner used the saved Josh Room cloud workspace. No other
worker, production source workspace, production MinIO/R2, merge, release, tag,
deployment, or downstream pin change was used. `AGENTS.md` and the exact
candidate release workflow/verifier were read. The workspace `.agents` directory
is empty and the checkout has no additional `.agents/skills` files.

## Finding and scope

The broad native Save exception handler dropped all evidence for unexpected
Python exceptions. A deterministic component-preparation fault under the real
pinned controller reproduced the reported outer shape on exact main: RCC exit
2 with only `{"error":"save-failed","ok":false}`. The cause in this controlled
experiment was an injected `FileExistsError`; it is **not** the established
cause of the operator's live regression.

The fix keeps the error code and emits only the last entered program-owned Save
stage, an allowlisted built-in cause type, and a fixed diagnostic. Custom class
names and exception text are never formatted. Known errors retain their existing
validation, cancellation, confirmation, and publication receipts. RCC
preparation still precedes keyset or Restic mutations. Canonical, bundled, and
template controllers match byte for byte.

The reported live Save regression remains unresolved pending a sanitized
failure receipt from the failing installed environment. This draft does not
claim that diagnostic instrumentation restores live Save or establishes its
root cause. No artifact repin or credential-validation bypass was made.

## Runtime and packaged evidence

The retained controller artifact
`sha256:136ac1121dc14b63333276c571e3712f3bc6c5eb16f497764200ea5bcfa8511a`
loads the explicitly supplied bundled controller before its installed package.
A direct RCC probe reported the bundled bridge and component module paths and
extension version `0.1.29`. The same check with the extracted review VSIX loaded
its controller files. The saved development wrapper includes `src` before the
controller; the packaged probes instead used only the extracted controller as
the application `PYTHONPATH`. Test helpers and pytest were appended to the probe
script's import path solely for fixture construction.

The extracted review VSIX's CLI, running under the real controller artifact in
a fresh isolated private RCC home, returned this bounded receipt for the same
injected fault:

```json
{"cause_type":"FileExistsError","diagnostic":"Save failed at rcc-component-prepare (FileExistsError)","error":"save-failed","ok":false,"stage":"rcc-component-prepare"}
```

Its RCC receipt matched the selected artifact and exit status 2. Only
allowlisted artifact/exit summaries were retained; raw RCC receipts were removed.
No success receipt, workspace snapshot, or catalog was published for the fault.

Disposable no-robot and realistic pinned-robot fixtures passed Preview and Save
with real age, RCC, and Restic, first with an in-memory S3 fixture/local Restic,
then with a loopback Moto 5.2.3 S3 emulator and real S3 Restic. The latter also
passed from the extracted review VSIX. Native RCC component capture ran for the
robot fixture; no nested JAT capture task is inferred from controller startup.
JAT's pinned freeze and helper files supplied the realistic environment fixture.
Provider credentials and native secret caching were synthetic/in-memory seams.
These are controller/API probes, not installed remote extension-host acceptance.

A minimal unpinned conda robot passed Preview but failed specifically at
`rcc env publish` with exit 1 when micromamba encountered a sandbox read-only
path. Provider state remained unchanged. This host/build failure does not prove
the production failure. It was not bypassed or repinned.

| Artifact | SHA-256 / digest |
| --- | --- |
| Review VSIX, 490,428 bytes, version unchanged at 0.1.29 | `7b3a1400dbb57c31f2129ab48e8d57724e8bc841348d11f90fc5c4aa684c7be5` |
| Retained controller RCCA archive | `3dd7d542b2fdd35633152a400e30d7c6bb861c5bb128f6e22ef6bdbc3b048f88` |
| Retained JAT RCCA archive | `eb5a2f5634b6bc78a66bc070493f7d7df81a775cfe421160b8b1ac34c2589d5f` |
| JAT artifact digest | `sha256:ae6b5802103c780de49b42768885a8c66073747272caf759784e9eb23f44e4f1` |
| Configured optional secure Room image; not pulled or executed | `sha256:5489eef4c4302cdb9ac6cff79ce0664511f7cfc8c31fd83ea59d9fa94ea5be25` |

The VSIX passed ZIP integrity and exact archive/source inventory verification
for all 98 source files, plus canonical/bundled/template parity. The verifier
used a throwaway local checksum record for archive comparison; the tracked
`release-promotion.json` remains unchanged and no promotion is claimed.

## Tests and unavailable gates

- Seven new Python cases were RED on exact main and GREEN after the change.
  They cover fresh/existing stores, unexpected preparation faults, setup/open
  boundaries, custom exception names, zero provider writes on preparation
  failure, and absence of a success receipt.
- Focused Python: 132 passed, 2 integration cases skipped in that invocation.
- Those two real-RCC integration parameters (1 and 2,048 entries) were then
  attempted under the pinned controller with an empty private capture home.
  Both failed at `rcc env publish`, exit 1, with micromamba's sandbox read-only
  filesystem error during a cold environment build. The imported-artifact
  controller/API probes above passed. Cold rebuild acceptance is blocked on
  this host; these failures are reported, not counted as passes or asserted
  to explain the live regression.
- Node extension/scripts/OMP suites: 278 passed, 2 skipped.
- Full Python: 1,735 passed, 10 skipped, 8 failed. All eight failures reproduced
  on exact main in a disposable checkout under the same interpreter:
  `test_jat_sigterm_kills_detached_descendant_that_holds_output_pipes`;
  `test_remove_without_owned_hooks_is_idempotent`;
  `test_install_receipt_failure_rolls_back_and_no_final_lf_is_exact`;
  `test_install_preserves_unrelated_hooks_and_remove_rolls_back`;
  `test_status_detects_stale_runtime_manifest`;
  `test_stale_repair_and_command_windows_contract`;
  `test_unowned_same_command_conflicts_with_install`;
  `test_remove_refuses_unowned_duplicate_canonical_command`.
  PCC failures report `config-untrusted` / `executable-untrusted`; the process
  cleanup test sees a remaining sleeping descendant. They were not changed.
- Ruff, `git diff --check`, VSIX packaging, and Python wheel/sdist build passed.
- Disposable actual MinIO unavailable: pulling
  `minio/minio:RELEASE.2025-04-22T22-12-26Z` returned pull access denied;
  `quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z` returned unauthorized;
  `https://dl.min.io/server/minio/release/linux-amd64/minio` returned HTTP 410.
  No actual MinIO image or binary checksum can be claimed.
- The configured secure image's digest-qualified GHCR manifest lookup returned
  `manifest unknown`. Its digest above is a configuration pin, not verified
  pulled-image acceptance.
- No VS Code, Code Insiders, Xvfb, or `secret-tool` executable is available;
  `keyring.backend_status().available` is false. Native Secret Service, GUI,
  installed remote extension-host Save-to-MinIO, Windows, and live operator
  Save acceptance remain unrun. `managed_runtime_acceptance.js` was not used
  as a substitute for those gates.
- Exact model variant and reasoning effort are not exposed by this executor.

User live Save acceptance and an established live root cause are required
before any later promotion. This deliverable is a draft diagnostic fix for
review, with the operational Save fix still blocked on that evidence.
