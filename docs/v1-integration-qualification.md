# v1 integration qualification

## Owner scope freeze

The combined feature scope is frozen. The new shared agent-history Save picker
and its direct Save adapter are off in the v1 candidate; their expanded
fault-injection/rollback matrix is deferred rather than a release gate. Existing
explicit capture, inspect and restore behavior and its safety requirements remain
unchanged. Historical pilot results below do not enable the deferred feature.

The immediate remaining execution is the already identified Windows harness
receipt-argument repair against the explicit immutable RCC candidate. No new
feature branch or broader qualification gate is authorized. JAT is an independent
upstream release lane: Room testing does not gate its publication. Once the
matching immutable JAT is actually published, pin it and perform the required
installed Linux/Windows Hauler acceptance. This does not authorize upstream
publication, premature pin changes, or Room merge/tag/publication; Room remains
on owner HOLD.

Target v1.0.0 remains on owner release hold. Draft
[#221](https://github.com/joshyorko/josh-room/pull/221) combines the Room
feature checkpoints on `feat/v1-integration`; it does not merge main or publish.
The current release gate checklist is
[#213](https://github.com/joshyorko/josh-room/issues/213).

## First frozen candidate

**Do not use Serve from the first frozen wheel or VSIX.** They predate the
process-lifetime correction described below. The synthetic zero-network HTML
is independent of Serve and remains available for an offline trial.

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
corrected packaged VSIX remains a separate gate.

Independent lifetime review then reproduced an RCC-exits-first orphan: the
guard exited while a detached listener retained output pipes. The follow-up
makes the guard a scoped Linux child subreaper and cleans adopted descendants
on every exit. Both ordinary RCC exit and RCC SIGKILL regressions now preserve
the wrapper status, stop the listener and release Room's output pipes. Native
matching-runtime repetition and independent recheck subsequently passed.

The matching-runtime repetition passed, but a subsequent independent scheduling
probe found an existing listener could reparent during the one-pass descendant
scan. The next correction rechecks and reaps until the kernel reports no
children. Its deterministic regression schedules that exact intermediary exit,
without a cleanup-time fork, and checks both listener closure and output EOF.
The full focused guard/JAT/cancellation selection passed 75 tests. At exact
`703e526443b494c5d6a99cb1a29604250632810a`, independent recheck passed five
selected process regressions with no remaining scoped finding. Actual corrected
source with the matching RCC/JAT candidate stopped the affected listeners and
descendants after inner RCC failure while an independent session survived.
Native Windows Job Object checks also passed in ordinary CI. These results do
not qualify the older frozen packages.

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

`1ebb277f43e51c333fb9aeb577b6c7a70972448e` incorporates final #219
`d3b65010b35b20f3f29f9cf2b05459674828b114` without changing the producer branch.
The combined [main job](https://github.com/joshyorko/josh-room/actions/runs/45060569383/job/137325323841)
passed Python 3,127 tests and 47 subtests with 121 skips, Node 451 with two
skips, and OMP 49. Template, native Windows, recovery and both controller
qualification and both managed-consumer jobs passed. All nine ordinary checks
succeeded; promotion and release-candidate were intentionally skipped.
Independent immutable-source review passed 121
focused Python tests with real age and synthetic storage/authority plus 11
extension tests, finding no merge-introduced regression. This did not rerun a
live provider, package, installed extension or unfinished adapter. Corrected
installed artifact acceptance remains open.

## Corrected local freeze

The [1ebb277 package receipt](receipts/v1-frozen-1ebb277.json) records one rebuilt
wheel and VSIX, both installed in isolated Linux paths. All 138 wheel source
files and 126 shipped controller files match the frozen source. All 149
installed extension entries match; the only permitted package.json change is
VS Code's added `__metadata` field. Initial direct no-isolation wheel building
could not load Hatchling; the successful build used uv's cached isolated backend.
Neither existing development dependencies nor host packages were modified.

The installed CLI exported and inspected the same synthetic HTML bytes as the
prior browser-qualified file. The new Linux and Windows controller CI archives
were downloaded and their GitHub ZIP digests verified. Their artifact digests
differ from the earlier c4b candidate despite equal dependency freezes; runtime
qualification therefore uses the exact new candidate, without treating the old
materialization as equivalent. The [installed Linux trial](receipts/v1-installed-linux-1ebb277.json)
passed native workbench-close/pull verification, independent-session RCC failure
isolation and eight executed data gates. Explicit session history was not run,
and the installed wheel was not substituted for the VSIX controller data matrix.
The exact frozen source remains `1ebb277`; no published pin has changed.

#219 supplies explicit same-point immutable capture history. Its real MinIO and
native-custody proof includes six historical payloads, zero-write no-op,
independently re-encrypted copy/export and complete removal. Installed explicit
capture/restore used the disclosed source-controller fallback. Those receipts
do not prove the new Save adapter or immutable controller acceptance. The
existing producer and Save owner checkpointed that adapter at `0b0c54a` under
the reviewed batch/attempt contract; all agent Save selectors remain disabled.
Independent source review requested six corrections covering cancellation,
exact-point retry, unsupported local flags, pending receipts and byte reporting.
The correction batch includes real-age exact-B retry after reviewed-A removal,
zero remaining pending receipts, nullable unknown upload counts and stricter
native partial-result binding. Focused checks passed 60 adapter/producer cases,
seven CLI boundary cases and 310 chooser/extension cases with two opt-in skips;
unchanged real-Restic callback tests were not repeated. Independent recheck,
new-head CI and real-provider adapter acceptance remain required. A subsequent
recheck retained three grant-revocation/pending-receipt gaps. The next batch uses
unreleased Save-plan v2 with a pre-reserved public workspace ID, grant-independent
read-only commit corroboration, deterministic pending-receipt reservation and
validated cleanup. Its real MinIO/native Secret Service CLI trial passed,
including accepted/lost workspace CAS, A removal, exact B retry, unknown upload
counts and first-grant revocation. Lost-CAS marker finalization uses a bounded
no-clobber custody transaction with process-crash/race tests. These source-level
results are not installed/RCC adapter acceptance; availability remains false.
#220's Windows installed chain remains a separate active lane.

## Save recovery review corrections

At exact `574c0b0799ec0d6ba0d91d75ef159aaea7e1fd03`, all nine ordinary
checks passed; promotion and release-candidate publication were skipped.
The [main job](https://github.com/joshyorko/josh-room/actions/runs/45066565966/job/137343935375)
passed 3,219 Python tests and 47 subtests with 125 skips, 463 Node tests with
two skips, and 49 OMP tests. Independent review nevertheless found unsafe
marker exception rollback, validation/unlink races and false attempt attribution
when the encrypted ledger is unavailable. CI success did not close those gates.

The next correction removes exception rollback entirely. Failed validation
retains custody rather than installing an unverified old marker. Marker control
cleanup claims into deterministic, private, anchored children and validates the
claimed bytes and inode before removing only that claim. Process-exit regressions
cover cleanup interruption after claiming both the old marker and the intent,
as well as the earlier marker-claim and promotion boundaries. Original-namespace
replacements remain retained with uncertainty. Pending-object cleanup follows
the same private-claim rule and keeps interrupted claims visible to reconciliation.

Without an accessible ledger, an independently verified scoped candidate point
is reported separately from this attempt's commitment: `workspace_committed`
remains null, upload counts remain unknown and no saved workspace baseline is
adopted. The native warning offers the same approved exact retry without claiming
Saved, Already saved or zero uploaded bytes. Restoring the same authority permits
ledger-backed reconciliation; it does not authorize choosing a newer point.
The [producer receipt](receipts/native-agent-save-producer-v2.json) retains the
superseded attribution result and separately records corrected real MinIO/native
Secret Service qualification. Fresh independent review and installed/RCC adapter
acceptance are still required. All agent Save availability flags remain false.

The explicit encryption-material input boundary also now refuses missing or
disappearing caller-owned handoffs before vault fallback or identity generation.
Normal requests for a new controller-owned output retain generation behavior.
The dedicated regression covers both cases and refusal to initialize an empty
bucket from an existing-input request. Native Windows ACL integration and direct
installed missing-handoff refusal are being qualified separately on #220.

Independent recheck at `3cd2c4c` passed 29 selected Python cases and 15 Node
cases (two runtime-dependent skips), confirming the backend corrections. It
found two remaining native retry-offer defects: a newly created Room had no
selected catalog project, and a marker with a different policy digest could
qualify. Agent previews now expose the CLI-resolved public Room ID; both create
and update use that reviewed ID, and retry also requires the marker's policy
digest to match. Registered create/update regressions cover known and unknown
commitment, cancellation and exact retry. This is a UI receipt correction;
agent Save availability remains false pending final integrated qualification.

## Windows installed checkpoint integration

The frozen cb860f7 Linux trial found a real chooser-routing defect. Direct
installed/current-RCC history status admitted both existing profiles, but the
registered New command's Dimension router removed the legacy `--backend` hint
and the new parser required it. The chooser therefore showed unavailable rows
before any target Room point was created. The retained package is not modified
or counted as a successful chooser trial. The correction makes the backend
hint optional, keeps the required explicit Dimension authoritative, and refuses
any status fallback that could reach snapshot creation. A cross-language test
feeds actual routed native arguments into the real controller parser; dispatch
coverage confirms metadata-only behavior. Corrected package acceptance is a
separate next trial.

The same frozen trial also refused a new Room whose folder/content matched the
bootstrap Room: Restic auto-selected that physical snapshot and skipped creating
the new Room's first snapshot. No target descriptor committed. When no logical
parent is selected, backup now uses the pinned Restic forced-scan path, which
disables automatic parent selection; explicit Linux parents retain incremental
behavior. The real-tool regression covers two root captures of the same folder,
then explicit-parent no-op and exact restore. A bridge regression covers distinct
logical Room identities sharing the folder/content. Windows forced-scan behavior
is unchanged.

Direct installed CLI/current-RCC checks on cb860f7 passed two-tool Save, exact
no-op, A-B-A history, changed-workspace capture and both cold historical restores.
They do not qualify the failed registered chooser. The wire trace observed three
temporary Restic lock PUT/DELETE pairs during repeat and no non-lock payload,
history or catalog writes. The literal zero-wire-write assertion remains a
recorded failure; earlier zero-Botocore observations were not wire measurements.
Locks remain enabled. Human no-op wording now says no new backup data rather
than implying zero network traffic. Fault/retry, remaining native UI and legacy
cases from this frozen pass remain skipped/unverified.

#220 at `5e05954ead917d7a4b8af18300e2fa6801a848ae` is now included. Its
[installed Windows job](https://github.com/joshyorko/josh-room/actions/runs/45068423905/job/137349700818)
passed alongside the native private-runtime and recovery jobs. Two environment
qualification/consumer checks were still pending at integration; no final
all-green claim is made for that source. The ordinary combined workflow now
runs its installed-recovery job and retains both viewer and runtime-adapter
Node suites. The only conflicts were the Node test command and the handoff
test's platform-aware identity fixture; both sets of behavior are retained.

Canonical auth keeps Windows native private ACL verification/protection before
identity bytes, POSIX admission, and required-existing handoff refusal. The
extension's Windows handoff producer coexists with the current agent retry
corrections; controller and template mirrors are regenerated from that merged
canonical source. Source-lane evidence does not qualify this new combined SHA.
The Windows installed recovery fixture still explicitly selects installed-source
Python, so its successful native UI/provider/keyring flow is not immutable RCC
feature acceptance. Final combined installed qualification remains required.

## Compatibility and rollback

The opt-in `windows-immutable-recovery-cb860f7` ordinary CI job consumes the
already retained Windows VSIX and RCCA for the frozen cb860f7 subject. Its
qualification label enables only this explicit test input; it is not a product
or default runtime pin. Both upstream ZIP digests, the subject SHA, VSIX and
RCCA digests are checked. The harness imports the existing archive and executes
with no-build artifact mode, using installed controller bytes and checking the
interpreter's private RCC origin. The previous source-Python path remains a
separate mode and ordinary tests are unchanged. No JAT job gates this trial.
Its acceptance harness commit can differ from its immutable product subject;
the receipt must retain both. The `022ddef` harness run failed before provider
setup: archive acquisition reached its 180-second deadline without output.
The interpreter probe did not run. This identifies the failed phase, not its
root cause; the failed receipt remains retained.

The next explicit job, `windows-immutable-recovery-022ddef`, consumes the
already-built corrected `022ddef` Windows subject rather than rebuilding it.
Controller artifact `14775243343` and product artifact `14774839605` were
downloaded and checked against their GitHub ZIP digests. The Windows VSIX is
`5306c503c202559f3809c292ba400ab21db7eb345e090efff429675d1e9d218b`;
its 152 files differ from the frozen Linux checkout only by CRLF conversion.
No other source or manifest difference was found; raw cross-platform byte
identity is not claimed. The RCCA is
`971e529bceb0d57eb694a497fc8b3569f80ff85c8f70c45d3c32453a64b28c4d`,
with environment
`sha256:756b529af1dda6a5da65cda69684d294e7ef6056a5922160e7d4f894f5511192`.

The acquisition harness now matches the successful artifact qualifier's
task-owned HOME, Mamba root and installed-controller working directory, while
retaining private temporary storage. It records bounded cache-directory counts
before and after acquisition. Acquire and interpreter deadlines remain 180
and 60 seconds; no solve, fallback or default-pin change is permitted. This
environment alignment is a hypothesis under test, not a native acceptance pass.

The subsequent `0ce17e3` harness run acquired the exact corrected archive in
124,667 ms with exit zero. Its interpreter probe failed in 26 ms with exit one,
before provider setup. Inspection of pinned RCC's execution contract identified
an omitted required receipt file when inheriting streams; the old receipt did
not retain stderr, so no verbatim diagnostic match is claimed. The next harness
supplies owned receipts for the probe, fixture, sanitizer and every provider CLI
call, then verifies the artifact and actual numeric child exit. Intended CLI
denials retain their nonzero exits rather than being mistaken for harness
failures. Interpreter origin checks, private storage, deadlines and no-build
mode remain unchanged. Native provider/UI acceptance is still pending.

The `7f705ce` harness then passed archive acquisition, interpreter origin checks,
native Credential Manager roundtrip, bucket creation and encryption initialization.
Its first snapshot creation failed `runtime-version-unavailable`: the direct
fixture omitted the version handoff normally supplied by the installed extension.
The harness now reads that value from the parity-verified installed VSIX manifest.
The product's version guard is unchanged; full native recovery still requires the
actual next job result. The failed receipt and successful owned-target cleanup
remain retained separately.

The `a727ba3` rerun instead reached the acquisition cutoff during cold
materialization: verified content, one materialization and four Hololib entries
existed, with one Holotree entry at 180,014 ms. The same immutable archive had
completed in 110,439 and 124,667 ms in earlier runs. The next existing test allows
one bounded 300-second acquisition attempt; interpreter, provider and UI limits
remain unchanged. There is no retry loop, cache/pin substitution, solve or skipped
verification. A further acquisition timeout remains a blocker rather than a pass.

The exact combined baseline `e67bc289059766db51c7320773305d6e47089e41`
passed all ten ordinary checks, with two intentional publication skips and no
unresolved review threads. Its independent recheck closed both remaining UI
retry findings and the bounded Windows handoff merge: 23 Node and five Python
cases passed independently. Parent merged checks passed 330 Node tests with
two opt-in skips and 81 Python tests with three native Windows skips.

The next source-only pilot enables the shared history chooser only on Linux
MinIO and only after existing profile/current-keyset admission. S3 remains
unavailable because the recorded real-provider trial qualified MinIO. The
general producer/native-resume capability remains false. The next exact green
pilot source must be frozen before building its one installed candidate; no
injected selector, runtime source override or prior package can qualify that
candidate. Failed installed acceptance must remain a failed gate, not be
converted into a qualification claim by this source flag.

[The bounded source probe](receipts/v1-source-compatibility.json) compares
immutable main source `23c80716b8a646cbf74f6eacb04eb639af87c9c2` (extension
metadata 0.1.52) and integration source `1ebb277`. Six decoder/schema checks
passed:

| Synthetic record | 0.1.52 source reader/schema | Integrated reader/schema |
| --- | --- | --- |
| Existing ordinary Logical JAT v1 | Accepted unchanged | Accepted unchanged |
| Composition `inherited-rules-v2` provenance | Rejected | Accepted |
| Agent state added as a fourth component slot | Rejected | Rejected |

Agent history remains outside the closed Logical JAT/PCC component slots.
Existing capture history v1 is readable under the v2 history contract; a
hydrated legacy point without authenticated semantic baseline is not silently
migrated. Composition records without v2 inherited-policy proof remain
read/restorable but cannot authorize child updates. No legacy namespace is
converted in place.

This probe is source-level parsing, not an installed 0.1.x binary cold-restore
or rollback trial. New composition records are not readable by the older
source; installing an older extension cannot be described as a compatible
rollback for those records. Final qualification must restore synthetic old
snapshots using the final package, check old bootstrap behavior and verify that
rollback preserves the old data and rejects unsupported new records without
rewriting them. Installed historical-binary and final immutable-runtime checks
remain open.

Final release also requires the approved matching JAT publication and immutable
pin promotion, final Linux/Windows installed qualification and regeneration of
the obsolete promotion from exact integrated source. No merge, release request,
tag, staging or publication is authorized by these receipts.
