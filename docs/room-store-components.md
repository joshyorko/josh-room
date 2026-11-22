# Room Store native RCC environment component

Save reports runtime capture, archive packaging, and archive verification as
separate progress stages. A first capture can take several minutes before file
backup begins. Cancel stops the operation; a cancelled attempt is not a
successful Save.

Room Store Save captures the root workspace `robot.yaml` environment through
the selected RCC runtime. RCC publishes the native Environment Artifact with
`rcc env publish --robot robot.yaml --json`, then exports that exact artifact
with `rcc env export --artifact <artifact-digest> --output
rcc-environment.rcca`. Josh Room records RCC's artifact and specification
digests, platform, RCC version, legacy blueprint key, and the safe relative
robot path. RCC remains the authority for environment identity; the local
source digest is only a reuse hint.

The component Restic snapshot contains exactly `rcc-environment.rcca` and
`metadata.json`. The metadata includes the canonical hash of the bounded
`robot.yaml` / referenced conda and pip input closure, selected RCC version,
and native host platform. Normal Save compares that hash and the current repository-bound
component descriptor before doing RCC work. An older descriptor without the
hash is recaptured once. Changed input invokes RCC publish/export and creates a
new component snapshot; an unchanged input reuses the prior component
snapshot/tree identity.

Source files are read as regular, non-symlink files with bounded size and
closure depth. The hash record contains relative source names and bytes only
while computing the digest; no source paths or content enter the descriptor,
logs, or component metadata. A source change during capture fails closed.
The RCC version is pinned to `v18.19.11`; absence, version mismatch, incomplete
native metadata, or an invalid export stops Save before publishing the logical
JAT. RCC publish, export, and archive verification complete before upgrading
the Room Store keyset or opening Restic. Failed RCC preparation therefore
performs no provider writes, including transient Restic lock writes, on fresh
or existing MinIO stores. Completed preparation records archive, reuse, or
absence explicitly; source changes do not restart RCC after Restic opens.
Component restore remains separate from workspace restore.

Homebrew recovery and Hauler content remain separate component owners and are
not inferred from an RCC capture.

In extension mode, component capture uses the exact managed RCC executable
handed off by the extension; a missing handoff fails closed instead of searching
the host PATH. Nested JAT execution puts that RCC directory first in its
inherited PATH; RCC supplies the selected artifact's own runtime tools while
retaining access to its host compatibility probes (such as `uname`).
Publish and export select RCC's local provider. If publication metadata omits
the platform, acquisition verifies the exact exported archive and published
digest under `--no-build --permissive-local` before component backup.
The private `ROBOCORP_HOME`, artifact pins, `--no-build` execution, and receipt
verification remain unchanged.

Capture failures report the command identity, stage, inner exit status, and
bounded sanitized stdout/stderr. Nested Build failures also retain allowlisted
RCC receipt and JAT result summaries before temporary receipt cleanup. Private
paths, credentials, and raw request arguments are not included in this evidence.
Preview does not run component capture or publish remote state.

Unexpected Python failures in native Save retain `error: save-failed` and add
`stage`, `cause_type`, and `diagnostic` to the public failure receipt. The stage
is the last entered Save boundary (for example, `rcc-component-prepare` or
`restic-store-open`). Only program-owned stages and a fixed list of built-in
exception types are exposed; other types become `UnexpectedError`. Exception
messages, arguments, custom class names, paths, and stack traces are omitted.
The diagnostic is a fixed sentence constructed from those allowlisted values.
Known capture, cancellation, deletion-confirmation, and publication errors keep
their existing receipts. A stage is not evidence of completed capture or
publication; validate the controller RCC receipt and any inner capture receipt
separately.

## Native Save component chooser (draft checkpoint)

New Room, Save Current Room and Save Folder As Room share one native multi-select.
Workspace is always included under the reviewed capture policy. Optional rows
request `keep`, `refresh`, or `omit` for the three existing closed descriptor
slots. Unchecking a row omits its reference only from the new recovery point;
immutable previous descriptors remain recoverable. Existing components default
to keep; RCC refreshes from root robot.yaml when selected. Non-secret mode choices persist in VS Code host global state, under a hashed
source/Dimension/Room key. Paths to external content and credentials are never
persisted. Refreshing external content always requires a new explicit source
selection; remembered modes do not confer authorization.

Preview and create receive the same version-1 `--component-selection` JSON:

```json
{"format_version":1,"components":{"rcc_environment":"refresh","homebrew_recovery":"keep","hauler_content":"keep"}}
```

Unknown slots, modes and versions fail closed. Explicit selections bypass local
workspace-only no-op caches; the backend resolves component identities before
comparing against the previous descriptor. Preview reports pending/reused/absent/
excluded, and the receipt reports actual descriptor references. `captured-or-reused`
means the resolver verified the selected material but does not distinguish a
content-addressed reuse from a new component backup. Estimated workspace bytes
are not total component bytes or a promised upload size. Preview does not run
installers or RCC capture. Selection is not authorization: existing host policy,
Dimension encryption, workspace validation and provider checks still apply.

Homebrew has three distinct meanings:

- **Brewfile-only recipe directory:** choose a folder containing exactly one
  regular, non-symlink, non-hardlinked Brewfile, at most 1 MiB. The controller
  packages these inert bytes privately, then uses existing JAT validation and
  the `homebrew_recovery` lifecycle. Extra files are rejected; use a prepared
  recovery archive for a fuller existing JAT export. This never runs Ruby,
  `brew bundle`, installers, or restore scripts. Native reinstallation is a
  separate user-approved operation; versions and cold-host reinstallation have
  not been verified by this checkpoint.
- **Prepared recovery archive:** existing validated `--brew-archive` semantics,
  independently of a Brewfile recipe. It does not establish installed binaries.
- **Installed prefix/binaries:** unavailable. Prefix, Cellar, repository and cache
  are distinct; a raw Cellar transplant does not establish relocatability, ABI,
  dependency/link completeness, collision safety or provenance. Fresh same-OS,
  architecture and exact-prefix execution, hardlink/reparse containment and
  no-clobber restoration remain required before enabling capture. No arbitrary
  prefix or cache is traversed by this feature.

OCI refresh supports all tagged images, explicit image references, an explicit
Hauler manifest or an explicitly selected additional file. RCC artifacts remain platform bound. Agent state is unavailable in this
main-based controller. Draft #210's Codex capability is history-only with missing
Dimension admission; T3 and Claude capture are blocked. No auth state is copied,
no second agent picker is added, and no unqualified provider becomes selectable.
Full #188 capability/admission integration remains a separate acceptance gate.

Preview tokens bind the Room, Dimension, source path, prior logical snapshot,
capture policy, selection and explicit external input bytes. Save checks a
supplied token before preparation, after component resolution, and before
publication. OCI tags are resolved during Save rather than pinned by preview.
Installed VSIX interaction and Windows/macOS remain unqualified. Those limits
must remain visible in PR evidence; it is not completion of issue #190 or release
qualification. Logical JAT v1 gains no new slots; existing copy/export/verify/
remove reachability continues to operate on the three validated component types.
