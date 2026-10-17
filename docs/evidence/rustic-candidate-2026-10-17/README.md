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

## Profiled repeat before further optimization

Source: `c1d666a7c42a40e4c241394c277cb86d8f43b574`, clean tracked source; same candidate digest as above. [Raw receipt](benchmark-patched-profile.json) records phase timings, exact corpus/source/binary hashes, native command counts, CPU, observed memory, parsing, validation, scans, cache work and separate authentication/index calibration. Actual production adapter; all five correctness checks PASS for both engines. Profiling adds observation overhead; these timings are a repeat of the pre-optimization candidate, not a final optimized result.

| Seconds | Restic 0.19.1 | Patched Rustic | Rustic / Restic |
| --- | ---: | ---: | ---: |
| initial_save | 22.499 | 38.736 | 1.72x |
| changed_save | 19.491 | 20.184 | 1.04x |
| warm_unchanged_save | 3.800 | 3.528 | 0.93x |
| inventory_cold | 4.674 | 4.558 | 0.98x |
| inventory_warm | 0.394 | 0.584 | 1.48x |
| staged_restore | 19.075 | 12.344 | 0.65x |

Native command counts (Restic / candidate): initial Save 7 / 7; changed Save 5 / 6; unchanged Save 2 / 3; cold inventory 2 / 2; warm inventory 0 / 0; staged restore 5 / 5. Diagnostic calibration commands are separate from these operation counts. Candidate cold inventory still uses one bulk `ls`, with no per-tree subprocesses.

Native initial backup is the largest remaining gap: Restic 5.037s observed wall / 10.003s child CPU; candidate 21.653s / 64.452s. Observed backup peak RSS is 184872 KiB / 142052 KiB; corresponding Python phase peaks are 179244 KiB / 196920 KiB. These sampled peaks are lower bounds, not whole-run simultaneous process-tree maxima. Process creation is measured separately (candidate backup 0.003522s). Native lifetime includes pipe backpressure and caller parsing; overlapping spans must not be added together.

Separate native calibration: password-authenticated config reads 0.892s / 0.387s; one key extraction 0.845s; config reads with the derived key 0.013s / 0.011s; authenticated index/root reads with that key 0.184s cold / 0.105s warm. Repeated authentication is measurable; CLI process creation alone does not explain the gap. No credentials, argv, environment or native output are retained.

The next engine investigation is repeated per-file Rabin-table construction in `rustic_core` chunker setup. This is a source-level hypothesis, not yet a measured or implemented optimization. Changed Save parity, final optimization, small/deep/larger-byte regression gates and complete candidate Windows acceptance remain unfinished. The earlier Windows TLS result above is historical; the literal-profile fix in `7a0c5b6` has Linux candidate acceptance, while its Windows candidate job was still running at this receipt checkpoint ([run](https://github.com/joshyorko/josh-room/actions/runs/39067358187)). Official Windows failures remain separate. No live-provider result is claimed.
