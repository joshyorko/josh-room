# Combined-review capture integrity corrections

Release remains on owner HOLD. Final corrected product is
`67fe8e036926929d7bd336d98ad63e9e344500cd`; the shared #216 product is
`5ba87102f730d06d8c204d533362fa2ced603cdf`. Later test/documentation commits do not
change those product bytes. [Sanitized machine receipt](review-fixes.json).
The [older Linux receipt](README.md) qualifies only its named earlier source.

## Corrected contracts

Parent glob exclusions are rebased as rules, including future-created paths.
Generated leading `#` is serialized as `\#`; Python and native `dirty.js` reload
it as a literal exclusion while ordinary unescaped comments remain comments.
Typed, length-framed `sha256-framed-v2` distinguishes empty files/directories and
content boundaries. Tagged retries recover real authenticated added-byte counts
and corroborate actual ancestry before descriptor publication; unknown is not zero.

Initial child descriptors authenticate `source.composition_policy=inherited-rules-v2`
only after the generated private-policy proof. Every subsequent Save requires that
proof and the exact baseline's effective policy digest before staging, including
fresh operation IDs and no-ops. Live/private/restored digests are checked through
capture and publication. All policy edits, including comments, require unsupported
explicit reconciliation. User-editable comments cannot certify provenance.

Absent/experimental-v1 provenance remains readable/restorable but cannot authorize
updates: earlier experimental clients could stamp an edited policy. Old descriptors
and history are never rewritten. Actual older decoders from `20ff8fa` and `aa03d9e`
read old descriptors and refuse v2 rather than silently dropping it. Normal opt-out
Save does not acquire this field. Local binding and remote publication are not
atomic; late local edits remain unverified and cannot be called clean/saved.

## Final narrow evidence

PASS with real Restic **0.19.1**, age **1.3.2**, Python **3.12.14**:
`tests/test_composition_updates_live.py -k 'marked_policy or policy_change_before
 or legacy_lossy'`: **5 passed, 15 deselected in 175.30s**. Covers unmarked/v1 legacy
fresh-ID refusal, marked-v2 rule removal before any staging/runtime/authority
write, fresh/no-op/committed retry reuse, unchanged-policy update success, exact
old/new payload restores, and policy edits after backup and descriptor PUT.
Late failures preserve old pins and retain pending/orphan outputs for reconciliation.

The final VSIX was installed into an isolated disposable profile. All **73**
controller modules and `dirty.js` matched final product bytes. Running the one new
policy-lock regression from the installed controller: **1 passed, 19 deselected
in 53.90s**. This executes the actual installed Python API using explicit existing
Python, not immutable RCC or registered native dialogs. Durable SQLite supplies
only synthetic S3 object transport; Restic/age are real, not mocked.

Final VSIX (vsce **4.0.0**) SHA-256:
`ff8be03d099978f38becd6bf5f3cffc2b1398ebe6196f204c0b4742b410df613`.

Installed `dirty.js` also passed actual-file escaped-hash/anchored-path/comment
matching and Python policy-hash parity. Its bytes did not change after `aa03d9e`.
A new cross-language regression passed; prior native dirty/catalog tests passed
**17** cases. Optional-source schema/current reader compatibility passed for
both v1 and v2; both actual older decoders refused v2. The #216 checkout passed
**4** narrow serialization/compatibility cases plus the updated v2 case.
Ruff, whitespace and canonical/controller/template parity passed.

NEW independent Astra HIGH review reported **SCOPED CORRECTED** on final product
and shared heads. The reviewer passed offline tests; native cases were skipped
because its host Restic was 0.20. Those skips are not native passes. The pinned
real and installed-controller evidence above is supplied by this lane.

## Earlier scoped results and retained failures

The receipt binds earlier runs separately: first-cycle product `4d84895` passed
**5** real framing/count/ancestry/future-rule cases (236.76s), then **6** installed
controller cases (320.51s), including CLI dispatch and old/new parent recovery.
Shared base `10f3770` passed **4** initial capture crash/retry cases (87.53s), with
nonzero accounting preserved. They were not repeated unchanged after recheck.

At `aa03d9e`, **2** new provenance/hash-rule real cases passed (61.75s), and **2**
installed-controller cases passed (61.35s). Ordinary opt-out real Save/no-op/
incremental/restore passed (12.44s), asserting no provenance on every publication.
Admission/unit tests passed **249** plus **47 subtests**; one additional Python/
native hash-parity case passed. These results do not claim final UI/provider
acceptance or substitute for the new final policy-lock regression.

Before correction, the future-rule/type-framing reproducer failed **5** cases.
An adapter fixture assertion and initial Ruff issues were corrected. Setup probes
initially omitted project PYTHONPATH/used the wrong installer manifest-platform;
correct pinned invocations succeeded. The initial broad run was aborted by sandbox
npm network denial. The complete offline run at `4d84895` had **2370 passed,
7 failed, 6 skipped, 33 integration deselected, 47 subtests**. All seven failures
were PCC trust checks under sandbox root UID65534 mapping; all **17** PCC tests
passed with normal host UID mapping and unchanged trust code. No broad pass is
claimed by erasing those failures. Secret-scan classification is in the receipt.

## Remaining limits and integration

Linux source-only/component-free; 128 child updates and 32 parent operations;
no history trimming, automatic pre-backup claim takeover, policy reconciliation,
unload or deletion authority. Changed reserved requests require reconciliation.
Corrected-source registered native dialogs, real MinIO/R2/S3, installed immutable
RCC, native Windows composition and large-workspace resource limits are NOT RUN.
Earlier installed UI/MinIO receipts remain scoped to their older source.

Shared integration surfaces include `logical_jat.py`, its closed schema,
`workspace_policy.py`, `dirty.js` and mirrors. Select #215 admission and #217
ordinary Save regressions when combining; no other feature branch was edited.
Preserve #218's explicit Restic `parent=` argument for fresh child/parent updates.
No merge, rebase, tag, release staging or release publication occurred.
