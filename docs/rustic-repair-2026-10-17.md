# Rustic repair follow-up

**Readiness: blocked.** The adapter overhead is repaired in part; the official
Windows engine still loses read-only metadata. Restic 0.19.1 remains default.
This work continues PR #108 on `qualify/rustic-after-0.1.36-20261016`; it does
not merge or change that PR branch, main, runtime pins, the release lock or
staged 0.1.36. All test storage and authority are generated fixtures.

Inspected main remains `19723c71d64f81bd664cf34491d13014c213773d` and the
original PR head remains `ce4bca3c4e491fea7247df9572a32966d26f740c`.
The repaired implementation is
[`820e3b4`](https://github.com/joshyorko/josh-room/commit/820e3b4747a74a1cf7aaec1a3d0c4c8eb45df47c).
The [earlier qualification](rustic-production-readiness.md) and its failed
runs are retained. Current receipts are in
[`evidence/rustic-repair-2026-10-17/`](evidence/rustic-repair-2026-10-17/).

## What was repaired

Cold exact inventory originally launched one `cat tree-blob` per tree and
repeated scrypt for every invocation. A real native startup probe measured
median password reads at 482.6ms and the supported direct-key reads at 8.3ms
on a small repository. The adapter now acquires the native master key once
after a cold tree-cache miss and reuses `RUSTIC_KEY` only inside that open
adapter context. This uses the engine's supported credential input; it does
not change encryption, key derivation parameters or repository format.

The key never enters argv, profiles, logs, receipts or disk caches. Host key
variables remain excluded. Owned mutable buffers are cleared on close,
explicit reopen, failed command entry, spawn failure and cancellation. Python
and OS copies of temporary strings are not guaranteed to be zeroized.
Independent review found missing early-failure cleanup; two failing regression
cases now pass, and actual native child cancellation primes a real key first.

Tree SHA-256 authentication, private-cache verification, output/entry bounds,
diagnostic rejection, check-without-cache behavior and refusal of confirmed
Rustic destructive maintenance remain in force. Restic behavior is unchanged.

## Current readiness matrix

| Contract | Linux x64 | Native Windows x64 |
| --- | --- | --- |
| Installer, private paths/ACLs, owned process-tree cancellation | PASS | PASS |
| Official-engine read-only capture/restore | PASS | **FAIL**, 0444 becomes 0666; snapshot mode absent |
| Same-size restored-mtime changes and relocated writable-file restore | PASS | PASS for independent raw-engine fixture; product remains blocked |
| Installed managed age and portable JAT, writable-file fixture | PASS | PASS for that fixture only |
| Complete installed Git recovery | PASS | **FAIL**, 21 mode mismatches; zero byte/missing/extra mismatches |
| Per-operation custom CA | Fresh narrow TLS contract PASS; provider acceptance unavailable | Unsupported in pinned backend; pre-spawn rejection PASS |
| Real production-adapter 100k workload | Correctness PASS; performance still blocked | NOT RUN |
| Concurrent mixed-engine destructive maintenance | BLOCKED; no distributed coordinator | BLOCKED |
| Live R2 / production MinIO / Windows S3 | NOT RUN | NOT RUN |

The Windows managed job is deliberately FAIL. Its public product guard is
checked, then explicit owned diagnostic fixtures exercise the raw adapter.
The independent writable-file fixture covers changed Save, preserved source,
two-recipient age catalogs, relocated restore and actual portable JAT export
and clean-room restore. The complete Git fixture retains its strict mode
assertions. A writable-file pass cannot establish read-only or Git fidelity.

## Comparable 100k production benchmarks

Both complete runs passed all restored bytes, inventories, full-data checks,
snapshot counts and 1,000 same-size edits with restored mtimes across all 250
directories. All four sequences have identical initial and changed corpus
hashes. Each has 100,000 1-KiB files. These are real `RoomStoreOperations`,
`ResticStore` and `RusticStore` calls with encrypted local format-2 repositories;
only catalog/keyset callbacks are synthetic. No shim benchmark is substituted.

The baseline imports clean `cc44dba`; the repaired run imports clean `820e3b4`.
Both use identical committed instrumentation and the same operations-fixture
function. The instrument runs from an external copy for the baseline so its
production checkout stays unchanged. Receipts retain actual source hashes.
Both run on this host's workspace overlayfs, sequentially, Restic then Rustic.
Cold means empty application caches; OS page caches are not evicted.

| Seconds | Before Restic | Before Rustic | Repaired Restic | Repaired Rustic |
| --- | ---: | ---: | ---: | ---: |
| Metadata scan, first | 1.892 | 4.359 | 1.564 | 1.629 |
| Metadata scan, repeat | 1.812 | 3.645 | 1.676 | 1.602 |
| Initial Save | 24.141 | 186.602 | 22.368 | 64.926 |
| Direct warm engine scan | 5.249 | 8.658 | 3.138 | 4.927 |
| Unchanged Save | 3.677 | 3.469 | 3.174 | 3.736 |
| Changed Save, all 250 directories | 29.020 | 165.129 | 20.699 | 50.694 |
| Cold inventory | 6.911 | 132.072 | 5.207 | 31.921 |
| Warm inventory | 0.345 | 0.432 | 0.354 | 0.629 |
| Staged restore | 23.299 | 12.298 | 39.929 | 10.904 |

Controls show host/cache variation, especially restore, so no restore speedup
is attributed to this patch. The command profile supports the authentication
finding: baseline cold tree reads consumed 129.7s; repaired reads consumed
28.9s plus one 0.386s key acquisition. Warm authenticated tree-cache traversal
took 1.313s in the repaired run.

This is not sufficient performance acceptance. Repaired changed Save is still
about 2.45 times the paired Restic result and cold inventory about 6.13 times.
The remaining 251 tree commands occupy 28.9 of 31.9 seconds. Source inspection
shows each CLI call opens an indexed repository. The official `ls --json`
returns names only, and long text loses metadata. Exact bulk metadata requires
an engine CLI change. The current aggregate 4-MiB wide-tree limit also remains.

## Engine changes required

The [upstream boundary and patch artifacts](patches/README.md) are reviewable
recommendations. They are not a maintained fork, replacement binary or product
runtime pin, and no upstream contact occurred.

1. `rustic_core` 0.13.0 Windows capture must record the supported read-only
   attribute in Go-compatible mode bits. Its destination setter must apply
   that attribute and expose its permission-error variant on Windows. Missing
   old modes cannot be reconstructed. This does not provide NTFS DACL or
   alternate-stream preservation. Full CLI metadata ordering and full managed
   recovery must pass before adopting any patched engine.
2. A bulk exact-node JSON stream is needed to remove per-tree process/index
   overhead without losing modes and link targets. The supplied CLI patch
   applies to v0.11.4 source, but its compilation, native behavior and 100k
   benefit are **NOT RUN**. A future adapter must retain snapshot binding,
   per-event and total-entry bounds, path validation, strict diagnostics and
   complete-process acceptance.
3. Windows custom CA requires a per-operator OpenDAL HTTP client with explicitly
   added validated roots. The pinned backend exposes no such option. The
   recommendation identifies the matching `OperationContext`/transport API;
   OS trust and process-global transport must remain unchanged.

The source-only Windows proof uses identical unit assertions against baseline
and patched verified crates, separately compiled. Its receipt is distinct from
official-product evidence. Earlier failed probes retain their CRLF and shared
Cargo-target mistakes; those outcomes were not relabeled as passing.
The final native proof passed: the original core fails both assertions and
the recommended core patch passes both, using rustc 1.99.0. This establishes
the tested file-mode repair at the core boundary, not full CLI qualification.

## Verification and limits

- Native source `820e3b4`, workflow
  [39032863831](https://github.com/joshyorko/josh-room/actions/runs/39032863831):
  Linux 13 passed / 5 skipped; Windows 1 failed / 12 passed / 5 skipped.
  The Windows failure is the unchanged strict read-only assertion.
- The same workflow's installed Linux consumer passes complete Git/JAT/age
  recovery plus the independent writable-file chain. Its installed Windows
  consumer passes the writable chain and fails complete Git modes.
- Focused adapter/runtime/probe regressions: 63 passed. The early-key cleanup
  tests were observed failing first. The strengthened real cancellation cases
  passed locally and run again in native CI. Restic/operations/inventory scope
  at the earlier repair checkpoint had 167 passed / 1 skipped.
- Node extension tests: 304 passed / 2 skipped. Source parity, Ruff, module
  compilation, whitespace checks and Gitleaks 8.30.1 passed.
- The owned Linux TLS fixture reaches missing-config HTTP only with its scoped
  CA. Missing/wrong CA and hostname mismatch reach no HTTP request before the
  deadline. Those negative results are bounded timeouts, not inspected native
  certificate error messages. Its initial missing-HEAD fixture failure is
  retained. This is not complete S3 Save/restore or live-provider evidence.
- Broad Python source `820e3b4`: **FAIL**, 2,011 passed / 16 failed / 13 skipped,
  2 private acceptance tests deselected, 217.58 seconds. All 16 failed nodes
  reproduced on exact inspected main in a separate worktree under the same
  final environment. The exact failed node sets match. Their names are retained
  in `verification.json`; detached JAT descendant cleanup is one of them.
- Native upstream proof: **PASS** for its two core assertions only. It retains
  independent source hashes, compiler version, canonical patch digest and the
  workflow artifact digest. Full patched CLI recovery remains NOT RUN.

There is no authorization here for a fork product, a merge, release publication,
Josh's machine installation, real provider storage or credentials. Mixed-engine
destructive maintenance still requires all writers stopped on all hosts and
remains unqualified for concurrency. Existing broad-suite baseline failures,
including detached JAT cleanup, remain separate release gates.

The review deferred one minor workflow limitation: a `docs/**`-only patch edit
does not trigger this isolated workflow automatically and needs explicit
dispatch. Current code/script commits triggered the recorded runs normally.
