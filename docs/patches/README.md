# Upstream Windows boundary

`rustic_core-0.13.0-windows-readonly.patch` is an upstream patch recommendation,
not a replacement binary or a product runtime pin. It applies to the exact
published 0.13.0 crate used by Rustic 0.11.4. The archive SHA-256 is
`ddab48a7f892b4943d9bffe7be44746c443982ae06ffcc3b2be55b31999b1725`,
verified against Rustic v0.11.4 Cargo.lock.

The original Windows `LocalSourceSaveOptions::nix_infos` returns no mode, and
`LocalDestination::set_permission` returns success without setting anything.
The adapter receives `mode: null`; the old snapshot contains no evidence from
which it can recover read-only state. Blocking selection prevents new damage
but does not fix that engine defect or repair existing snapshots.

The patch records Go-compatible Windows mode values, 0666/0444 for writable
and read-only files and 0777/0555 plus the directory bit for directories. It
restores the supported Windows read-only attribute with `set_readonly`, using
Go's owner-write-bit rule, and leaves symlink targets alone. It does not claim
NTFS DACL, alternate data stream or arbitrary Unix permission preservation.
Missing old mode values remain unknown. This patch cannot recreate them.

The native probe adds exactly the same assertions to baseline and patched
core source. It requires two baseline assertion failures followed by two
patched passes. It verifies source flags and bytes, read-only and writable
restoration, and directory capture. It does not build or install a replacement
Rustic CLI. The ordinary native product gate still runs its strict restoration
assertion against the unchanged official binary.

```powershell
uv run python scripts/rustic_windows_upstream_probe.py --output dist/rustic-upstream/windows-core.json
```

Before adopting an engine containing this patch, rerun the complete CLI and
managed Git/JAT/age acceptance, including metadata application order, relocated
restore, read-only Git objects, directory traversal, supported symlinks and
read-only/writable transitions. Core unit proof alone does not qualify a new
engine. No upstream issue, PR or separate maintained fork was created.

## Scoped custom CA

This also requires an engine/backend change. The pinned `rustic_backend`
0.7.0 OpenDAL constructor passes the option map to `Operator::via_iter` and
installs the default HTTP transport. There is no scoped CA option in this
constructor. OpenDAL's reqwest transport 0.58.1 creates `reqwest::Client::new()`.
The pinned Windows verifier uses OS trust rather than the adapter's
`SSL_CERT_FILE` input. Passing the environment variable does not implement CA
support. The current Windows rejection must remain until this is addressed.

Recommended backend patch: add a named `cacert` option to
`rustic_backend::opendal::OpenDALBackend::new`, consume it before `via_iter`,
read a bounded PEM bundle, parse every certificate strictly, and construct a
fresh reqwest client with those certificates added. Keep normal chain and
hostname validation. Wrap it in `ReqwestTransport`, then `HttpTransporter`, and
attach it only to this operator using
`Operator::with_context(OperationContext::new().with_http_transport(...))`.
Do not replace the process-global transport or modify OS trust. The pinned
OpenDAL 0.58.1 API supports this per-operator context. A direct dependency on
the matching HTTP transport crate would be required in rustic_backend.

Required native tests are success with the explicitly supplied generated CA,
failure with a different CA, malformed/empty bundle rejection, hostname mismatch
rejection, no trust leakage between operators, and cold/reopened S3 Save and
restore. A disposable local TLS/S3 fixture can establish these contracts; it
does not establish live R2 or production MinIO acceptance.

Verified source archives, all from Rustic v0.11.4 Cargo.lock:

| Crate | SHA-256 |
| --- | --- |
| rustic_backend 0.7.0 | `a0a52bffdb1a55a66f08cfc91f87ad928e502444e94ea88fce1c48c58ead909d` |
| opendal-core 0.58.1 | `ec75551ff4cf3e57da98979f6a937aaa9ddb3915bf68cc17d03df733be6646ed` |
| opendal-http-transport-reqwest 0.58.1 | `ad4d4f19c3ce01126a30611f8e544eaa217104a278c889ac17c9374fe4f9e4ef` |
