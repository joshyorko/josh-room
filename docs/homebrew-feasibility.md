# Installed Homebrew recovery feasibility

Issue #190 has a deliberately narrow installed-prefix format, separate from
Brewfile recipes and existing prepared recovery exports. Qualified source capture
uses the existing `homebrew_recovery` component and native Save chooser. A recipe
or a generic prepared archive does not establish installed-binary recovery.

## Bounded disposable-prefix trial

On 2026-11-22, a fresh public Homebrew container installed GNU Hello 2.12.3 into
`/home/linuxbrew/.linuxbrew`. No host prefix or user state was mounted. The source
had no installed formulae before this installation. The source and clean target
both reported Ubuntu 22.04.5 LTS, x86_64. Exact image pins, bottle provenance,
archive/binary digests and result boundaries are recorded in the
[synthetic receipt](receipts/issue-190/homebrew-closure.json).

`brew deps --installed --include-implicit hello` returned no formula
dependencies. ELF inspection nevertheless identified a prefix loader alias and
the operating system's `libc.so.6` as runtime prerequisites. The original selected
archive contained only the exact Hello keg, its `opt/hello` link and `bin/hello`
link. It contained 58 regular files; all regular files were beneath the selected
versioned keg. It did not contain the Homebrew repository, caches, services,
configuration directories or other formulae.

A keg-only execution attempt failed without the prefix loader alias. The source
prefix's `lib/ld.so` pointed to `/lib64/ld-linux-x86-64.so.2`. For a second,
**synthetic declared-linkage archive**, the test reconstructed that observed
symlink as a separate explicit archive record. This link was not captured from
the original source archive and must not be described as restored source bytes.
The operating system's loader and libc were not archived or restored; the target
supplied them, and their digests differed from the source image's system
libraries. This trial therefore tests one package against an explicit compatible
OS ABI prerequisite, not byte-identical runtime-library restoration.

The target used a new empty 16 MiB executable tmpfs at the exact approved prefix,
with networking disabled, the base filesystem read-only, all Linux capabilities
dropped and no-new-privileges enabled. Extraction refused any pre-existing prefix
entry. The declared archive admitted only the selected keg, in-prefix opt/bin
links and the one explicitly named OS-loader link. No installer, `brew link`,
service or package hook ran on the target.

Observed results:

- **PASS:** clean-target process exited zero. `hello --version` reported GNU
  Hello 2.12.3, and a custom synthetic greeting matched exactly.
- **PASS:** all 58 restored regular-file SHA256 values matched the original keg
  archive. The target's complete 167-entry prefix topology matched the declared
  synthetic archive, with no unrelated prefix files.
- **FAIL, retained:** the first target used an implicitly non-executable tmpfs;
  execution failed before dependency loading. An explicit executable mount fixed
  that setup boundary. The keg-only missing-loader failure remained distinct.
- **NOT RUN:** source OS-library byte restoration, a formula with nonempty
  dependencies, existing-prefix collision handling, different prefix/distro/ABI,
  macOS, Windows, services, Room Store/JAT installed-prefix capture, and installed
  extension binary restore.

The separate earlier jq binary-only experiment also failed at the identical
prefix without its loader. The Hello result does not generalize to jq or a full
Cellar. Package metadata alone did not enumerate every operating-system
prerequisite in this trial.

## Earlier trial admission limits

At that earlier checkpoint, a selectable component still needed a closed versioned package/link/OS ABI
manifest, source provenance and consistent capture, declared dependency closure,
no-clobber target admission, containment rules for all links and hardlinks,
platform qualification, and complete snapshot verify/copy/export/remove/restore
lifecycle support. A system-loader link must be an explicit reviewed prerequisite
or normalized packaging decision; it cannot be silently admitted as an arbitrary
outside-prefix link.

The reliable fallback remains a separately approved native installation from a
reviewed recipe or qualified package source. This trial did not execute or verify
cold-host native reinstallation, and does not promise historical Brewfile
versions or generic package portability. Those earlier results alone did not enable installed-prefix selection. The
subsequent bounded format below addresses only its explicitly qualified case.

## Qualified exact-prefix v1

`josh-room-homebrew-installed-v1` currently admits only the tested Hello 2.12.3
executable and source loader/libc SHA256 values listed in
`src/josh_room/homebrew_prefix.py:QUALIFIED`. Qualification is Debian 12 x86_64
and the exact prefix `/home/linuxbrew/.linuxbrew`. A different bottle relocation,
package, runtime update, OS, architecture or prefix is unavailable until separately
qualified. `INSTALL_RECEIPT.json` is captured and hashed; its declared bottle/core
origin is admission metadata, not an independent cryptographic upstream attestation.
No package code is executed to discover dependencies.

The manifest has closed package/platform/prefix, file, directory and link fields.
Every regular file has a digest, size and safe mode. The selected keg has no
hardlinks, special files or internal symlinks. Only its five fixed aliases are
reconstructed. Captured OS loader/libc bytes live under `.josh-room-runtime`,
and in-prefix loader/library aliases point there. Package binaries are not patched.
The source's external loader alias and the normalized target alias are both
recorded. Archive members are inert regular files/directories; restore creates
only the validated manifest topology. Limits are 5,000 entries, 16 MiB per file
and 256 MiB total payload. The Brewfile fallback is inert and does not pin an
upstream historical bottle or prove native reinstallation.

Preview freezes the manifest and package bytes for that invocation. A fresh Save
capture must match the reviewed plan token, Room, Dimension, policy and latest
snapshot. The workspace remains baseline and must be separate from the selected
prefix. The immutable descriptor retains the existing Homebrew archive digest
and Restic reference; copy/delete/reachability semantics use that existing slot.

After an explicit component Save, export the component from an immutable recovery
point. Then inspect and restore it on a qualified clean machine. Run these three
commands in order; replace `MANIFEST_SHA256` with the digest returned by the plan.
The destination prefix must be absent, and its parent must be caller-owned and
not group/world writable. An existing prefix is never merged or overwritten.

```sh
josh-room snapshot export-homebrew synthetic-room --snapshot IMMUTABLE_ID --dimension synthetic-dimension --backend minio --output /tmp/selected-homebrew.tar.zst --json
josh-room homebrew plan-restore --archive /tmp/selected-homebrew.tar.zst --prefix /home/linuxbrew/.linuxbrew --json
josh-room homebrew restore --archive /tmp/selected-homebrew.tar.zst --prefix /home/linuxbrew/.linuxbrew --confirm-manifest-sha256 MANIFEST_SHA256 --json
```

Restore is offline and does not invoke installers, package hooks, `brew link` or
the restored program. It writes a private sibling stage and atomically promotes
with Linux `renameat2(RENAME_NOREPLACE)`. Regular cancellation removes the stage.
SIGKILL can leave a private `.room-prefix-*` sibling; interruption or an fsync
failure after promotion can leave a complete restored prefix despite an error
receipt. A failed final inode/content check returns `commit-uncertain` and requires
inspection before retrying; retries refuse any occupied prefix. Shared material
path/content rules also apply to captured and decoded package bytes. These checks
do not isolate files from a malicious same-UID writer after verification.

The [installed-prefix receipt](receipts/issue-190/homebrew-installed-prefix.json)
records the concrete test boundary. It does not establish full Homebrew restore,
other packages, locale/NSS behavior, services, mutable existing prefixes,
cross-distro portability, macOS, Windows or the published managed RCC artifact.
Work-JATT's broader installed tools remain unqualified. Retain the separate recipe
fallback and approve native reinstallation separately when the installed format
is unavailable; cold-host native reinstallation has not been tested here.
