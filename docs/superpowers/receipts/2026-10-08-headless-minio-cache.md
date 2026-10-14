# Headless MinIO Room Store cache correction

## Observed failure

The installed VS Code remote extension host in a Wolfi development container reached
`room-store-keyset` and failed with `RuntimeError` on 2026-10-08. Its native backend
reported `available: false`, `reason: helper-missing`, and `secret-tool-missing`.
The disposable loopback MinIO held a v2 keyset at generation 1 without a repository
binding. There was no snapshot or successful workspace marker. Preview, encryption
setup, provider connection, and controller/JAT imports had succeeded.

## Contract and fix

MinIO's fixed, validated remote keyset already owns durable Room Store material.
`ensure_room_store_keyset` nevertheless required a native cache write after its
conditional durable upgrade. The error also affected binding, later preview, and
existing-store reads. Native availability is an optimization for this MinIO material,
not a second mandatory custody authority.

A typed `NativeSecretBackendUnavailable` distinguishes unavailable native storage
from validation errors and failures after a backend is available. Only MinIO tolerates
this type. Available native caches are still compared against the remote authority;
write/read errors, malformed scope/material, foreign domains, repository binding,
conditional-write reconciliation, credential and artifact/receipt checks remain fatal.
Existing-store reads now explicitly check the remote encryption domain before opening
Restic, rather than depending on a cache comparison to enforce it indirectly.

Unavailable includes the native probe's locked, denied, or failed states, not just a
missing helper. No plaintext persistent cache is added. The existing protected,
short-lived password handoff and cleanup remain unchanged. R2's broker/native custody
contract remains required and fails closed.

## Verification

- Fresh v1 and existing v2 keyset/Save tests were RED at the native cache call, then GREEN.
- A real age/Restic vertical with an in-process S3 adapter was RED on subsequent preview,
  then GREEN across first Save, no-op Save, incremental Save, preview and hydration.
- A wrong-domain headless existing-store test was RED before the explicit check, then GREEN.
- Focused Python: 112 passed, 3 skipped (cold RCC capture/other conditional integration cases).
- Node extension: 276 passed, 2 skipped; scripts/OMP: 2 passed.
- Full Python run: 1739 passed, 11 skipped, 16 failed. All 16 failures reproduced on the
  unchanged 93fe522 source archive with the same interpreter/environment. They concern
  PCC executable/config trust and detached process cleanup. A final full rerun is recorded
  in the PR update; no claim of a green full suite is made.
- Ruff, diff whitespace, canonical/bundle/template/package byte parity, and VSIX zip
  integrity passed.

The package is version 0.1.29, SHA-256
`743ffe0b767243f2f8bb5620e0fcec4dede9741dee50e3ca252b06516bcdb721`.
Build: `npm --prefix vscode-extension run package -- --out <candidate.vsix>`.
Runtime artifact pins are unchanged. No merge, release, deployment, or production
provider writes occurred. The installed remote extension-host/real MinIO retest is
pending separately; these automated results are not a GUI acceptance claim.

## Baseline failures

```text
FAILED tests/test_cli_contract.py::test_doctor_json_is_stable - KeyError: 'in...
FAILED tests/test_cli_contract.py::test_extension_doctor_uses_managed_rcc_and_jat_doctor_not_host_tools
FAILED tests/test_jat.py::test_jat_sigterm_kills_detached_descendant_that_holds_output_pipes
FAILED tests/test_minio_backend.py::test_doctor_probes_selected_backend_and_oauth_is_r2_only
FAILED tests/test_minio_encryption_flow.py::test_doctor_surfaces_minio_encryption_state_before_catalog_decrypt[backend0-legacy-legacy-encryption-migration-required]
FAILED tests/test_minio_encryption_flow.py::test_doctor_surfaces_minio_encryption_state_before_catalog_decrypt[backend1-uninitialized-encryption-initialization-required]
FAILED tests/test_minio_encryption_flow.py::test_doctor_surfaces_minio_encryption_state_before_catalog_decrypt[backend2-failed-encryption-domain-mismatch]
FAILED tests/test_pcc_hooks.py::test_remove_without_owned_hooks_is_idempotent
FAILED tests/test_pcc_hooks.py::test_install_receipt_failure_rolls_back_and_no_final_lf_is_exact
FAILED tests/test_pcc_hooks.py::test_install_preserves_unrelated_hooks_and_remove_rolls_back
FAILED tests/test_pcc_hooks.py::test_status_detects_stale_runtime_manifest - ...
FAILED tests/test_pcc_hooks.py::test_stale_repair_and_command_windows_contract
FAILED tests/test_pcc_hooks.py::test_unowned_same_command_conflicts_with_install
FAILED tests/test_pcc_hooks.py::test_remove_refuses_unowned_duplicate_canonical_command
FAILED tests/test_pr18_backend_repair.py::test_doctor_dimension_minio_inspects_named_minio_backend
FAILED tests/test_pr18_backend_repair.py::test_doctor_explicit_minio_uses_minio_check_label_and_provider
```
