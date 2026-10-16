# Proposed v0.1.14-jat-runtime: exact merged-source candidate

Proposal only; no tag, release, registry write or publication executed. Proposed source: fe43065a654d1649ba9ebe5a6f202eac612e4385 (merged JAT21), RCC v18.19.7, Hauler v2.1.1. Do not substitute earlier branch artifacts.

Candidate run https://github.com/joshyorko/josh-all-the-things/actions/runs/38934211098 passed Linux and Windows embedded restore and actual schema-v3 promotion gates. Standard push run38934211121 passed Linux/Windows; publishing jobs skipped. Both downloaded ZIP hashes/sizes and contained archive hashes/sizes, source identity, artifact/specification digests and schema-v3 receipts verified. Linux freshly reacquired and requalified without external source overlay, exit0.

- linux: artifact https://github.com/joshyorko/josh-all-the-things/actions/runs/38934211098/artifacts/12060820191; archive SHA256 `de39c0f9374dfae541ee8688b643ed53e950331ee4efe9ca498c6b28b89cdf09`, 170877798 bytes; RCC `sha256:ce8617814b77a9080f0efc8247029b00da03b10a68cef329693d4037365ea5e4`; specification `sha256:b4193059c5e1d4823c4d8c60e8520802145513cd1cb6a4f207466fd098e60431`.
- windows: artifact https://github.com/joshyorko/josh-all-the-things/actions/runs/38934211098/artifacts/12059169968; archive SHA256 `8bbf7aa8925b5a517164e7e6ea00b735394cc1eac026ba34404e729d89e441b1`, 104094478 bytes; RCC `sha256:65e87f6a17a8d92185d5b99aa4928517dc0bbdf61cabeaf1670f74bf17881f7e`; specification `sha256:716adb4f9b65b98f954ec564000ffb981562748b25a90d9f02947a17e3f7157b`.

HTTP mapping: copy unchanged Linux archive to jat-runtime-linux-amd64.rcca and unchanged Windows archive to jat-runtime-windows-amd64.rcca; copy each unchanged promotion-receipt.json to the respective jat-runtime-{linux,windows}-amd64.json. Keep original jat-filtered-candidate.rcca filenames for the read-only qualifier and native OCI publisher. Mapping changes asset names only, never bytes or receipt identity. Release URLs would be https://github.com/joshyorko/josh-all-the-things/releases/download/v0.1.14-jat-runtime/jat-runtime-{linux,windows}-amd64.rcca. Existing native provenance mapping must record these SHA/size values, exact source/run, and actual separately authorized OCI carriers. Verify v0.1.14 remains unused immediately before approved staging.

Descriptor evidence: each local artifact directory contains descriptor-provenance/{manifest.json,specification.json,normalized-blueprint.yaml}. Bytes extracted from the exact archive are verified against manifest digest/size, receipt specification digest and specification.normalizedBlueprint. Both include the exact source embedding postinstall. These are input provenance, not newly generated resolved freezes.

Resolved-freeze packaging gate PASSED. Exact Linux acquired artifact generated a native resolved freeze through --no-build ht vars and --no-build task run JAT --help. Exact Windows archive was freshly acquired in isolated native Windows run https://github.com/joshyorko/josh-all-the-things/actions/runs/38935421625 (SUCCESS), proof driver74641d260210a2824a5bbf54dcb966694a537865, artifact12061575165. No-build descriptor resolution and native JAT --help generated the resolved Windows freeze; retained archive hash unchanged, embedded JAT imported under acquired prefix with sourcefe430. Driver is evidence-only and is not the qualified artifact source. Both freezes retain the exactfe430 source embedding postinstall.

Windows freeze SHA256 a3e029f0417aa52ee76fd2dd7a75176ec1f6e0f2662bc6f709de6c8e08616d30,843bytes. Its proof used checksum-pinned RCC18.19.7 Windows binary d3338be49ef22bbd9f3e42b606e01231cb5c5d86716b18d24fadf85e7630e0be. The exact freeze bytes and recovery receipts accompany this proposal as jat-fe430-{linux,windows}-resolved-freeze.yaml and jat-fe430-{linux,windows}-freeze-receipt.json.

Publication remains pending explicit supporting-runtime authorization; no technical freeze-recovery blocker remains. Assemble the native eight release assets using these recovered freezes, unchanged renamed archives and promotion receipts, actual approved-publication provenance and SHA256SUMS. Do not rebuild from the recovered freeze: it is supporting evidence for existing archive bytes, not a replacement artifact specification. Exact source/release tag/provenance and remote uploaded hashes must still be verified at approved staging. Final feature Room publication remains Josh's action.


## Requested supporting publication scope

The proposed action publishes only supporting JAT runtime v0.1.14-jat-runtime
from exact main fe43065a654d1649ba9ebe5a6f202eac612e4385. It preserves the qualified
archive bytes, pushes the two platform artifact carriers through the existing
native publisher to ghcr.io/joshyorko/josh-all-the-things-jat-runtime, and creates
and publishes the new GitHub runtime prerelease with its eight native assets.
The resolved freezes and unchanged schema-v3 receipts accompany the archives;
provenance records actual carrier digests after the approved writes. No existing
release/tag is replaced. Recheck exact current main and unused v0.1.14 before any
write; a moved source requires fresh qualification.

This proposal does not authorize final Room publication. Room artifact adoption
must verify the actual published runtime asset metadata and exact receipts first.
