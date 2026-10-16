# Room 0.1.36 feature candidate

Historical pre-adoption checkpoint. Room147 and JAT21 are now merged, and
[approved JAT0.1.14 publication](jat-v0.1.14-publication-receipt.json) is complete.
Runtime adoption is qualifying in [Room150](https://github.com/joshyorko/josh-room/pull/150).
The historical package/source receipts below remain attributed to their original runs.

Published maintenance Room 0.1.35 and template 0.1.5 remain unchanged at
`e773963106829668c9bca8bf94c045c99f8267f1`. The subsequent feature candidate
advances the extension to 0.1.36 and template to 0.1.6 through the existing
repository version-update helper. All package/runtime/candidate locks and both
bootstrap pins agree. Runtime artifact pins remain the existing maintenance
pins pending deliberate filtered-JAT adoption.

## Exact packaged native Linux flow

Source `62fd601f50f5bd67823a8b3a474c0036f2f60b29` completed the entire visible
native flow on one installed 0.1.36 package: create a Room for a source distinct
from the editor workspace, initialize and edit exclusions, scan four Git blockers,
review a zero-preselected picker, cancel without changing ignore bytes, restart
and confirm one exact worktree exclusion, rescan and retain three main repository
blockers, explicitly confirm source-only, rescan to a complete valid result,
preview, Save and restore at another path.

Actual UI-tested VSIX SHA256:
`7472a9624da838e761554d3b06c610b7aad116f5924ccefb38d9f5d151cb54a1`.
Snapshot `fd4cf050b0144ad6b811f353d1b1d1a9` restored with its original path
unavailable. Selected project and retained worktree draft bytes matched; the
ignore file matched exactly; all Git metadata, generated content and the omitted
checkout were absent. All 47 original files and Git records remained unchanged.
Root independently verified the VSIX hash/current source payload, actual restored
files and all original hashes. The encryption-ready notice remained visible
while Save continued.

Environment: disposable Debian 13 Docker/Xvfb, VS Code 1.142.0, RCC18.19.7,
native Secret Service and Moto5.1.15. No fake keyring sentinel, test system-tool
symlink, cache clearing, local runtime fallback or CLI credential seed was used.
The prior native connection and disposable recovery identity were reused.
This does not qualify live MinIO/R2.

See `62fd601-native-ui-receipt.json`, `62fd601-native-flow-extract.json`,
`62fd601-native-cancel.json` and `62fd601-native-restore.json`. Raw renderer
text/logs and screenshots are not published. Earlier 4bfe/995/505 receipts remain
attributed to their original candidate identities, not substituted for this run.

## Windows GUI qualification and earlier failure

The [f41 Windows renderer run](https://github.com/joshyorko/josh-room/actions/runs/38931772592)
verified its installed payload, selected the exact native command row, downloaded
and verified RCC, and imported the controller artifact. Controller execution
failed before creating its receipt. `windows-ui-f41c608-failed.json` preserves
the sanitized failure. Full Windows Save/restore remains NOT RUN.

The [113 Windows diagnostic run](https://github.com/joshyorko/josh-room/actions/runs/38933500853)
revealed `cmd.exe` missing from the controller PATH. The shared runtime helper
copied Windows environment keys into a case-sensitive object, then the controller
launcher read only uppercase `PATH`, losing an inherited `Path`. Source
`62fd601f50f5bd67823a8b3a474c0036f2f60b29` fixes that conversion and the final
GUI environment merge, with template mirrors synchronized. The regression failed
before the fix; all 304 Node tests now pass, with 2 skips. The actual Windows rerun [38934426184](https://github.com/joshyorko/josh-room/actions/runs/38934426184)
passed at exact source62fd601: installed payload identity, native source-only
valid result, a fresh full-mode Git rejection and unchanged original Git pointer.
Its VSIX SHA256 is
`d5c007418cde5a5f6d5ba6e11a21eed60a9a7abb73d57e4b5d395d2fbbd62e71`.
See `windows-ui-62fd601-passed.json`. Full Windows UI Save/restore remains
NOT RUN; this narrower gate is separate from backend managed-runtime checks.

## JAT integration and publication gate

JAT PR21 head `2f9c671985cfcc055d5ab54e6e2e5bc5127c78f6` passed [both platform
candidate and schema-v3 promotion checks](https://github.com/joshyorko/josh-all-the-things/actions/runs/38932280780).
The change also makes the read-only candidate workflow run on main, so an
eventual merge source gets new embedded-source artifacts rather than relabeled
branch receipts. Existing exact d084 cross-repository restore evidence remains
preserved in `cross-final-filtered-capture-receipt.json` at its original source.

The proposed next supporting runtime is `v0.1.14-jat-runtime`, RCC18.19.7.
It is not published or release-ready. After explicit owner approval, PR21 merged
as `fe43065a654d1649ba9ebe5a6f202eac612e4385`. Its exact main-source qualification
passed both platforms in workflow 38934211098. Exact archive identities and
recovered native freezes are recorded in `jat-v0.1.14-runtime-proposal.md` and
`jat-v0.1.14-publication-plan.json`; supporting publication remains unapproved. Earlier branch artifacts are not relabeled
as this main source.

Room PR147 remains unmerged while its final hosted RCC qualification completes. Qualified
feature integration is owner-approved. No feature release was tagged, staged against trusted
main, published or deployed. Final Room publication remains with Josh. The existing
release workflow will stage only an exact qualified main candidate; no old
0.1.35 source, stage run or artifacts may be reused for the feature release.


Room62fd601 consumed the exact JAT main-source Linux artifact12060820191.
Filtered build and restore passed using Python from that acquired artifact,
without a source overlay. Selected source/database/manifest/worktree draft bytes
matched, exclusions were absent and originals were unavailable and unchanged.
Capsule SHA256 `4f5d551166a8b0bbf1880002aa59fcbb313c10857772e245342ac49e6d5bdfd2`,
2,185 bytes. See `cross-62fd601-capture-receipt.json`. A prior minimal-container
attempt failed library compatibility due to absent X11; the successful run used
the native selected environment and retained the safety checks.
