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
JAT. Component restore remains separate from workspace restore.

Homebrew recovery and Hauler content remain separate component owners and are
not inferred from an RCC capture.
