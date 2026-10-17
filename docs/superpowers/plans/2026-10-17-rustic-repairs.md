# Rustic repair implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan in the current session. The user has explicitly authorized implementation and pushes to the existing isolated branch.

**Goal:** Repair adapter overhead and establish the exact upstream boundary for Windows recovery fidelity without changing the pinned product or hiding failures.

**Architecture:** Keep the official Rustic 0.11.4 and Restic 0.19.1 pins. Reuse Rustic's supported master-key authentication only inside an open adapter context, retaining SHA-256 tree verification, bounded output, private caches, and destructive-operation refusal. Supply a reviewable upstream Windows patch separately from the shipped runtime.

**Tech stack:** Python adapters, pinned native executables, native GitHub Actions, managed RCC/JAT/age.

**Spec:** Delegated repair request dated 2026-10-17; baseline report `cc44dba` and code `13d6d7f`.

## Global constraints

- Restic remains default; main, PR #108 and staged 0.1.36 stay untouched.
- Only the existing `qualify/rustic-after-0.1.36-20261016` branch is pushed.
- No new PR, release, machine deployment, user storage/credentials or upstream contact.
- Preserve strict read-only assertions, integrity/cache guards and mixed-engine maintenance refusal.
- Keep Linux, native Windows, hypothetical upstream patches and unavailable provider evidence separate.

## Review focus

- Invalid or diagnostic-bearing key output must fail closed without revealing material.
- Host-provided Rustic credentials must not enter child environments.
- Keys must be discarded on close, failed open, reauthentication and cancellation.
- Cache corruption and oversized tree output must still be rejected.
- Windows writable/read-only files and directory modes require native evidence; missing old modes cannot be reconstructed.

### Task 1: Establish Windows engine boundary

- [ ] Recheck pinned source against Cargo.lock and retain strict real-engine failure.
- [ ] Produce an upstream-only patch for Windows capture and supported read-only restoration, with native tests and exact application instructions.
- [ ] Trace scoped CA behavior through the pinned HTTP backend and document a concrete supported change boundary.

### Task 2: Repair repeated derivation overhead

Files: `src/josh_room/rustic_store.py`, `tests/test_rustic_store.py`, real native tests and synced controller copies.

- [ ] Add failing tests for request-scoped key reuse, strict key schema validation, host isolation and cleanup.
- [ ] Add bounded in-memory key acquisition through `cat masterkey`; pass only through child environment using supported `RUSTIC_KEY`.
- [ ] Keep repository identity verification and authenticated tree caches; no fallback after key/auth failures.
- [ ] Run focused contracts and real-engine lifecycle, cancellation, installer and metadata tests.

### Task 3: Reproducible comparison and acceptance

Files: `scripts/rustic_production_benchmark.py`, a rerunnable profiling script, qualification workflow and readiness receipts.

- [ ] Add per-command timings without retaining argv, environments, raw output or private paths.
- [ ] Rebenchmark baseline and repaired adapters on the same deterministic 100k-file / 250-directory corpus and filesystem with all correctness checks.
- [ ] Run native Linux/Windows and managed Linux/Windows acceptance with explicit failures and unrun steps retained.
- [ ] Run broad Python and Node suites, source parity, packaging, lint, whitespace and secret checks.
- [ ] Push code and evidence with remote-head guards and confirm main, original PR and release pins remain unchanged.
