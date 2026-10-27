# ADR 0010 — OMP extension surface

Oh My Pi (OMP) is the agent harness. Josh Room contributes an OMP plugin package
(`omp-extension/`) to it; Josh Room core does not grow an agent loop. This
narrows the "no agent harness" exclusion in ADR 0001 and ADR 0007 for OMP only.

The plugin is a thin projection over the versioned Josh Room JSON CLI. Startup
checks only whether `.josh-room.json` exists and then calls the local, auth-free
`josh-room context --workspace <cwd> --json`; outside a Room it does nothing.
Status, snapshot and Room browsing, Save, JAT inspection, and doctor run only on
an explicit command or tool call. Unknown controller schemas fail closed.

Model tools are bounded to `room_context`, `room_status`, `room_snapshots`,
`room_save`, and `jat_inspect`. Read tools use OMP's `read` approval tier and
Save uses `write`. There is no restore, delete, provider, encryption, or shell
passthrough tool. Session checkpoint entries
(`com.joshyorko.josh-room.checkpoint.v1`) hold opaque identifiers only and are
never recovery or mutation authority.

The VS Code extension remains the first-class graphical surface. Neither
surface duplicates provider, encryption, marker, storage, RCC, or JAT logic.
