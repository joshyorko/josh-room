# Josh Room agent boundaries

Josh Room is a public-first, one-owner CLI for encrypted workspace snapshots.

- Room of Requirement owns the base image and template.
- Josh All The Things owns capture/restore and Hauler details.
- age owns encryption; Cloudflare R2 is the private production blob backend.
- RCC owns runtime environments; Actions owns runtime execution; Hive owns
  agents, models, tools, and task orchestration.
- Do not add web UI, extension, daemon, watcher, provider framework, multi-user
  auth, sync, DevPod/Devsy, or snapshot garbage collection.

Never commit real identities, credentials, catalogs, snapshots, private paths,
employer/customer data, or private repository names. Use synthetic fixtures.

Never add an interactive Cloudflare login to normal operations. Private R2
configuration lives under `$XDG_CONFIG_HOME/josh-room/` and contains only
non-secret settings plus an opaque keyring profile. Persistent sensitive auth
material belongs in the host OS Secret Service/keyring, not plaintext files or
the container filesystem. Prefer a dedicated bucket-scoped token; do not make
Josh Room depend on private Fizzy/Kamal configuration at runtime.

Verify with the focused Python tests, the real JAT/age local vertical when those
tools are installed, JSON/shell checks, secret scanning, and `git diff --check`.
Report unavailable tools and secret-gated R2 tests as skipped, never passed.
