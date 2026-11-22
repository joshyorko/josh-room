# v1 integration qualification

Target v1.0.0 remains on owner release hold. Draft
[#221](https://github.com/joshyorko/josh-room/pull/221) combines the seven Room
feature checkpoints on `feat/v1-integration`; it does not merge main or publish.
The current release gate checklist is
[#213](https://github.com/joshyorko/josh-room/issues/213).

## First frozen candidate

Source `c4b735f7fc89421681310c715fa475779cea1f27` includes #205, #210, #217,
#216 then #218, #215 and #214. Obsolete promotion #208 is excluded.

The [combined CI run](https://github.com/joshyorko/josh-room/actions/runs/45054969665)
passed Python 2,894 tests and 47 subtests, with 110 skips; Node passed 442 with
two managed-runtime opt-in skips; OMP passed 49. Both native controller
[qualification jobs](https://github.com/joshyorko/josh-room/actions/runs/45054969659)
passed. Promotion and release-candidate publication were intentionally skipped.

One wheel and VSIX were built and installed in an isolated Linux profile:

| File | SHA-256 |
| --- | --- |
| `josh_room-0.1.0.dev0-py3-none-any.whl` | `4262b9e6de8cbbb801a6793b01f4f61958dcf8e8f86b48d102eec2e8c0e295d3` |
| `josh-room-0.1.52.vsix` | `674fd66c3095361c2c56a688dd0399340c41f301de410f3e9b874d505e2460b8` |

All 135 wheel package members and 123 shipped controller files matched the
canonical source. These packages retain their pre-release-preparation version
metadata and default published pins, including JAT v0.1.18. They are not a
v1.0.0 release. Explicit local CI-candidate selection is separate from default
bootstrap and published-pin acceptance.

Combined browser acceptance passed 110 live label rebuilds with 111 distinct
title/subtitle pairs. The atlas retained 20 current textures and disposed all
260 observed textures exactly once, without premature disposal. Inspector
polling changed the displayed image from 320 to 77 pixels. Offline reopening,
zero external requests/CSP violations and a 41-tile 2D fallback passed.

Real disposable MinIO and native Secret Service acceptance used the installed
controller through the exact Linux CI RCC environment candidate, not host
Python. Save/no-op, ordinary historical Enter, child capture/update/retry,
explicit parent refresh, old/new child hydration and ordinary historical
tree/diff/extraction passed. Registered installed recovery also rendered the
native tree, read-only Monaco diff and selective Save dialog.

The matching unpublished JAT #43 candidate at
`1eb2d9ffe638dc66f04e1f90256dca021f979b3c`, with its pinned Hauler
2.2.0-dev.5, passed four dynamic serving modes, three real digest-qualified
pulls, two byte-identical file downloads and OCI manifest/config/layer export
identity through the installed wheel. File listeners were contained and
disclosed as all-interface; registry listeners were loopback. The separate
stock Hauler 2.1.1 trial is compatibility evidence, not the matching-runtime
receipt. Neither trial publishes JAT or changes Room's default pins.

## Acceptance corrections

The first candidate exposed three integration gaps. The correction after that
freeze adds explicit namespace/domain-bound selective child recovery and
recognizes verified managed Serve signal exits as cancellation. Artifact
identity checks remain mandatory; interruption never becomes success.
Caller-owned startup files can remain after interruption; the extension owns
and removes its private invocation directory.

Closing the actual test workbench left the inner RCC/JAT/Hauler listener alive.
A subsequent Serve-scoped hangup handler also failed that native close trial;
neither failure is counted as a pass. The next correction adds a foreground
Linux lifetime-pipe guard and a Windows kill-on-close Job Object. Local Linux
tests cover abrupt owner death, startup EOF/races, normal exit status and an
independent concurrent listener. The exact CI Python initially refused before
starting a listener because its `os.pidfd_open` binding was absent. A verified
libc fallback corrected that packaging boundary. Actual editor-close acceptance
then passed with the same CI environment and corrected source: Room, guard,
RCC, JAT and Hauler all disappeared, the socket closed and the startup receipt
was absent without manual signals. This is an explicit source override; the
corrected packaged VSIX and native Windows Job Object CI remain separate gates.

Independent lifetime review then reproduced an RCC-exits-first orphan: the
guard exited while a detached listener retained output pipes. The follow-up
makes the guard a scoped Linux child subreaper and cleans adopted descendants
on every exit. Both ordinary RCC exit and RCC SIGKILL regressions now preserve
the wrapper status, stop the listener and release Room's output pipes. Native
matching-runtime repetition and independent recheck remain required.

The matching-runtime repetition passed, but a subsequent independent scheduling
probe found an existing listener could reparent during the one-pass descendant
scan. The next correction rechecks and reaps until the kernel reports no
children. Its deterministic regression schedules that exact intermediary exit,
without a cleanup-time fork, and checks both listener closure and output EOF.
The full focused guard/JAT/cancellation selection passed 75 tests. Final
independent recheck and exact-head CI remain pending.

Focused correction checks passed 63 JAT tests, 35 recovery tests and six native
action tests. The final namespace regression passed with real age and pinned
Restic 0.19.1 over synthetic S3 transport, including historical updates,
wrong-scope refusal before payload access and unchanged marker/guard state.
Both actual managed Serve interruption paths returned cancellation/130 and
closed all listeners and owned children. Those interruption results used
corrected source; they do not qualify the earlier installed packages.

The final local supervision/JAT selection passed 70 tests with three Windows
skips. Combined Node checks passed 444 with two managed-runtime opt-in skips.
The mixed-provider CLI case initially failed on the sandbox's read-only default
state directory; it passed with a task-private state root. One package-building
repository test was left to ordinary CI after sandbox npm access was denied.

## Remaining gates

The correction requires its own exact-head CI, independent review and installed
artifact acceptance. #219's same-point native capture history and #220's
Windows installed acceptance remain separate active follow-ups. Agent Save
selectors stay disabled until their reviewed lifecycle contract qualifies.
Historical binary rollback and older-reader access to new composition records
remain unqualified; source-reader tests are not installed old-binary proof.

Final release also requires the approved matching JAT publication and immutable
pin promotion, final Linux/Windows installed qualification and regeneration of
the obsolete promotion from exact integrated source. No merge, release request,
tag, staging or publication is authorized by these receipts.
