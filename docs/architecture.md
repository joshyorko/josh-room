# Architecture

```text
Room of Requirement image
          ▲
          │ consumed by
          │
      Josh Room ───── invokes ─────> Josh's All the Things
          │                              │
          │ encrypts with age            │ captures/restores Hauler payloads
          ▼                              ▼
 encrypted envelope ─────────────> private Cloudflare R2
```

Josh Room owns logical projects, encrypted catalog resolution, snapshot
envelopes, encryption orchestration, local/R2 transport, safe hydration,
receipts, the CLI, and the thin Room template. It does not own base images,
Hauler capture internals, runtime environments, Actions execution, or agent
orchestration.

The public repository contains code, schemas, synthetic fixtures, tests, ADRs,
and the thin consumer template. Real credentials, age identities, catalogs,
snapshot objects, private paths, and employer/customer data stay outside Git.

The personal template performs capability bootstrap only. Stable RCC remains a
normal tool inside the hydrated workspace; RCC Environment Artifacts are a
future environment-identity seam and are not a Josh Room v0.1 dependency.
