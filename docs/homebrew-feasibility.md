# Installed Homebrew recovery feasibility

Issue #190 keeps **installed-prefix/binary recovery unavailable**. A Brewfile is
an installation recipe; a prepared Homebrew recovery export is a separate
existing archive contract. Neither establishes installed-binary recovery.

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

## Admission remains blocked

A future selectable component needs a closed versioned package/link/OS ABI
manifest, source provenance and consistent capture, declared dependency closure,
no-clobber target admission, containment rules for all links and hardlinks,
platform qualification, and complete snapshot verify/copy/export/remove/restore
lifecycle support. A system-loader link must be an explicit reviewed prerequisite
or normalized packaging decision; it cannot be silently admitted as an arbitrary
outside-prefix link.

The reliable fallback remains a separately approved native installation from a
reviewed recipe or qualified package source. This trial did not execute or verify
cold-host native reinstallation, and does not promise historical Brewfile
versions or generic package portability. Keep actual installed-prefix selection
disabled while these gates remain open.
