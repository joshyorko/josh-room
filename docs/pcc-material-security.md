# PCC material security boundary

`josh_room.material_security` is the mandatory caller-owned gate for logical
evidence material. It accepts only the closed session transcript, session
metadata, and session asset classes from known logical source kinds. A shell
history item can enter only through the distinct `sanitized.shell-history`
source kind after explicit declaration approval; it is never relabeled as a
Codex transcript. Structural classification denies credential authorities and
unsafe objects before content is read; credential/key-store subtrees such as
`.ssh`, `.gnupg/private-keys-v1.d`, and Helm registry storage fail closed even
for nonstandard child names. The approved root and every ancestor of the
candidate must also be free of symlink/reparse components. Fish history paths,
including XDG Fish locations, remain shell history and require the distinct
sanitized source. Adapter declarations cannot override those denies, while
ordinary safe files outside those stores remain eligible.

Content scanning is bounded streaming defense in depth. It recognizes a small
set of high-confidence synthetic private-key, bearer/API, cloud, connection,
cookie, AWS assignment, and credential-URL shapes across chunk boundaries. A
positive match blocks the logical item; an input over the configured bound is
quarantined. The bound counts bytes, including non-byte-format memoryviews
normalized before slicing. The scanner does not redact, rewrite, or return
matched values.

The scanner is intentionally not a DLP system. It can miss encoded, novel, or
split representations outside its patterns, and it can conservatively block a
future high-confidence credential-like string. Structural rules are the
authority for known unsafe classes; a clean scan is not permission to capture
work/customer material or to send anything to a destination.

Receipts contain only an adapter label, material class, decision, stable reason
code, count, and optional digest. Paths, values, environment contents, raw
exceptions, and matched content are not receipt fields or diagnostics.

## Consumer opening contract

The #4 gate does not open files, run a daemon, or provide a race-free capture
loop. Before #5 reads an allowed candidate, its adapter must open the approved
root and candidate using the platform's no-follow/reparse-safe primitives,
revalidate containment and regular-file identity on the opened handle, and
reject replacement, truncation, hardlink-count, or prefix-digest changes. The
adapter must stream from that stable handle through `scan_content` and treat
any open/read/revalidation error as quarantine with an opaque receipt. Policy,
profile, recipient, and destination authority remain outside this module.
