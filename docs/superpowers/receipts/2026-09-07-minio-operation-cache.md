# MinIO operation cache correction — local candidate

Save and New rejected ready migrated Dimensions when extension SecretStorage
had no operational-material entry. Catalog and migration controller paths can
resolve the selected bucket keyset without that cache. The cache is now optional
for selected operations; controller binding and identity validation remain.
Ordinary controller operations explicitly disallow initialization, including
when an ambient recovery handoff exists. Explicit initialization is unchanged.

Both Save/New regressions reproduced the exact reported error before the patch
and passed afterward. Both no-enrollment regressions failed before the controller
guard and passed afterward. Full verification: 562 Python passed, 6 skipped;
230 Node passed, 2 skipped. Ruff and git diff checks passed. Mirrored extension
and controller changes are included in the 0.1.23 candidate VSIX. Archive
integrity and extracted-package secret scanning passed. The source scan retains
the existing synthetic redaction-test finding; scanner rules were not changed.

Bounded Luna review found no further correction necessary; its own pytest was
unavailable. Test execution above was performed by the parent in the project
environment. No user bucket, catalog, recovery backup, or credentials were
modified. Installed post-migration Save/New and real JAT/Hauler lifecycle are
not verified. Secret/tool-gated tests remain skipped, not passed.

This is an unpublished local candidate, not release or issue-close acceptance.
