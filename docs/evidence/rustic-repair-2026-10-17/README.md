# Repair receipts

Read the [current readiness report](../../rustic-repair-2026-10-17.md) for the
decision and remaining engine boundaries. No receipt implies broad readiness.

| Receipt | Status and scope |
| --- | --- |
| [benchmark-before.json](benchmark-before.json) | PASS correctness; clean baseline `cc44dba`, real 100k production adapters |
| [benchmark-after.json](benchmark-after.json) | PASS correctness; clean repaired `820e3b4`, identical corpus and instrument; performance remains blocked |
| [startup-profile.json](startup-profile.json) | Actual native password/direct-key reads on a small owned repository |
| [native.json](native.json) | Linux PASS, official Windows strict read-only FAIL; native installer/ACL/cancellation checks |
| [managed-linux.json](managed-linux.json) | PASS installed consumer, complete Git/JAT/age and writable-file chain |
| [managed-windows.json](managed-windows.json) | FAIL complete Git fidelity; independent writable-file JAT/age/relocated restore PASS; public product remains blocked |
| [upstream-windows-core.json](upstream-windows-core.json) | PASS native core-only RED/GREEN; original 2 failed, patched 2 passed, separate builds |
| [upstream-initial-failure.json](upstream-initial-failure.json) | Retained FAIL before build-target isolation; no product pass inferred |
| [probe-recovery.json](probe-recovery.json) | Exact source/job history for CRLF, shared-target failure and final core-only success |
| [verification.json](verification.json) | Broad suite FAIL; all 16 failures replay on main, exact node names retained; Node and other checks |
| [review.json](review.json) | Independent static findings, three Important fixes and explicit remaining boundaries |
| [linux-tls.json](linux-tls.json) | PASS narrow actual-engine TLS/missing-config fixture; negative cases time out without HTTP |
| [linux-tls-initial-failure.json](linux-tls-initial-failure.json) | Initial fixture FAIL before adding required HEAD handling |
| [tls_probe.py](tls_probe.py) | Rerunnable owned Linux TLS fixture; no live provider or user authority |

The same benchmark script hashes match both receipts. Baseline instrumentation
runs outside the clean baseline worktree and imports that worktree's actual
production modules. The exact `operations` fixture function is unchanged;
receipts record its hash rather than inferring whole-file equality after new
managed fixtures were added.

Native proof artifacts are from
[workflow 39032863831](https://github.com/joshyorko/josh-room/actions/runs/39032863831).
The official Windows job and Windows complete managed recovery remain FAIL.
The recommended core patch is not shipped or installed as a product binary.

TLS reproduction from the repository root, using a verified official binary:

```sh
PYTHONPATH=src python docs/evidence/rustic-repair-2026-10-17/tls_probe.py \
  --rustic /path/to/verified/rustic --output /tmp/owned-tls-receipt.json
```

This transport fixture is not a MinIO or R2 implementation and does not qualify
S3 Save/restore. No shim is used in either production benchmark.
