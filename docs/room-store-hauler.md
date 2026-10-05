# Room Store Hauler content component

The Hauler resolver accepts explicit images, Hauler manifests, and local file
inputs. It creates an owned temporary Hauler store, stages only those selected
inputs through JAT's `HaulerAdapter` (`sync_image_txt`, `sync`, and
`sync_files`), validates native `store info --output json` inventory evidence,
and exports that selected store with Hauler's `save` command. The local-image
path that reads from a host Docker daemon is not used.

Each inventory reference must carry a native SHA-256 digest and a recognized
native kind or native media type. The resolver records only those values;
registry names, input paths, and manifest contents remain out of the Room Store
descriptor and component metadata. Media types are preserved only when Hauler
returns them. Mutable tags are resolved through Hauler before reuse is
considered. Digest-pinned image selections and file inputs with matching
content hashes can reuse the prior component without Hauler export or Restic
backup.

The Room Store Restic component tree contains exactly `hauler-content.tar.zst`
and `metadata.json`. Its source digest covers selected inputs and Hauler
version; the descriptor carries that digest and version alongside native
content references. A copied Josh Room runner invokes JAT's native
`HaulerAdapter` inside the selected JAT Environment Artifact. The controller
and managed runtime exchange closed-enum request and result files under a
private temporary directory. Canonical private-path helpers apply and verify
POSIX modes or Windows owner-only DACLs for the temporary directories,
request/result files, and RCC receipts. RCC receipts and the worker result are
both validated. The private Hauler store, request/result files, temporary
directory, selected image list, and component stage are removed on every exit.
Signal or keyboard cancellation unwinds through the existing JAT `_run_cli`
process-tree cleanup. A passed cancellation token is checked before and after
each managed call; it is not polled during a blocking call. Each call also has
a bounded timeout.

The runner also exposes a closed `acquire_rcc` operation for Enter/restore. It
validates the saved RCCA checksum and metadata, resolves the saved robot path
inside the materialized workspace with JAT's path helper, and calls JAT's
`RCCArtifactAdapter.acquire()` with strict artifact, specification, legacy-key,
version, and platform checks. It then runs JAT's `verify()` before returning
only the verified artifact digest, specification digest, and native platform.
RCC uses the injected executable and the selected private `ROBOCORP_HOME`.

JAT's current Build operation still composes a whole workspace capsule. It does
not expose a supplied-content export operation that composes a materialized
Room Store workspace with exact RCCA/Homebrew/Hauler component references. That
portable-export contract remains with JAT and is outside this capture module.
