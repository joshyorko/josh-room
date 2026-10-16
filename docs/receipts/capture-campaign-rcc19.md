# Capture campaign: RCC18.19.7 acceptance

The packaged native Save and relocated restore gate **passed** on 2026-10-16.
This supersedes the prior native Save failure recorded in the earlier campaign
receipt. It does not authorize merge, tagging, release publication, deployment,
or replacement of a user's installed extension.

## Exact source and candidates

Room draft [PR147](https://github.com/joshyorko/josh-room/pull/147) feature head
`c9a6fce16f49e01b6317693e5262315f98eff1c6` integrates main
`c0fe8066ff429b2826e10738fd4eef00159e1e39` (merged maintenance PR148) into
`ae4ab4334c25809772aff66780bc6af4762d8f64`. Integration was conflict-free.
Release pins and version0.1.35 are inherited unchanged from main. No PR was
merged by this campaign. Canonical/controller synchronization changed zero files.

Actual UI-tested VSIX SHA256:
`a5c49d032739f3a3f86904ed7a664a0f3aa867505f9679b61777cada8390d4bc`.
[Hosted review VSIX and receipt](https://github.com/joshyorko/josh-room/actions/runs/38901495573/artifacts/12046196317)
SHA256: `d4b1789a39cb0dba0b91743605e1ff6298d66f64ef79dd23d3bd6c05b26693b5`.
Downloaded hosted archive hashes were checked, and every unpacked file is
byte-identical to the UI-tested local VSIX. Archive-level hashes differ.
See `rcc19-hosted-vsix-receipt.json` and `rcc19-manifest-evidence.json`.

## Actual packaged UI result

Disposable Debian13 Docker/Xvfb, VS Code1.142.0, real SecretService, and isolated
Moto5.1.15 S3-compatible storage were used. Existing synthetic storage fixture
setup was retained; this is not new native Add Storage qualification or a live
MinIO/R2 provider test. Only a disposable profile/extension directory was updated.

Native Save completed at12:38:32 for `UI RCC19 Capture Fixture`: ten logical
entries,704bytes; restic added approximately4.3KB. Both manifest-pinned controller
and JAT artifacts imported through RCC18.19.7 without Build Locally fallback.
After verifying original hashes, only the disposable original source was renamed
unavailable. Native Enter Room restored to a different path and opened it at
12:39:23. Snapshot: `dd58b4152c624be4833b9167cfadd0c4`.

All five selected file hashes matched: application source, package manifest,
database, upload and retained worktree draft. Ignore-file bytes matched. All
`.git` entries, `generated-env`, and `worktree-two` were absent. All50 original
file/link hashes stayed unchanged. Exact inventory and hashes are in
`rcc19-native-restore-result.json`; the comprehensive UI receipt records scope.
Prior reviewed-selection/cancellation evidence remains distinct and unchanged.

Two setup failures were retained, not treated as clean upgrade acceptance:

- The earlier disposable profile retained a controller archive under a stable
  filename; it failed the new archive size check. Only that obsolete test cache
  was removed; the fresh download was verified.
- Fresh RCC18.19.7 initially reported an empty worker library set. Its Linux
  probe resolves `ldconfig -p` through PATH and silently treats command failure
  as an empty set. The genuine system executable listed the required loader.
  The disposable environment made the real executable discoverable via
  `/usr/bin/ldconfig` pointing to `/usr/sbin/ldconfig`, then restarted VS Code.
  Artifact import and the complete Save/restore flow passed. No probe output was
  fabricated, compatibility check disabled, artifact changed, or pin modified.
  See [RCC's Linux probe](https://github.com/joshyorko/rcc/blob/v18.19.7/environmentlifecycle/compatibility_linux.go).

## Automated and exact-artifact qualification

[Full hosted test job](https://github.com/joshyorko/josh-room/actions/runs/38901502683/job/116974612424):
**1904 Python passed,28 skipped;288 Node passed,2 skipped; Ruff and diff passed**.
Synthetic PR merge `c7509be167aa2a23bf040093b9b4184885f3a0b5` and feature head
share tree `852030972fc53b8249c3e3da78322990855255ef`.
[Capture qualification](https://github.com/joshyorko/josh-room/actions/runs/38901495573):
**77 passed each on Linux and native Windows, no skips**, including actual restic
Save/restore. [Windows receipt](https://github.com/joshyorko/josh-room/actions/runs/38901495573/artifacts/12047125609).
The separate managed-runtime Linux consumer passed; Windows consumer was still
running when this receipt was prepared.

Local focused container command passed111tests, skipped1 (restic absent from
that command's PATH; actual restic is independently covered above). Local native
extension suite passed197tests, skipped2. No host dependency installation:

```sh
python scripts/sync_controller_sources.py
python -m pytest tests/test_workspace_policy.py tests/test_workspace_init.py tests/test_source_only_capture.py tests/test_workspace_report.py tests/test_jat_capture_handoff.py tests/test_room_store_components.py tests/test_snapshot_core.py -q
node --test vscode-extension/extension.test.js
git diff --check
```

JAT draft [PR21](https://github.com/joshyorko/josh-all-the-things/pull/21) remains at
exact qualified source `e1332dd30e5aefd6820df36889adc86a4b7915dc`.
Its [embedded Linux candidate](https://github.com/joshyorko/josh-all-the-things/actions/runs/38893454020/artifacts/12042762601)
archive SHA256 remains
`d0ccc70eca1b6733f12fd48d1d135a85aec94a91a7c03141c624f767e71e99e7`;
RCC artifact `sha256:d53e6d36023f8ae8cb4cb6ac5a9da5773e6bad666bf617131c411f39ed5d1030`.
The integrated Room head consumed that exact hosted artifact again through
RCC18.19.7: filtered build/restore passed with original unavailable, selected
bytes intact, excluded entries absent, original unchanged and no JAT checkout
overlay. Capsule SHA256:
`bbc06676155b0a93466fb7d577aa61860b0dd90229e54ab766c0c3d0f9f284ae`.
See `cross-rcc19-capture-receipt.json`. Earlier native Windows embedded-artifact
qualification remains valid for its named source/artifact; it was not rebuilt.

## Remaining scope and next steps

#144 and #146 remain parallel capabilities; full #145 depends on both. Native
Room saves do not depend on JAT#19. Filtered portable export requires PR21 and
explicit adoption of its qualified artifact; this campaign did not change main's
JAT release pins to that unpublished filtered candidate.

Review both drafts and retain candidate artifacts before expiry. Before any
separately authorized release, decide artifact adoption, address or document the
cache/command-discovery setup issues, rebuild the exact release candidate and
repeat acceptance in its intended user environment. Windows visible UI and live
provider acceptance remain unrun. No screenshot publication was retried; only
sanitized textual evidence is included here.
