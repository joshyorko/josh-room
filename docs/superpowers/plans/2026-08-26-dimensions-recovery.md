# Josh Room Dimensions Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconstruct the Provider -> Dimension -> Room -> JAT experience from
the issue #17 contract without relying on the lost candidate or weakening the
existing storage, encryption, credential, and Environment Artifact boundaries.

**Architecture:** Add named, secret-free Dimension records above the existing
concrete R2 and MinIO backends, with one encrypted catalog per Dimension.
Versioned catalog and workspace-marker evidence establishes authoritative local
state; later native and copy flows consume that evidence without introducing a
provider framework or decrypt/re-encrypt path.

**Tech Stack:** Python 3.11+, pytest, Ruff, JSON Schema 2020-12, age, JAT/Hauler,
RCC v18.19.2, boto3 S3 APIs, VS Code extension JavaScript, DevPod, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-26-dimensions-recovery-design.md`

## Global Constraints

- Start from `main@d44d4cbb908a3637da5456bf788646685131a58b` on
  `patchraptor/coherent-dimensions-rollout-rebuild`.
- Treat lost candidate `4efc672373557d00169e300ec4760ec6e11b502b` as
  historical requirements evidence only.
- Keep Cloudflare R2 and MinIO concrete and preserve the ObjectStore and age
  boundaries; do not add a generic provider framework or shared catalog.
- Preserve top-level `r2` and `minio` configuration compatibility.
- Store no access key, secret key, session token, age identity, or absolute
  private workspace path in Dimension records, catalogs, or markers.
- Use SHA-256 and byte size for integrity; never use an ETag as content identity.
- Keep writes create-only/conditional and publish catalog state only after
  immutable object verification.
- Preserve typed RCC Environment Artifact metadata and `--no-build` behavior.
- Begin every implementation slice with a focused failing test and record the
  exact RED and GREEN commands on the branch/PR receipt.

---

### Task 1: Recovery contract and schemas

**Files:**
- Create: `docs/superpowers/specs/2026-08-26-dimensions-recovery-design.md`
- Create: `docs/superpowers/plans/2026-08-26-dimensions-recovery.md`
- Modify: `src/josh_room/config.py`
- Modify: `schemas/private-config.schema.json`
- Create: `schemas/dimension-catalog-v2.schema.json`
- Create: `schemas/workspace-marker-v2.schema.json`
- Modify: `tests/test_config.py`
- Create: `tests/test_dimension_schemas.py`

**Interfaces:**
- Produces `DimensionConfig.from_private()`, `DimensionConfig.to_private()`,
  and `dimension_configs()`.
- Produces declarative v2 catalog and marker contracts; operations continue
  writing legacy formats until Tasks 2 and 3 migrate them.

- [ ] Add a failing config test proving legacy top-level R2 is normalized as a
  Dimension and inline secret fields cannot serialize.
- [ ] Implement the smallest concrete R2/MinIO Dimension value contract.
- [ ] Add failing tests for named private config, catalog v2, and marker v2
  schemas, then add valid JSON schemas to pass them.
- [ ] Run `uv run --group dev python -m pytest tests/test_config.py tests/test_dimension_schemas.py tests/test_snapshot_core.py -q`.
- [ ] Run Ruff on touched Python, parse all schemas with Python's JSON module,
  and run `git diff --check`.

### Task 2: Dimension registry and independent catalog routing

**Files:**
- Modify: `src/josh_room/config.py`
- Modify: `src/josh_room/cli.py`
- Modify: `src/josh_room/catalog.py`
- Modify: `src/josh_room/r2.py`
- Modify: `src/josh_room/minio.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli_contract.py`
- Modify: `tests/test_r2_backend.py`
- Modify: `tests/test_minio_backend.py`

**Interfaces:**
- Produces selected-Dimension resolution to an existing concrete backend.
- Produces one catalog location and revision stream per Dimension.

- [ ] Add RED tests for explicit named selection, legacy defaults, missing
  Dimensions, independent buckets/catalog keys, and provider-option rejection.
- [ ] Route R2 and MinIO constructors from `DimensionConfig` without changing
  keyring lookup or age encryption.
- [ ] Bind new catalog v2 bodies to `dimension_id` while retaining a tested v1
  read/migration path.
- [ ] Run focused config, CLI, catalog, R2, and MinIO tests to GREEN.

### Task 3: Marker v2, fingerprint, status, Link, and Repair

**Files:**
- Modify: `src/josh_room/operations.py`
- Modify: `src/josh_room/cli.py`
- Create: `src/josh_room/workspace_state.py`
- Modify: `tests/test_hydration_safety.py`
- Create: `tests/test_workspace_state.py`
- Modify: `tests/test_cli_contract.py`

**Interfaces:**
- Produces canonical path SHA-256, deterministic workspace fingerprint, v2
  marker read/write, auth-free status, storage-backed Link, and fail-closed
  Repair.
- Consumes Dimension catalog v2 and selected concrete backend from Task 2.

- [ ] Add RED tests for copied/stale path detection, catalog mismatch, changed
  files, valid Link, ledger-independent Repair, and unavailable auth.
- [ ] Compute fingerprints with deterministic path ordering and explicit noise
  exclusions; never include Josh Room bookkeeping.
- [ ] Require catalog snapshot ID, Dimension ID, Room ID, ciphertext evidence,
  and workspace fingerprint to corroborate before Link/Repair succeeds.
- [ ] Run workspace, hydration, CLI, R2, and MinIO focused tests to GREEN.

### Task 4: Native saved/dirty state machine

**Files:**
- Modify: `vscode-extension/dirty.js`
- Modify: `vscode-extension/dirty.test.js`
- Modify: `vscode-extension/extension.js`
- Modify the byte-identical counterparts under `templates/room/vscode-extension/`.

**Interfaces:**
- Produces separate disk-baseline and unsaved-buffer signals with replay-safe
  asynchronous initialization.
- Consumes auth-free workspace status from Task 3.

- [ ] Add RED native tests for pre-start changes, create/change/delete/rename,
  ignored files, events during baseline load, dirty buffers, and reset timing.
- [ ] Implement deterministic event accumulation and combine disk/buffer state
  only at presentation time.
- [ ] Reset baseline only after verified Save, Enter, Link, or Repair results.
- [ ] Run all native tests and root/template byte-parity checks to GREEN.

### Task 5: Native Provider and Dimension experience

**Files:**
- Modify: `vscode-extension/registry.js`
- Modify: `vscode-extension/registry.test.js`
- Modify: `vscode-extension/extension.js`
- Modify: `vscode-extension/package.json`
- Modify the byte-identical counterparts under `templates/room/vscode-extension/`.

**Interfaces:**
- Produces Provider -> Dimension -> Room -> JAT tree nodes, native Dimension
  settings, selected routing, progress, and error presentation.

- [ ] Add RED tests for friendly labels plus provider/bucket diagnostics and
  selected-Dimension Save/Enter/Link/Repair/Delete/Serve arguments.
- [ ] Add native Add/Open Dimension flows that never request plaintext secrets
  or require manual JSON/keyring/Kubernetes commands.
- [ ] Route commands through existing CLI contracts and preserve R2-only OAuth.
- [ ] Run native tests, JavaScript syntax, manifest checks, and parity to GREEN.

### Task 6: Copy as New and folder drop

**Files:**
- Modify: `src/josh_room/object_store.py`
- Modify: `src/josh_room/operations.py`
- Modify: `src/josh_room/cli.py`
- Modify: `vscode-extension/registry.js`
- Modify: `vscode-extension/extension.js`
- Add focused Python/native copy tests and template counterparts.

**Interfaces:**
- Produces create-only verified ciphertext transfer and conditional destination
  catalog promotion with an explicit orphan receipt on conflict.

- [ ] Add RED tests proving new logical ID/time, unchanged source, verified
  reuse, one-pass transfer, no decrypt/encrypt/JAT Build, and conflict orphaning.
- [ ] Use existing ObjectStore file transfer primitives and validate SHA-256 and
  size before the destination catalog mutation.
- [ ] Add native JAT-to-Dimension, JAT-to-Room, and folder-drop routing with
  progress and actionable failures.
- [ ] Run focused copy, object-store, native drag/drop, and parity tests GREEN.

### Task 7: Integration and exact-head acceptance

**Files:**
- Modify only when a verification failure demonstrates a scoped defect.

**Interfaces:**
- Produces reproducible PR receipts and exact-head acceptance evidence.

- [ ] Run the full Python suite, Ruff, all native tests, JavaScript syntax,
  root/template parity, repository-boundary tests, JSON checks, and classified
  tracked-secret scan.
- [ ] Run private MinIO put/get/list/delete with existing scoped authority.
- [ ] In fresh DevPod A, Save a synthetic RCC workspace with typed Environment
  Artifact metadata and prove no dependency rebuild, then destroy A.
- [ ] In genuinely fresh DevPod B, Enter under `--no-build`, verify byte-identical
  restore, status, JAT Doctor, and no dependency rebuild.
- [ ] Require independent whole-branch review with no unresolved Critical or
  Important findings, then verify hosted CI and template publication on the
  exact pushed SHA before marking the PR ready.
