# Issue 50 legacy broker custody correction

This 0.1.21 follow-up preserves 0.1.20 startup/readiness/progress behavior and
corrects both R2 sign-in compatibility and the missing broker-managed legacy
source identity path.

## Verified behavior

- Standard age-keygen comment lines are normalized to one strictly validated
  operational identity. Real-age tests confirm the derived recipient is unchanged.
  Multiple/invalid identities and invalid recipient types/counts remain rejected;
  diagnostics report categories, not material values.
- The original released R2 auth consumer rejects the commented-identity
  regression; the corrected consumer passes it. A user-mediated live R2 sign-in
  also passed in an isolated candidate runtime. No storage operation was performed,
  and temporary session material was removed without changing the original identity.
- A separate live, unauthenticated start/cancel probe showed that the deployed
  broker does not advertise purpose/capabilities and requests R2 scopes even for
  an encryption-purpose request. No browser approval or identity polling occurred
  for that probe. No exact deployed Worker version is inferred from this evidence.
- Explicit legacy migration offers broker retrieval or an existing local backup.
  Modern encryption-only source handoffs remain strictly capability-bound. A pre-purpose
  broker requires a separate modal acknowledging its R2 read/write authorization,
  then a fresh R2-purpose session with an explicit source-only compatibility flag.
  Malformed advertised metadata cannot be treated as missing metadata.
- Broker source retrieval writes only a private temporary old-identity handoff.
  It does not initialize MinIO, relabel the destination as R2, alter existing R2
  session/configuration files, or retain returned storage credentials. Source and
  destination recovery handoffs remain separate and are cleaned after success,
  error, or cancellation, including interrupted writes.
- Planning remains read-only; execution still requires the separate migration
  confirmation and matching source-catalog ETag. Ordinary MinIO operations and
  new MinIO keysets remain independent of Cloudflare.

## Local gates

- Python with real age: 549 passed, 6 skipped.
- Node 22 extension tests: 226 passed, 2 skipped.
- Worker protocol tests: 7 passed.
- Ruff, source/template/controller parity, builds, packaged secret scan, and
  `git diff --check` passed. The tracked-source scanner's existing synthetic
  redaction-test false positive is not a credential; no scan rule was weakened.

## Installed candidate acceptance

The unmodified installed 0.1.21 candidate passed in Code Insiders with its native
Node extension host, actual controller, real age, and controlled loopback broker/S3
fixtures. Candidate SHA256:
`98328832459a5b6b6646c1cd1837cb8cc1be536c83615188f269ec262410568a`.

One browser context connected MinIO and R2, then completed three real registered
migration-command flows: modern encryption-only broker, existing local backup,
and pre-purpose broker with explicit R2-scope consent and a fresh authorization.
Each reached the read-only preview and returned cancelled. Every source/recovery
handoff was removed; all four existing R2 runtime files remained byte-identical.
The S3 fixture recorded zero write attempts. No controller, runtime, or VS Code
API was mocked. A companion command forwarded the real cached Dimension node to
the registered migration callback and observed its completion.

The preservation baseline was taken only after new R2 sign-in completed; merely
observing cached session files could otherwise capture the previous grant.
Published assets and downloaded-release installation are verified separately.

Skipped suite gates remain private first-run, live MinIO storage, live R2 storage,
the unavailable full JAT/Hauler vertical, secure-room registry/tool acceptance,
opt-in large streaming, and the two optional managed-runner Node fixtures.
The live R2 sign-in check above is not live R2 storage acceptance. No live MinIO
migration, rotation, deletion, storage write, JAT payload mutation, Worker deployment,
infrastructure change, or shared-cache removal was performed.
