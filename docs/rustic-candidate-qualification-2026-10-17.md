# Disposable engine candidate qualification

This continues the repair request by building and testing the proposed engine
patches. It does not adopt a fork, change a runtime pin, distribute a candidate
binary, publish a release or enable Windows product selection. Restic remains
default; main, PR #108 and staged 0.1.36 remain unchanged.

## Build inputs and reproduction

- Rustic v0.11.4 source commit `6b96f976ff437a0528a4742358b172b32c3e3932`.
- Verified published `rustic_core` 0.13.0 and `rustic_backend` 0.7.0 archives.
- [Exact-node CLI stream](patches/rustic-0.11.4-json-nodes.patch),
  [Windows read-only correction](patches/rustic_core-0.13.0-windows-readonly.patch)
  and [scoped operator CA](patches/rustic_backend-0.7.0-scoped-ca.patch).
- Rust 1.99.0, optimized release profile and upstream `release` feature set.
  Dependency versions and checksums must remain unchanged after local source
  overrides. No compiled candidate is added to the repository or uploaded.

```sh
uv sync --group dev
# A disposable Rust 1.99.0 toolchain must be on PATH.
uv run python scripts/build_rustic_candidate.py --destination /tmp/owned-rustic-candidate
```

The builder supplies `PROJECT_VERSION=v0.11.4`, matching upstream `build.sh`'s
`git describe --tags` convention. A first full Linux build completed in about
13 minutes but used Cargo's plain `0.11.4` version text; the adapter correctly
rejected it. The supported upstream version environment is now reproduced,
without relaxing the exact version assertion. Build receipts record actual
patch, lock, source-file, compiler and binary identities.

## Candidate-only adapter path

`metadata_format="nodes-v1"` and `ca_mode="operator"` are explicit low-level
qualification inputs. Product callers retain the official runtime and existing
defaults. The stream requires an exact snapshot/root/path header, validated
native nodes and a completion count. It rejects duplicate paths, missing parent
directories, cycles, unsupported metadata, malformed/duplicate JSON, truncated
output, diagnostics and nonzero exit. No rows or cache escape a failed process.
Per-event, depth and total-entry bounds remain. Its cache namespace is separate
from the original authenticated-tree inventory.

Scoped CA bundles are bounded and strictly parsed. Roots augment normal
verification through this OpenDAL operator's HTTP context. The patch does not
modify system trust, install a process-global transport or disable certificate
chain or hostname verification.

## Qualification status at this checkpoint

- Exact-node parsing: 23 tests PASS, including process-failure cache rejection.
- Original official CLI: proposed `--json-nodes` flag rejected, exit 2.
- First Linux optimized build: completed; initial version-string mismatch
  retained and corrected through the upstream build convention.
- Native candidate recovery, Windows build, managed consumers and full 100k
  comparison are in progress. Their results must be recorded before claiming
  qualification. The official Windows engine remains FAIL in its separate gate.

The candidate workflow builds native Linux and Windows binaries locally in
disposable runners and retains only receipts and review packaging. It runs the
same strict live assertions, a 15k-entry wide tree, exact blob/stream metadata
parity, generated-CA TLS/S3 fixture and installed managed Git/JAT/age recovery.
The protocol fixture does not verify AWS signatures and does not qualify live
R2 or production MinIO. A passing candidate does not authorize adoption.
