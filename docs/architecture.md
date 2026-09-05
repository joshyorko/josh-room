# Architecture

```text
                         optional golden host
                    Room of Requirement image
                              │
                              │ may host
                              ▼
Josh Room native VS Code UX ──┬── managed pinned RCC
                              │       │
                              │       ▼
                              │  JAT Environment Artifact
                              │       ├── JAT runtime
                              │       ├── Hauler via JAT rccPostInstall
                              │       └── tar/zstd and JAT dependencies
                              │
                              ├── encrypts with age ──> selected physical bucket / Dimension
                              └── invokes JAT capture/restore/serve substrate
```

Josh Room owns logical projects, encrypted catalog resolution, snapshot
envelopes, encryption orchestration, local/R2 transport, safe hydration,
receipts, the CLI, and the thin Room template. It does not own base images,
Hauler capture internals, runtime environments, Actions execution, or agent
orchestration.

The public repository contains code, schemas, synthetic fixtures, tests, ADRs,
and the thin consumer template. Real credentials, age identities, catalogs,
snapshot objects, private paths, and employer/customer data stay outside Git.

The standalone VSIX owns acquisition of the pinned RCC binary and keeps its
private `ROBOCORP_HOME` under VS Code global storage. It consumes the immutable JAT RCC v18.19.3 Environment Artifact; JAT remains a separate source and
runtime substrate, and Hauler remains JAT-owned through its `rccPostInstall`
contract. RCC owns environment specifications, artifact verification, and
materialization. Save asks JAT for `rcc_environment=auto` and may carry typed
Environment Artifact receipt metadata inside the encrypted inner manifest.

The repository and OCI Dev Container template retain the digest-pinned Room of
Requirement image as an optional pre-optimized/golden-host path. It is not a
dependency of VSIX activation, JAT artifact acquisition, or normal Josh Room
operations.

R2 and MinIO are concrete provider backends. The physical bucket / Josh Room
Dimension is the encryption boundary: each bucket has its own operational age
identity and encrypted catalog. MinIO keyset enrollment and catalog reads do
not require Cloudflare; Cloudflare remains the R2-only authority.

Provider credentials and encryption material are separate. Private identities
and credentials never enter config, workspace files, logs, receipts, argv, Git,
or examples. A fixed MinIO keyset makes bucket credentials an intentional trust
decision while never storing a recovery private identity in the bucket.

Legacy MinIO migration re-encrypts only the outer trusted envelope. It never
rebuilds or restores JAT payloads; production migration requires explicit
approval. Runtime, CI, and live gates remain separate.

Josh Room remains public-first and native-extension-aware. It does not add web
UI, webview, daemon, generic provider framework, automatic bucket administration,
JAT changes, or MinIO infrastructure changes.
