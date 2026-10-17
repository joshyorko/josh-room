# Disposable Rustic candidate qualification

The user explicitly authorized building and testing the previously proposed
patches in disposable checkouts, without distributing or adopting a fork.
Restic stays default and all runtime/release pins remain unchanged.

- [x] Verify unchanged branch heads and exact upstream source `6b96f976ff437a0528a4742358b172b32c3e3932`.
- [x] Reproduce official CLI rejection of the proposed bulk flag.
- [x] Add strict schema-1 snapshot binding, exact node records and a required completion record to the CLI patch.
- [x] Implement and observe RED/GREEN for strict adapter parsing; preserve bounds, modes, links, cache safety and destructive-operation refusal.
- [ ] Build release-mode Linux and native Windows candidates with the core read-only correction and scoped CA backend patch.
- [ ] Exercise native exact metadata parity, wide trees, cancellation, installer guards and complete managed Git/JAT/age recovery.
- [ ] Exercise scoped CA success and negative cases without system trust changes; keep fixture/provider limits explicit.
- [ ] Run complete comparable production-adapter 100k benchmarks and retain correctness and command timings.
- [ ] Review the implementation, run broad checks and push source patches, reproducible build instructions and source-bound receipts to the existing branch.

Candidate-only constructor options are passed explicitly by qualification
harnesses. The product does not infer capabilities from a same-version custom
binary and does not select or install the disposable candidate.
