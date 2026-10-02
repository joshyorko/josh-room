# Managed restic runtime

Josh Room pins restic `0.19.1` for its current `linux-x64` and `win32-x64`
runtime matrix. The pin is recorded in
[`vscode-extension/runtime/restic-manifest.json`](../vscode-extension/runtime/restic-manifest.json)
and copied into the packaged Room template. Both archive URLs and SHA-256
digests come from the official restic `v0.19.1` GitHub release metadata:

| Platform | Official asset | Compressed SHA-256 |
| --- | --- | --- |
| `linux-x64` | [`restic_0.19.1_linux_amd64.bz2`](https://github.com/restic/restic/releases/download/v0.19.1/restic_0.19.1_linux_amd64.bz2) | `f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c` |
| `win32-x64` | [`restic_0.19.1_windows_amd64.zip`](https://github.com/restic/restic/releases/download/v0.19.1/restic_0.19.1_windows_amd64.zip) | `da948ad707ed690426473aaba2046cd61f8f90f6f0e7dab6be0d5796531de67d` |

Primary source: [restic `v0.19.1` release](https://github.com/restic/restic/releases/tag/v0.19.1),
including the release asset digests and sizes published through the GitHub
release API. The upstream [`SHA256SUMS`](https://github.com/restic/restic/releases/download/v0.19.1/SHA256SUMS)
lists the same compressed artifacts. Those sources do not publish separate
uncompressed executable digests, so the installer does not invent one: it
records the digest of the extracted executable beside the binary and checks
that digest before reusing the private cache.

The managed controller invokes the focused installer only at an explicit
Room Store operation boundary:

```sh
PYTHONPATH=/path/to/packaged/controller python scripts/install_restic.py \
  --manifest vscode-extension/runtime/restic-manifest.json \
  --destination "$ROOM_RUNTIME_ROOT" \
  --platform linux-x64
```

The controller must expose the packaged `josh_room` module (or the repository's
`src` directory) through its explicit `PYTHONPATH`; the installer imports
`josh_room.tls.system_ssl_context` and passes that scoped native-trust context
to `urllib`. It does not alter global SSL state or disable certificate checks.
Failed network downloads keep the public error message bounded and include only
a private diagnostic boundary, exception class, and numeric TLS errno or HTTP
status. Certificate text, response bodies, and paths are omitted.

`ROOM_RUNTIME_ROOT` must be the caller-owned private runtime directory. The
installer downloads only the exact upstream URL in the manifest, checks the
compressed size and SHA-256, extracts only the expected binary, checks that
`restic version` reports exactly `0.19.1`, and atomically promotes the binary
into a versioned subdirectory. It uses Python's standard `bz2` and `zipfile`
modules and does not call host package managers, shell extraction tools,
`restic self-update`, or a floating release URL. A failed download, checksum,
archive, or version check leaves no promoted executable. Cached executables are
rechecked against their local verified digest marker and version.

The installer flushes the extracted binary and digest marker through writable
file handles before atomic replacement. On POSIX it also fsyncs the containing
directory after promotion. Windows keeps file fsync, `os.replace`, and cache
digest/version checks; it skips directory fsync because Windows does not provide
the same supported directory-handle fsync operation. After sudden power loss,
directory-entry durability therefore follows the Windows filesystem's rename
guarantees.

This packaging prerequisite is lazy and standalone. It does not run during
extension activation or change catalogs, Room startup, repository format, or
provider configuration. Repository initialization remains explicit and uses
format 2 as required by Phase 0.

The release assets exist for Windows, but that alone does not prove Windows
packaging acceptance. Run the installer and the Room Store Phase 0 probe on a
real supported Windows x64 host before making that claim.
