# Room Store native RCC environment component

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
The RCC version is pinned to `v18.19.5`; absence, version mismatch, incomplete
native metadata, or an invalid export stops Save before publishing the logical
JAT. RCC publish, export, and archive verification complete before upgrading
the Room Store keyset or opening Restic. Failed RCC preparation therefore
performs no provider writes, including transient Restic lock writes, on fresh
or existing stores. Component restore remains separate from workspace restore.

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
