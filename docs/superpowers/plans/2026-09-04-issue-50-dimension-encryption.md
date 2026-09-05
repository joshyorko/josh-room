# Storage Dimension Encryption Domains Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each physical R2 or MinIO bucket a stable encryption domain, let MinIO enroll and read its own age keyset without Cloudflare, preserve existing R2 ciphertext, and rekey legacy MinIO ciphertext without rebuilding or mutating JAT payloads.

**Architecture:** Add a small controller-owned encryption-domain module for strict keyset validation, scoped local material, control-object CAS, and migration journals. Resolve material from the selected Dimension at the CLI boundary; keep provider credentials separate from age material and retain R2’s existing Cloudflare-issued identity as its compatibility path. Extend copy and migration to decrypt only the outer trusted envelope, then make the native extension broker one selected domain through SecretStorage and a bounded temporary handoff.

**Tech Stack:** Python 3.11+, pytest, Ruff, JSON Schema 2020-12, boto3/botocore, managed `age`/`age-keygen`, Node.js `node:test`, VS Code SecretStorage and native progress APIs, RCC-managed controller runtime.

**Spec:** GitHub issue #50, https://github.com/joshyorko/josh-room/issues/50

## Global Constraints

- Start from live `origin/main` at `71cadddd0b6d68839809cd55471d5576d22b522d` and record the exact base/head SHAs in the PR.
- A physical bucket is the canonical encryption boundary; each distinct bucket receives a distinct operational age identity and stable random `encryption_domain_id` plus positive `key_generation`.
- Aliases resolving to the same provider endpoint/account and bucket reuse the same remote keyset or fail deterministically; they never create competing keysets.
- The fixed keyset, migration journal, and catalog are control state; snapshot ciphertext remains immutable content-addressed data.
- The keyset has strict v1 fields/bounds/binding/recipient validation, derives the operational recipient with managed `age-keygen -y`, and never stores recovery private identity.
- MinIO keyset enrollment/read and fresh catalog operations do not request Cloudflare; Cloudflare remains an R2-only authority and existing R2 ciphertext remains readable without a second authorization.
- Legacy migration decrypts and stream-verifies only the existing `snapshot.jroom` envelope and embedded payload digest, re-encrypts exact envelope bytes, never calls JAT Build or Restore, journals unique shared objects, resumes safely, and cuts over the catalog conditionally before any cleanup.
- Same-domain copy may reuse verified ciphertext; cross-domain or cross-generation copy re-encrypts exact envelope bytes and preserves logical/JAT metadata.
- Provider credentials and encryption material use separate scoped SecretStorage/keyring keys and separate bounded `0600` runtime handoff files; private identities never enter config, workspace state, argv, logs, receipts, or Git.
- Native UX uses `"extensionKind": ["workspace"]`, Workspace Trust checks, native tree states/actions, one migration confirmation, cancellable progress, and no webview or implicit auth/key fetch during activation/refresh.
- Preserve root/package/template/controller parity, make no JAT or JAT-format changes, and do not change MinIO/Kamal, DNS, certificates, or the live user bucket.
- No live migration is run until synthetic migration, independent review, and explicit user approval of the production operation exist; secret-gated or unavailable live gates are reported as skipped.
- Before completion or changing the PR from Draft, the installed/managed RCC Josh Room path must perform a real read-only acceptance against the user’s configured non-empty Josh Room MinIO bucket: authenticate through the existing secure credential path, discover the intended bucket/Dimension, enumerate non-zero existing Room/project and chat records, and open/read one existing Room through the user-facing path without creating, overwriting, migrating, rotating, or deleting live objects. Empty dimensions/catalogs, synthetic buckets, or authentication-only results are failures. Report only non-secret counts and status; never reveal Room names, identifiers, chat contents, object keys, credentials, or private data.

---

### Task 1: Domain, keyset, crypto, and control-object contracts

**Files:**
- Create: `src/josh_room/encryption_domain.py`
- Modify: `src/josh_room/crypto.py`
- Modify: `src/josh_room/keyring.py`
- Modify: `src/josh_room/object_store.py`
- Modify: `src/josh_room/r2.py`
- Modify: `src/josh_room/minio.py`
- Modify: `src/josh_room/envelope.py`
- Modify: `src/josh_room/catalog.py`
- Modify: `schemas/private-config.schema.json`
- Create: `tests/test_encryption_domain.py`
- Modify: `tests/test_r2_backend.py`
- Modify: `tests/test_minio_backend.py`
- Modify: `tests/test_snapshot_core.py`

**Interfaces:**
- Produces `EncryptionKeyset`, `EncryptionMaterial`, `KEYSET_CONTROL_KEY`, `MIGRATION_JOURNAL_KEY`, strict keyset parsing/serialization, endpoint transport policy, and scoped keyring helpers.
- Produces `ObjectStore.read_control(key, max_bytes)`, `create_control(key, body)`, and `replace_control(key, body, expected_etag)` with explicit missing/conflict/outcome-unknown behavior.
- Produces `crypto.generate_identity()`, `crypto.derive_recipient()`, and `envelope.verify_envelope_file()` for bounded, read-only envelope verification.
- Extends `Catalog` with optional `encryption_domain_id` while preserving v1 reads and existing R2 data.

- [ ] **Step 1: Write the failing contract tests.** Add tests for two independent keysets, same-bucket alias identity, strict known fields, size bound, provider/bucket binding, random domain ID, positive generation, derived-recipient equality, duplicate-recipient rejection, recovery-private-key exclusion, non-loopback HTTP rejection, and control-key allowlisting.
- [ ] **Step 2: Run the focused RED command.** Run `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python -m pytest tests/test_encryption_domain.py tests/test_r2_backend.py tests/test_minio_backend.py -q`; confirm failures are missing contracts rather than test import errors.
- [ ] **Step 3: Implement the smallest contract.** Keep control keys fixed and opaque, use `IfNoneMatch="*"` and `IfMatch=<etag>`, verify exact read-back, classify `412` and `409` as conditional conflicts, and never include control bodies in exceptions or receipts.
- [ ] **Step 4: Add managed age and envelope seams.** Generate/derive through `_managed_executable`, keep private files mode `0600`, and have `verify_envelope_file()` stream the payload into a digest without writing a second payload copy or invoking JAT.
- [ ] **Step 5: Run GREEN and compatibility checks.** Run the same focused command, then `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev ruff check src/josh_room/encryption_domain.py src/josh_room/crypto.py src/josh_room/keyring.py src/josh_room/object_store.py src/josh_room/r2.py src/josh_room/minio.py src/josh_room/envelope.py src/josh_room/catalog.py tests/test_encryption_domain.py tests/test_r2_backend.py tests/test_minio_backend.py tests/test_snapshot_core.py` and parse `schemas/private-config.schema.json` with Python’s JSON loader.
- [ ] **Step 6: Commit.** Use `git add` on the files above and commit `feat: add storage encryption domain contracts`.

---

### Task 2: Scoped material resolution and Cloudflare-free MinIO operations

**Files:**
- Modify: `src/josh_room/config.py`
- Modify: `src/josh_room/auth.py`
- Modify: `src/josh_room/cli.py`
- Modify: `src/josh_room/operations.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_auth.py`
- Modify: `tests/test_cli_contract.py`
- Modify: `tests/test_dimensions_backend_wave.py`
- Create: `tests/test_minio_encryption_flow.py`
- Synchronize: `vscode-extension/runtime/controller/josh_room/{config.py,auth.py,cli.py,operations.py}`
- Synchronize: `templates/room/vscode-extension/runtime/controller/josh_room/{config.py,auth.py,cli.py,operations.py}`

**Interfaces:**
- Consumes Task 1’s `EncryptionKeyset`, control seam, scoped keyring, and `Catalog.encryption_domain_id`.
- Produces `resolve_encryption_material(dimension, backend, ...)`, `ensure_minio_domain(...)`, `encryption status/initialize` CLI actions, and machine-readable Dimension/encryption-state errors.
- Preserves positional compatibility for existing `create_snapshot`, `hydrate`, `remove_*`, and `serve_snapshot` callers by adding optional selected-material/domain arguments rather than changing old call order.

- [ ] **Step 1: Write RED tests.** Cover fresh MinIO keyset creation with no catalog, 412/409 winner races and losing-key discard, existing keyset read-back, cache domain/generation matching, catalog-without-keyset legacy state, no Cloudflare request/session dependency, fresh save/read paths, and R2 identity compatibility.
- [ ] **Step 2: Run the focused RED command.** Run `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python -m pytest tests/test_minio_encryption_flow.py tests/test_config.py tests/test_auth.py tests/test_cli_contract.py tests/test_dimensions_backend_wave.py -q`; verify failures identify the absent scoped resolution path.
- [ ] **Step 3: Implement selected-Dimension resolution.** Use the remote keyset’s stable domain ID as the cache namespace, accept a verified cache only when domain ID and generation match remote control state, reject duplicate physical aliases deterministically or share the same fetched keyset, and keep top-level `age_identity_profile`/`age_recipients` read-only legacy input for migration only.
- [ ] **Step 4: Route all ordinary encrypted controller operations.** For MinIO resolve its keyset/material before catalog reads and use no `ensure_runtime_session`; for R2 retain `_identity_environment()` and the existing Worker identity contract. Return `legacy-encryption-migration-required` when a catalog exists without a keyset and never auto-migrate during refresh.
- [ ] **Step 5: Add explicit CLI state/actions.** Add parser/dispatch coverage for `encryption status`, `encryption initialize`, `encryption migrate`, and `encryption resume`; make initialization require public recovery recipients or the explicit recovery handoff, and write newly resolved private material only to the designated side channel, never JSON result output.
- [ ] **Step 6: Run GREEN and controller parity checks.** Run the focused command, `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python -m pytest tests/test_minio_encryption_flow.py tests/test_auth.py tests/test_cli_contract.py -q`, and synchronize both packaged controller copies byte-for-byte with canonical `src/josh_room` modules.
- [ ] **Step 7: Commit.** Use `git add` on the scoped controller/config/test files and commit `feat: resolve encryption by storage dimension`.

---

### Task 3: Exact-envelope cross-domain copy and resumable legacy migration

**Files:**
- Modify: `src/josh_room/operations.py`
- Modify: `src/josh_room/cli.py`
- Modify: `tests/test_snapshot_core.py`
- Modify: `tests/test_streaming_snapshot.py`
- Create: `tests/test_encryption_migration.py`
- Create: `tests/fixtures/encryption_migration_manifest.json`
- Synchronize: packaged controller copies of changed modules

**Interfaces:**
- Consumes Task 1’s control-object and envelope-verification seams and Task 2’s `EncryptionMaterial` resolution.
- Produces `plan_encryption_migration(...)`, `migrate_encryption(...)`, journal status/resume semantics, and cross-domain `copy_snapshot_stream(...)` behavior.
- Migration plans expose only Dimension/bucket authority, counts, sizes, generations, revisions, transport state, and journal status; receipts expose no private identity, raw recipients, decrypted manifest, or local path.

- [ ] **Step 1: Write RED migration/copy tests.** Build synthetic envelope bytes with two logical snapshots sharing one ciphertext object and an intentionally JAT-unrestorable payload marker; assert exact payload digest/bytes survive, JAT Build/Restore are not called, shared input is processed once, same-domain copy reuses ciphertext, and cross-domain copy changes only outer ciphertext.
- [ ] **Step 2: Run the focused RED command.** Run `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python -m pytest tests/test_encryption_migration.py tests/test_snapshot_core.py tests/test_streaming_snapshot.py -q`; confirm failures occur at the missing migration/cross-domain branch.
- [ ] **Step 3: Implement the bounded journal and planner.** Store migration ID, domain/generation, source catalog revision, unique source digest/size plan, verified mappings, bounded status/error class, and timestamps through the fixed control object. Allow one active migration, verify journaled destination objects before reuse, and leave the old catalog authoritative before cutover.
- [ ] **Step 4: Implement one-object-at-a-time migration.** Download and digest-check source ciphertext, decrypt only to a private staged envelope, call `verify_envelope_file()`, encrypt the exact envelope bytes to destination recipients, publish create-only and verify destination ciphertext, checkpoint mapping conditionally, and preserve all logical/JAT metadata.
- [ ] **Step 5: Implement cutover and recovery ordering.** Re-read the source catalog revision, conditionally publish the new encrypted catalog, mark the journal committed, reconcile crash-after-commit, retain safe orphans on cancellation/conflict, and do not delete old objects before verified cutover plus MinIO-only read proof.
- [ ] **Step 6: Implement domain-aware copy.** Compare source/destination domain ID and generation; reuse verified immutable bytes only for equality, otherwise verify/decrypt/re-encrypt the envelope without JAT calls and conditionally publish the destination catalog.
- [ ] **Step 7: Run GREEN and mutation checks.** Run the focused command, mutate the migration to call `run_build`/`run_restore`, omit one shared mapping, and force a catalog conflict to confirm the tests fail for each forbidden behavior; then run Ruff on touched files.
- [ ] **Step 8: Commit.** Use `git add` on migration/copy modules, tests, and the synthetic fixture, then commit `feat: migrate and copy encryption domains by envelope`.

---

### Task 4: Native SecretStorage, trust, states, actions, and parity

**Files:**
- Modify: `vscode-extension/extension.js`
- Modify: `vscode-extension/package.json`
- Modify: `vscode-extension/extension.test.js`
- Modify: `vscode-extension/registry.js`
- Modify: `vscode-extension/registry.test.js`
- Modify: `tests/test_repository_boundary.py`
- Modify: `tests/test_native_cli_contract.py`
- Synchronize: all corresponding files under `templates/room/vscode-extension/`

**Interfaces:**
- Consumes Task 2’s explicit encryption actions and private-material side channel and Task 3’s migration plan/progress states.
- Produces provider-credential keys, domain/generation encryption keys, recovery keys, one selected `JOSH_ROOM_ENCRYPTION_MATERIAL` handoff, and native Dimension actions/states.

- [ ] **Step 1: Write RED Node tests.** Cover MinIO connection/hierarchy without `connectCloudflare`, scoped SecretStorage keys, selected-only encryption handoff, cleanup on success/failure/cancellation/timeout, trust blocking/hiding, no auth/key fetch during activation/refresh, native migration confirmation/progress/logs, and recovery export through `showSaveDialog` only.
- [ ] **Step 2: Run the focused RED command.** Run `node --test vscode-extension/extension.test.js vscode-extension/registry.test.js`; verify missing commands/state/handoff behavior rather than harness failures.
- [ ] **Step 3: Implement secure handoff and lifecycle.** Keep provider credentials in the existing provider file, put only selected domain/generation material in an owned mode-`0600` encryption file, remove it in all completion/error/cancel/timeout paths, cache returned material under `josh-room.encryption.v1:<domain>:<generation>`, and invalidate affected readiness/tree state from `SecretStorage.onDidChange`.
- [ ] **Step 4: Replace MinIO Cloudflare fallback.** Make `connectEncryption` invoke MinIO initialization/status, keep Cloudflare browser flow R2-only, add explicit Initialize/Migrate/Resume/Recovery actions, and ensure activation/tree refresh does not perform key retrieval, provider login, network reads, or browser launches.
- [ ] **Step 5: Enforce trust and native state presentation.** Require `workspace.isTrusted` inside Save, Enter, Serve, Link, Repair, migration, recovery import/export, and workspace-reading handlers; represent ready/uninitialized/legacy/resumable/insecure/failed Dimension states with contextual native actions and one modal migration plan confirmation.
- [ ] **Step 6: Set manifest/parity contracts.** Add `"extensionKind": ["workspace"]`, explicit untrusted-workspace capability metadata, command/activation/menu entries, and parity assertions for root/template extension, provider, runtime, package, and every controller Python module. Synchronize copies mechanically and inspect the resulting diff.
- [ ] **Step 7: Run GREEN.** Run `node --test vscode-extension/*.test.js`, `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python -m pytest tests/test_repository_boundary.py tests/test_native_cli_contract.py -q`, and `git diff --check`.
- [ ] **Step 8: Commit.** Use `git add` on root/template extension and parity tests and commit `feat: add native encryption domain controls`.

---

### Task 5: Documentation, acceptance, review, and Draft PR publication

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `docs/MINIO-SETUP.md`
- Modify: `docs/R2-SETUP.md`
- Modify: `docs/architecture.md`
- Modify: `tests/test_repository_boundary.py` only for demonstrated documentation/parity contract gaps
- Create: `docs/superpowers/receipts/2026-09-04-issue-50-encryption-domains.md`

**Interfaces:**
- Consumes all implementation slices and their exact test/runtime evidence.
- Produces public-first operator guidance that names MinIO keyset read access as decryption/enrollment authority, keeps R2 compatibility, documents no automatic bucket administration, and records every gate separately.

- [ ] **Step 1: Write RED documentation-boundary tests.** Assert AGENTS/README/MinIO/R2 guidance names R2 and MinIO as concrete providers, removes the stale sole-R2/no-extension instruction, states MinIO does not need Cloudflare, separates provider credentials from encryption material, and preserves the no-JAT-change/no-secret rules.
- [ ] **Step 2: Run the focused RED command.** Run `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python -m pytest tests/test_repository_boundary.py -q`; confirm failures match stale guidance.
- [ ] **Step 3: Update documentation minimally.** Preserve existing ownership boundaries and add only the issue #50 domain/keyset/migration/trust facts; do not add web UI, generic provider framework, daemon, automatic storage administration, or live migration instructions.
- [ ] **Step 4: Run complete local gates.** Run `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev ruff check src tests`, `UV_CACHE_DIR=/tmp/uv-cache uv run --group dev python -m pytest -q`, `node --test vscode-extension/*.test.js`, `rcc ht vars -r vscode-extension/runtime/controller/robot.yaml --json`, JSON/shell checks, packaged VSIX inspection, root/template/controller parity, tracked secret scan, and `git diff --check`. Classify host-only age failures, missing RCC, and secret-gated R2/MinIO verticals as skipped/unavailable rather than passed.
- [ ] **Step 5: Publish the Draft PR as soon as the first implementation head is independently verified.** After Task 2’s clean task review, push `patchraptor/issue-50-encryption-domains` and open a GitHub Draft PR against `main` before starting Task 3; the PR must remain Draft. Do not merge, tag, release, direct-push `main`, or run live production migration.
- [ ] **Step 6: Obtain the required fresh adversarial review.** Dispatch a read-only Sol High subagent with a fanout checklist covering Python contracts/migration, R2/MinIO authorization, Node trust/handoff, parity/packaging, and all non-negotiable issue #50 criteria. Repair every valid Critical/Important finding through the original writer and obtain a scoped re-review; never mark the PR Ready before this step passes.
- [ ] **Step 7: Run authorized synthetic verticals and final gates.** Keep the Draft PR open while exercising fresh MinIO initialization, cache deletion/re-enrollment, legacy shared-object migration with interrupt/resume and cutover conflict, cross-Dimension copy, and R2 fixture regression; then run the complete local, RCC, VSIX, parity, and secret-scan gates. Before completion or Ready status, run the real installed/managed RCC MinIO acceptance through existing secure credential injection: discover the known non-empty Josh Room bucket, enumerate non-zero existing Room/project and chat records, and open/read one existing Room through the user-facing path. Treat empty/synthetic/auth-only results as failure; perform no live create, overwrite, migrate, rotate, or delete.
- [ ] **Step 8: Write the receipt and finish.** Record base/head, commits, changed modules, keyset/control semantics, migration counts/mappings/orphans/cleanup, payload/JAT proof, fresh MinIO and R2 results, local/remote/Windows/trust gates, skipped/unavailable gates, secret-scan result, Sol routing evidence, PR URL, and the explicit remaining production approval.

---
