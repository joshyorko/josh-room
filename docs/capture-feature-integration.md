# Capture features: integration and release sequence

Room PR #147 and JAT PR #21 are merged for the subsequent 0.1.36 feature delivery.
The published Room 0.1.35 maintenance release and its artifacts remain unchanged.
Josh authorized qualified feature integration and publication of supporting JAT
runtime 0.1.14 from its exact reviewed source and artifact proposal. Final Room
publication remains with Josh.

## Review boundary

Room provides ignore initialization, bounded aggregate scanning, native reviewed
exclusions and explicit source-only Save/restore. Existing full-repository rules
and old snapshot defaults remain unchanged. Native Room saves are independent
of JAT's filtered-capture capability.

JAT provides explicit literal exclusions through its standalone CLI, service and
thin RCC adapters. Filtered Room exports require this contract and the exact
qualified JAT source/runtime pair. The 0.1.36 candidate adopts published
[`v0.1.14-jat-runtime`](https://github.com/joshyorko/josh-all-the-things/releases/tag/v0.1.14-jat-runtime)
at `fe43065a654d1649ba9ebe5a6f202eac612e4385`. Earlier runtime pins must not be
silently treated as supporting filtered capture.

## Safe order

1. Preserve published maintenance release 0.1.35 at
   `e773963106829668c9bca8bf94c045c99f8267f1`, including its assets, tags and
   template. Reconcile subsequent main changes before final qualification.
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
6. Prepare the new Room 0.1.36 feature candidate and template 0.1.6 with their
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

Feature merges and supporting JAT publication are complete. The remaining owner
action after final Room qualification is to publish the named 0.1.36 source and
staging run through the existing release workflow. A stage created before JAT
0.1.14 adoption is historical evidence and must not be used for that release.
