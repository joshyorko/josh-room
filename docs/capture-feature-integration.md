# Capture features: integration and release sequence

Room PR #147 and JAT PR #21 are a subsequent feature delivery. They must not be
added to the staged Room 0.1.35 maintenance release or its existing draft.
The maintenance owner controls that draft and the main release workflow.
This document prepares review and qualification; it does not authorize merging
or publishing either feature PR.

## Review boundary

Room provides ignore initialization, bounded aggregate scanning, native reviewed
exclusions and explicit source-only Save/restore. Existing full-repository rules
and old snapshot defaults remain unchanged. Native Room saves are independent
of JAT's filtered-capture capability.

JAT provides explicit literal exclusions through its standalone CLI, service and
thin RCC adapters. Filtered Room exports require this contract and the exact
qualified JAT source/runtime pair. The current published JAT runtime must not be
silently treated as supporting filtered capture.

## Safe order

1. Let the maintenance owner finish the isolated 0.1.35 release. Keep its draft,
   assets, tags and release workflow under that owner's control. Reconcile any
   merged main changes into the feature branch before final qualification.
2. Review JAT PR #21 and Room PR #147 at their named heads. Check focused/full tests,
   platform receipts, actual packaged UI evidence and remaining limitations.
3. After explicit merge authorization, integrate JAT's filtered capability.
   Build and qualify a self-contained candidate from the exact resulting main
   commit. Preserve source embedding and task bridge; a dependency-only rebuild
   is not equivalent to the qualified filtered artifact.
4. Use JAT's native promotion qualification and publication gates for those exact
   candidate bytes. Publication requires separate authorization and a new feature
   runtime tag; never replace the already published maintenance runtime.
5. After authorization, integrate Room's feature PR into the subsequent feature
   line. Adopt the exact qualified JAT artifact/source pair in one reviewed
   follow-up. Native Room saves can integrate independently, but do not claim
   filtered export acceptance until this adoption is verified.
6. Prepare a new Room feature candidate (recommended next patch: 0.1.36) with its
   version, lock, manifests, canonical controller and template mirrors consistent.
   Rebuild and run candidate/readiness checks against that exact final source.
   Repeat packaged Save/relocated restore and exact filtered-export acceptance.
7. Only after release approval may the owner tag, publish or deploy. Candidate
   qualification and a passing PR do not themselves authorize those actions.

## Evidence required at the handoff

- Exact source SHA/tree, extension version, VSIX digest and dependency pins.
- Python/JavaScript policy agreement, aggregate/incomplete scan checks, explicit
  selection/confirmation/cancel behavior, and refreshed scan/preview state.
- Packaged native Add Storage with native credential custody; repeated identical
  storage selection must not overwrite config, and cancellation must not start
  credential entry.
- Actual packaged Save and restore at a different path with disposable original
  unavailable; selected content hashes match and excluded metadata/files are
  absent. Preserve original file/link hashes.
- Native Linux/Windows backend qualification, plus explicit scope of visible UI
  testing. A Windows scan UI test is not Windows Save/restore acceptance.
- Exact embedded JAT artifact qualification, retained unsafe-link rejection,
  and Room consuming the named artifact without a checkout overlay.
- Separate passing, failed, blocked and unrun gates. Moto fixtures do not prove
  live MinIO/R2 behavior. No denied screenshot upload is required for evidence.

The minimal owner decision after a qualified handoff is approval to merge the
named feature heads into the subsequent delivery and approval of its new version
and artifact adoption. Publishing remains a separate decision.
