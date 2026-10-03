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
content references. The private Hauler store, temporary directory, selected
image list, and component stage are removed on every exit. Cancellation is
checked before and after each Hauler adapter call; each adapter call retains
its configured timeout.

JAT's current Build operation still composes a whole workspace capsule. It does
not expose a supplied-content export operation that composes a materialized
Room Store workspace with exact RCCA/Homebrew/Hauler component references. That
portable-export contract remains with JAT and is outside this capture module.
