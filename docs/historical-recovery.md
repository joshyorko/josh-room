# Historical file recovery (draft)

`josh-room recovery tree|inspect|diff|extract ROOM --dimension DIMENSION --snapshot EXACT_ID --path RELATIVE --json`
uses the existing authenticated catalog/descriptor context, including ciphertext
verification and Dimension credentials. `latest` is rejected. The selected
Logical JAT must match its catalog, Dimension, encryption domain and repository.
The Restic snapshot tree must match the descriptor. There are no catalog writes,
Room hydration, migrations or marker updates.

`tree` supports `--offset N --limit N` (1–500 entries per page) within the selected
directory; the returned `next_offset` resumes that same immutable directory.
Directories are read on demand via content-addressed tree blobs. There is no
global workspace item ceiling, but each tree/blob must fit the existing bounded
Restic capture (4 MiB), with a 128 MiB aggregate read budget per request. A larger directory fails closed rather than appearing
complete. Paging rereads the selected directory; this is not a persisted index.

`inspect` reports a SHA-256 and size by default. Explicit `--include-text`
requests a complete admitted UTF-8 preview of at most 2 KiB, with digest-only
fallback for excluded, sensitive, binary or larger files. Browsing does not read file content.
Filenames are returned only to the authenticated caller, never written into a
new remote index. The existing Restic cache remains governed by its private
runtime lifecycle.

`extract --destination ABSENT_PATH [--dry-run]` recovers exactly one file or
subtree, never the root workspace. Dry-run reads metadata only. Recovery rejects
links, special files, portable case/Unicode collisions, traversal and Room
markers, with a 10,000-entry / 64 MiB logical content budget. It stages beside
the selected destination privately, verifies content-addressed blobs and staged
file hashes, and uses atomic no-replace promotion. Existing destinations are
never overwritten. Parent aliases/symlinks/reparse points are rejected. Do not
select a destination under a parent concurrently controlled by another user.
No historical code is executed or served.

| Format | Capability |
| --- | --- |
| Logical JAT / Room Store v1 | On-demand tree, digest inspection, selective recovery |
| Portable source-only / Hauler archive | Unsupported; use existing whole-Room restore explicitly |

Receipts distinguish `logical_bytes`, `blob_bytes_read` and `blob_calls` from
`network_bytes_transferred` (null: unmeasured). Restic can download complete
encrypted packs containing unrelated chunks. Plaintext blob counts do **not**
prove wire-transfer savings. Metadata/authentication calls are outside blob
counters. Cancellation between reads and before promotion cleans staging;
individual Restic commands retain the existing deadline.

Initial Linux synthetic acceptance with real pinned Restic 0.19.1: 3 snapshots,
six-byte historical file, 2 MiB unrelated binary: 2,552 plaintext blob bytes,
3 blob calls for listing plus selected recovery. Current file preserved;
Unicode directory recovered separately. This uses synthetic catalog authority
and a real local encrypted Restic repository, not a live object-store credential
or installed VSIX. Windows, wire-byte measurements, installed VSIX, native
RCC/JAT and live R2/MinIO/S3 acceptance remain unproven. v1.0.0 remains on hold.

## Comparison and native navigation

`diff --compare-snapshot EXACT_OTHER_ID` compares the same path within the
same authenticated Room/Dimension. File comparisons use verified SHA-256;
directory comparisons use immutable Restic subtree identities (no recursive
text diff). Added/removed/modified/unchanged are relative to the historical
source. `diff --workspace EXPLICIT_ROOT` compares a file to an explicitly
selected current workspace on Linux using no-follow directory handles and the
current capture exclusions. Windows current-file comparison uses native handles that reject reparse points
and hardlinks, hold ancestors against rename, and deny file writers/deletion.
UNC/device paths and unavailable native guarantees still fail closed.
Native Windows qualification is reported separately from source support.
Without `--include-text`, the existing digest-only JSON contract is unchanged.
With it, admitted text is available for native read-only comparison.

The native Logical JAT row offers **Browse Recovery Point…**. QuickPick keeps
its initial Room/Dimension/point while paging, navigating, inspecting checksums,
recovering to a Save Dialog destination, or comparing digests. Canceling a
picker makes no extraction call. Untrusted workspaces can browse metadata but
cannot recover, preview text or compare. Admitted text opens in the native
read-only source/diff editor; other results retain the JSON metadata fallback.
Historical content is never executed or served.

Final validation: 135 focused Python tests passed with real Restic 0.19.1.
The same synthetic six-byte recovery measured 2,412 blob bytes / 3 calls on the
final run (tree metadata varies with synthetic file timestamps). The earlier
2,552-byte observation is retained above; neither includes wire overhead or
authentication. Security/controller suites: 115 passed; the real symlink case
initially failed on host Restic 0.20.0, then passed with pinned 0.19.1.
Node suite: 375 passed / 4 skipped / 1 failed due to missing system-Python
botocore; the single failed case passed with the existing project venv on PATH.
The four new native-navigation cases passed. The VSIX packaged and installed
successfully into disposable isolated extension/user directories; installed
controller help and source byte checks (package JSON ignores VS Code-added
installation metadata) are separate from UI acceptance.
No installed interactive QuickPick/diff or live provider acceptance is claimed.

## Installed native helper acceptance, 2026-11-22

At source `a112b891bcc429e688b870498decc23c067e3347`, the built VSIX was
installed in disposable user/extension directories under VS Code Insiders
1.148.0 (`0d15d4d60248e35fac1a3fe234d74ed780e15691`, Linux x64). A private
Xvfb display with TCP disabled hosted the real native QuickPick and Save
Dialog. An acceptance-driver extension loaded the byte-matched **installed**
`recovery-actions.js`, passed real VS Code APIs, and dispatched its requests
through the installed CLI parser/recovery implementation against the real
pinned encrypted Restic fixture. Catalog authority was injected synthetic
fixture context, not a live provider/keyring or the registered row command.

Observed and inspected screens: pinned `jat-ui-old` directory listing,
`changed.txt` actions, current-file digest comparison (`modified`), native
absent-destination Save Dialog, and recovered JSON receipt. The recovered
file was exactly six bytes with SHA-256
`b640e840b19d378660b32fb51ae18d67dccb4a8596a29e7bd72c1b2ae5928f41`.
Its extraction reported one file, 1,212 plaintext blob bytes and two blob
calls. All three current-workspace file hashes (including the 2 MiB unrelated
binary) matched the pre-recovery inventory. Zero staging directories remained.
Canceling the subsequent picker ended the session without another write.
These are native **installed helper** results, not production row-command,
managed RCC/controller, provider ciphertext-authority or keyring acceptance.
At that earlier source, text diffs were withheld and comparison used JSON
metadata. The bounded text follow-up below supersedes that limitation.

The initial disposable D-Bus probe found no `org.freedesktop.secrets` service.
A bounded task-owned container setup is being evaluated independently; the
user's real keyring and credentials are never used for this acceptance.

## Registered command with real synthetic provider/keyring

A subsequent Linux trial passed the actual installed
`joshRoom.browseRecoveryPoint` command through normal Dimension discovery,
age-encrypted catalog/descriptor validation, real loopback MinIO, and a
network-isolated native Secret Service container. Native browse, current-file
digest comparison and Save Dialog recovery all passed. All four current
workspace files, including the Room marker, retained their pre-recovery
hashes, and no staging directory remained. The selected first recovery point
was preserved. This supersedes the earlier injected-catalog limitation for
this trial, but **not** the runtime limitation: the existing runtime test hook
selected source Python/installed controller in `local-build-fallback` mode.
Immutable RCC packaging remains unproven. The first attempts exposed and
corrected harness lifetime (MinIO child exited) and runtime-mode (host age
correctly rejected in managed mode) mistakes; neither failed attempt is counted
as a pass.

See the [sanitized native receipt](receipts/issue-209-native-recovery.json) and
[reusable isolated acceptance recipe](recovery-acceptance.md). The parameterized
provider setup script was then rerun in a second fresh keyring/socket/port
namespace and successfully enrolled another three real encrypted snapshots.
No real credentials, host keyring, source Room mutations or release actions
were used. Windows recovery now has a focused non-release CI lane with checksum-pinned
Restic 0.19.1, real encrypted three-point file/Unicode-subtree extraction,
current-file comparison, hardlink/junction/writer denial, and no-clobber races.
The exact source job passed all four native cases on Windows x64 with Restic
0.19.1; see the receipt below. Wire-byte counts and text diff policy remain open.

## Native Windows comparison boundary

Local-drive comparison opens every ancestor using `CreateFileW` with
`FILE_FLAG_OPEN_REPARSE_POINT` and no delete sharing; all handles remain held
until the content read completes. Reparse attributes are rejected before the
next component is opened. The final regular, single-link file is opened for
read with read sharing only, denying existing/new writers and deletion. Size
and bounded SHA-256 are read from that handle. Missing native APIs, UNC/device
paths, directories, hardlinks and reparse points fail closed. Capture exclusions
are applied while the root and existing ignore file are locked. The code adds
no overwrite/current-workspace restore path. Atomic selected extraction reuses
the existing native Windows no-overwrite rename and private ACL staging.

The `windows-recovery` job in ordinary CI installs the checked-in pinned
Windows Restic binary, then runs `tests/test_recovery_windows.py` and uploads
the pin receipt and JUnit evidence. A missing executable is a test failure,
not a skipped native vertical. Catalog authority in that job remains a
synthetic fixture; Restic, NTFS handles, private ACLs, junction denial, current
comparison, selected writes and no-clobber promotion are actual Windows
operations. This does not prove Windows provider/keyring or installed VSIX UI.

## Hosted native Windows result

At implementation source `ddc330341a9a6914f09d807f6c63b0fb4d978b66`,
[the ordinary Windows recovery job](https://github.com/joshyorko/josh-room/actions/runs/45034616072/job/137245466482)
passed **4 tests, 0 failed, 0 skipped**, in 13.82 seconds with Python 3.13.17
and checksum-pinned Restic 0.19.1. The tests performed real encrypted Restic
file/Unicode-subtree recovery and native current-file comparison, hardlink,
junction and writer rejection, existing-destination and racing-destination
preservation, unchanged current file/Room marker, and no staging residue.
Catalog authority remained a synthetic fixture. This closes the focused
headless Windows recovery gate, not Windows live-provider/keyring or installed
native VSIX UI acceptance. The [sanitized receipt](receipts/issue-209-windows-recovery.json)
links the uploaded pin/JUnit evidence and records its artifact digest. Wire
bytes remain unmeasured and are not inferred from plaintext blob counts.

## Bounded historical text preview and native diff

The former `withheld-policy` result was an implementation placeholder, not a
repository-wide prohibition. This slice reuses existing capture defaults and
captured `.josh-roomignore` exclusions, material-security credential path
classes/content patterns, CLI credential-key and identity sanitization, and
Workspace Trust. It adds no policy file, private-data prerequisite or new
blanket approval. Snapshot preview uses defaults plus the captured ignore file
when present; it does not invent a missing historical policy index.

`inspect --include-text` and `diff --include-text` opt in to full-file UTF-8 text
of at most 2,048 bytes per side. Admission precedes encoding. Sensitive paths,
credential assignments, shared content matches, non-UTF-8/control-byte files,
excluded paths and oversized files return explicit states and metadata/digests,
not partial text. Excluded current files are not read and report unknown
comparison coverage. A↔B and historical↔current use the same boundary. The
ordinary inspect/digest API remains unchanged when the flag is absent.

The admitted envelope is base64 UTF-8 so existing JSON sanitization cannot
collapse indentation, CRLF, spaces or Unicode; it stays within the existing
4,096-character scalar/64 KiB result budgets. Encoding is not security: the
extension decodes it only after canonical encoding, byte-size and SHA-256
checks against the separately reported historical/current digest. Missing,
malformed, oversized, sensitive or mismatching envelopes retain digest fallback.

Native **Preview text** and comparison actions use an opaque random URI and
read-only `TextDocumentContentProvider`, then the normal `vscode.diff` editor.
Only a safe filename extension is used for syntax association; provider,
Dimension, Room IDs, original paths and content do not enter the URI. The
provider retains at most 32 small buffers in memory and drops them when their
documents close. It creates no compare files or workspace artifacts, webview,
server, filesystem daemon or executable preview.

Text requests are pipe-only in the extension; no runtime result-file path is
passed. The CLI independently strips reversible envelopes from any requested
runtime receipt. Existing stdout suppression prevents preview data entering
extension logs/progress, and metadata fallback removes payloads. Admission is
bounded pattern-based protection, not a claim to detect every possible secret.

Focused follow-up verification: 148 Python PASS/4 native-Windows SKIPPED on
Linux, including real pinned Restic; 380 Node PASS/4 SKIPPED. Regressions cover
preview→actual CLI serialization→native provider bytes, Unicode/whitespace,
credential/binary/oversize/omitted/tampered denial, digest fallback, untrusted
preview denial, readonly opaque URIs, close cleanup, and no runtime receipt,
log or progress payload. Updated native Windows text tests remain a separate
exact-head CI result; earlier Windows evidence does not prove these changes.
