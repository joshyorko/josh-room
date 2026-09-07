# Exact tested candidate promotion

Josh reported successful installed 0.1.24 acceptance and authorized promotion
of that exact VSIX. The archive SHA256 is pinned in release-promotion.json.
The initial source commit containing the tested controller and extension is
18446eb8480f2f6390446effaf7641b7879d2984. Subsequent release-only verification
and metadata edits do not alter any packaged file. Final tag/merge source
identity and downloaded publication evidence are recorded in release notes.

The tag workflow requires an existing unpublished draft targeted at its exact
source SHA. It verifies the archive checksum, package versions, source bytes,
and complete packaged file inventory. Only Python ancillary artifacts and
SHA256SUMS are built/uploaded; the VSIX is never rebuilt or overwritten.
Verification repeats before and after publication. Missing or changed bytes,
source, tag, or draft identity fail closed. Cleanup is a separate authorized
operator action and requires a verified public download first.

The release set authorized by Josh is the current working 0.1.24 release plus
v0.1.10-controller-artifacts. The controller release, assets and URLs are
retained unchanged. No tags, branches, history, local candidates or MinIO data
may be deleted. Complete paginated inventory and post-cleanup readback are
required. Concurrent repository-admin mutation is not an atomic GitHub API
guarantee; post-publication verification detects drift before cleanup proceeds.

Verification covers controlled CLI workflow execution and candidate/source
drift tests, full Python/Node suites, lint, shell/YAML checks and archive parity.
Source secret scanning retains the pre-existing synthetic redaction fixture
finding; no allowlist is weakened. Packaged secret scanning is separate.
Josh's installed acceptance is user-reported. A fresh Windows installed gate,
the full real JAT/Hauler vertical and secret-gated live storage checks were not
run by this promotion and must not be reported as passed. No runtime URLs or
runtime component versions were repinned for this promotion.
