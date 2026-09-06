# Issue 50 startup readiness patch

This 0.1.20 follow-up preserves the merged encryption lifecycle work in PR #52
and the closure of issue #50. It repairs the disconnected explicit first-run
action without restoring implicit runtime, credential, or storage access on
activation or cache-only refresh.

## Implementation

- An idle Prepare/Load Storage action and real retry lead to one cancellable
  runtime preparation and an authoritative storage-tree reload.
- Runtime download/import/reuse progress reaches native notifications and
  sanitized logs. Shared controller/JAT preparation respects each consumer's
  cancellation and stops underlying work when no consumer still needs it.
- MinIO add/edit/reconnect and encryption initialization reload authoritative
  storage. Error actions retain connection versus Dimension routing.
- Migration uses separate private source and recovery handoffs. Its read-only
  preview precedes confirmation; execution checks the confirmed catalog ETag
  and disk requirements before enrollment. Published cutovers can reconcile
  without the old source identity. Standard age-keygen comments are accepted
  without allowing multiple operational identities.

## Local verification

- Full Python suite with real age/age-keygen: 435 passed, 6 skipped.
- Full Node suite: 219 passed, 2 skipped; also passed under release Node 22.
- Ruff, JSON parsing, tracked shell syntax, source/template/controller parity,
  packaged-source parity, Python distribution build, VSIX build, and
  `git diff --check` passed.
- Gitleaks found no secrets in the candidate VSIX. The tracked-source scan's
  sole finding is a pre-existing deliberately synthetic redaction-test value,
  not a credential. No scanner rules or repository thresholds were weakened.

## Acceptance boundaries

The final candidate VSIX SHA256 is
`bbde096583b5c0d5eb28aa1cf4c96ee35913683747af86459f759bb3d9015485`.
It passed in an isolated Code Insiders web workbench with its native Node
extension host, unmodified installed extension source, and real pinned runtime
acquisition. Cold startup showed RCC/controller download and import; a distinct
extension-host process reused the same private disk cache on warm startup.
Both reached the ready storage tree.

The separate synthetic loopback S3 phase used the actual packaged controller
and age. It loaded a saved connection and Room/JAT hierarchy, exercised generic
error retry and connection editing, and cancelled at the old source-identity
prerequisite. The previously observed object-stringification receipt defect
was fixed and did not recur. The fixture recorded zero storage-write attempts.
This is not live MinIO acceptance.

Installed candidate and released-VSIX acceptance are recorded separately from
the local suites. Real existing non-empty MinIO read/open remains operator-owned.
No live MinIO/R2 migration, rotation, deletion, storage write, JAT payload
mutation, infrastructure change, or shared-cache removal is authorized by this
receipt or claimed as tested.

Skipped Python gates: private first-run, live MinIO, live R2, unavailable full
RCC-first JAT/Hauler vertical, registry/tool-gated secure Room, and opt-in large
streaming acceptance. The two optional managed-runner Node tests require their
separate runtime fixture and remain skipped in the ordinary suite.
