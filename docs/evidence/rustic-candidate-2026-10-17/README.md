# Patched Rustic performance checkpoint

Source: `1a1e9634f61370edd2e1ee1bdb665a3a3edc5c92`. Actual production adapters, encrypted local repositories, 100k files, 250 directories, 1000 distributed same-size/restored-mtime edits. All four engine runs passed full byte comparison, full-data check, inventory equality and no-op/two-snapshot checks. Cold means application caches; OS caches were not evicted. Synthetic catalog/keyset callbacks; no live provider.

| Seconds | Restic control A | Official Rustic | Restic control B | Patched Rustic |
| --- | ---: | ---: | ---: | ---: |
| initial_save | 23.499 | 62.269 | 23.688 | 37.300 |
| changed_save | 19.302 | 47.925 | 18.340 | 20.012 |
| warm_unchanged_save | 3.131 | 4.382 | 3.090 | 3.245 |
| inventory_cold | 5.148 | 31.751 | 5.418 | 3.225 |
| inventory_warm | 0.402 | 0.424 | 0.384 | 0.428 |
| staged_restore | 18.339 | 8.991 | 16.683 | 10.809 |

Cold inventory changed from 253 commands to 2: one snapshot lookup and one authenticated `ls --json-nodes` traversal. The patched run used exactly one bulk listing and no per-tree CLI calls. Its cold inventory was 0.60x its Restic control; changed Save remains 1.09x, initial Save 1.57x, unchanged Save 1.05x and restore 0.65x. These are initial observations, not repeated convergence proof.

Linux candidate binary SHA-256: `875328943343d8a14077ca1489a8f8f65b8a771f3dd1625885bc55071b1f0197`. Build receipt honestly records its prepared working-tree provenance. Both benchmark receipts bind the clean tested source SHA and exact adapter/instrument file hashes.

Linux native: 44 PASS, 3 SKIPPED. Scoped TLS/S3, strict invalid-CA construction and same-process trust isolation PASS. Earlier unverified-tree binary failed both replay rejection assertions; corrected root/subtree cases pass. Native Windows: 42 PASS, 5 SKIPPED, then TLS FAIL at the initial version command under the S3/CA profile; candidate managed composition was NOT RUN. Official Windows read-only failure remains visible. See [Actions](https://github.com/joshyorko/josh-room/actions/runs/39041714530).

Remaining work: profile and close changed/initial Save gaps, repeat comparable controls, measure memory, cover small/deep/larger-byte workloads, diagnose Windows TLS and finish managed Windows acceptance. No final optimized result is claimed. Mixed-engine destructive maintenance remains blocked; no pins, staged 0.1.36, merge, release, PR or user storage changed.
