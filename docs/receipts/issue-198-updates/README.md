# Synthetic Linux child-update and parent-refresh acceptance

Dependent draft [#218](https://github.com/joshyorko/josh-room/pull/218) extends
[#216](https://github.com/joshyorko/josh-room/pull/216). The tested product source
is `801c79af97867748218cb8e77e35667e4bd43a29`. The final evidence commit changes
documentation and adds a synthetic multi-repository test; product bytes remain
bound to that source revision. See [the sanitized receipt](linux-acceptance.json).
**Release remains on owner HOLD. No merge, rebase, tag or release publication.**

## Implemented and observed

Independent whole-child Save publishes a verified immutable Logical JAT with
explicit prior lineage and atomically associated retention metadata. It updates
only that child's guard/marker. Parent references do not advance implicitly.
Explicit parent refresh publishes a new parent from verified saved parent bytes
and user-selected existing child pins. Working parent files remain unchanged.
Source-only capture excludes Git history and optional runtime components.

The final VSIX was packaged and installed into a disposable Linux profile. Its
installed controller and JavaScript matched the tested source. Registered
`joshRoom.save` routed a guarded child through its dedicated namespace writer;
`joshRoom.saveChild` repeated that action as a no-op at the same pin;
`joshRoom.refreshCatalogPins` published the explicitly selected child reference.
Actual built-in dialogs were operated through the private editor renderer.
There were no mocked provider responses, keyring results or encryption decisions.

The installed trial used an explicit existing-Python/controller-source override
in `local-build-fallback` mode, not the immutable managed RCC artifact. The
synthetic fixture was trusted with workspace trust disabled; this is not native
untrusted-workspace acceptance. Existing unit guards cover trust admission.

Only product widgets are retained in these cropped screenshots:

- [Child Save approval](native-child-approval.png): exact baseline and source-only disk capture.
- [Child no-op approval](native-child-noop-approval.png): no new recovery point.
- [Expanded child result](native-child-saved.png): complete immutable child pin.
- [Parent refresh approval](native-parent-approval.png): exact existing parent and selected path/pin.
- [Expanded parent result](native-parent-saved.png): complete new parent pin; working folders unchanged.

After the registered actions, normal CLI restores into absent targets verified
all four parent generations against their respective child IDs and bytes,
including the oldest and intermediate versions after child latest advanced.
The original source's complete four-file set and bytes, legacy snapshot
inventory, and working parent marker/manifest remained unchanged. The namespace
had exactly nine logical records and nine Restic snapshots. No-op and committed
retries added no snapshots. The final controller also replayed the same committed
native operations and reverified their exact payloads before local rebinding.

## Scoped checks

- PASS: broad pinned Python run at the tested product revision: 2362 passed,
  47 subtests, 6 skipped, 27 integration cases deselected. The later added
  multi-repository integration case was run separately.
- PASS: focused metadata/schema/CLI contracts: 146 passed and 47 subtests.
- PASS: actual Restic 0.19.1/age tests cover historical pins, child and parent
  snapshot/commit process loss, interruption between guard and marker,
  pre-backup claimed-without-snapshot refusal, late-path stale approval,
  child/parent no-op, and remote-committed/local-dirty reporting. Durable SQLite
  supplies only the synthetic S3 object transport for those tests; see the
  receipt for revision-scoped runs and counts.
- PASS: two real synthetic Git repositories with history-only payload; neither
  child nor parent snapshots contain Git administration/history, parent snapshots
  contain no child paths, and both original repositories remain present.
- PASS: Node extension/dirty/catalog/inventory suites: 276 passed, 4 skipped.
- PASS: actual normal CLI enrollment and update/refresh/retry/no-op against
  disposable loopback MinIO and native Secret Service, plus installed actions and
  subsequent normal CLI exact-pin recovery. These are separate from SQLite tests.
- PASS: pinned vsce 4 packaging, bundled secret scanner, installed source parity,
  canonical/controller/template byte parity, Ruff and `git diff --check`.

Retained failures: a metadata rejection assertion initially expected a narrower
diagnostic; default host XDG state was read-only; the first Node invocation used
Python without project dependencies. Corrected isolated/pinned invocations pass.
Native harness startup initially exceeded Linux Unix-socket path length. First-run
editor onboarding obscured the product dialog, so acceptance waited for its
dismissal and used visible hit-test checks; false captures were not retained.
Post-run direct authority inspection initially omitted the required selected
encryption context; it resumed using the normal context and verified existing
exact-pin destinations instead of deleting them. These were harness/test failures,
not product acceptance passes.

## Reproduction and remaining gates

Use project Python, pinned Restic 0.19.1, age, and disposable state. Point
`JOSH_ROOM_TEST_RESTIC` and `PATH` at the verified Restic binary when the host
version differs. Keep XDG/config/instance/npm/pytest state in task scratch.

```sh
python -m pytest tests/test_composition_updates.py -q
python -m pytest tests/test_composition_updates_live.py -q
node --test vscode-extension/catalog-actions.test.js
python scripts/composition_minio_acceptance.py --scratch /synthetic/task \
  --minio /synthetic/task/minio --secret-service-bus /synthetic/task/keyring-runtime/bus \
  --qualify-updates
```

The provider driver generates credentials in memory and uses only a separately
provisioned native keyring socket inside that task root. It verifies the pinned
MinIO binary and stops its own server. `--hold-for-native` optionally keeps that
owned fixture alive until its `stop-native` file is created. Never point it at
the host keyring, existing buckets or user data. The nonsecret setup pattern
comes from [#209's recipe at `2398f0d`](https://github.com/joshyorko/josh-room/blob/2398f0d0d769f2c9f8d559529849e587fa797aa1/docs/recovery-acceptance.md).

SKIP/UNPROVEN: native Windows composition (fails closed), immutable managed RCC
execution, remote R2/S3, large-workspace resource benchmarks, independent review
and unload/deletion authority. Broad suite skips separately include private
device-ready/secret-gated provider checks, opt-in large streaming, and native
Windows ChangeTime semantics. No claim interrupted before a snapshot exists is
automatically stolen; changed reserved requests and stale baselines require
operator reconciliation. No existing history is trimmed to make progress.
