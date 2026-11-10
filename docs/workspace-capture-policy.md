# Workspace capture policy v1

Josh Room uses the versioned defaults in `src/josh_room/workspace_capture_defaults.json` for workspace fingerprints, dirty tracking, and capture exclusions. The VS Code extension reads the packaged copy at `runtime/controller/josh_room/workspace_capture_defaults.json`; the package copy is generated from the canonical source file. Both files must have identical contents.

The defaults exclude these names at every directory depth: `.josh-room.json`, `.DS_Store`, `.pytest_cache`, `.ruff_cache`, `.venv`, `venv`, `node_modules`, `__pycache__`, `.robocorp`, and `.rcc_home`. In the default full-repository mode, Git metadata is recovery content: included `.git` directories and files, objects, refs, indexes, hooks, config, reflogs and stashes are preserved. Exclusions cannot prune `.git` or its descendants, even when a ref is named `venv` or a workspace ignore rule names `.git`. An excluded ancestor, such as `node_modules`, still excludes its whole subtree. A caller may also pass the resolved active runtime root explicitly. When it is inside the workspace, that directory and its contents are excluded unless they are Git metadata; its relative path contributes to the policy SHA256. A runtime root outside the workspace adds no exclusion. The workspace root itself cannot be selected as the active runtime root.

An optional UTF-8 `.josh-roomignore` file adds exclusions. Each nonblank line is one POSIX-style pattern. A pattern with `/` is rooted at the workspace root; a single-component pattern such as `*.tmp` matches that basename at any depth. Lines starting with `#` are comments except the explicit capture-mode directive documented below. `*` and `?` match within one path component; a component equal to `**` matches zero or more components. Adjacent `**` components are collapsed. A matching directory excludes its descendants. Rules are limited to 128 non-comment lines, 1,024 characters, and 128 path components. Bracket expressions, absolute paths, `.`, `..`, backslashes, and `!` negation are rejected. Git ignore files and Git ignore behavior are not read. The ignore file must be a regular, non-symlink file no larger than 64 KiB; invalid rules fail policy loading rather than silently changing capture.

The ignore file itself is not excluded by default. Its exact UTF-8 contents, including comments and line endings, contribute to `capture_policy_sha256`, so any edit changes the policy identity. The shared defaults bytes and the explicit active runtime path also contribute. A changed policy therefore changes the workspace fingerprint when the caller binds the policy hash into that fingerprint or snapshot metadata.

Python callers use `load_capture_policy(workspace_root, active_runtime_root=...)`; JavaScript callers use `loadCapturePolicy(workspaceRoot, { activeRuntimeRoot })` or pass a compiled result from `compileCapturePolicy` to `shouldMarkDirty`. `CapturePolicy.is_excluded` / `policy.isExcluded` expose the same compiled inclusion decision. The raw `restic_excludes()` / `policy.resticExcludes()` rules describe generated exclusions but must not be passed directly to backup: Save uses `resolved_restic_excludes(root)` to emit exact, escaped absolute paths so broad cache globs cannot discard Git internals or matching sibling paths.

Save and staged Restore validate Git storage without running Git commands or hooks. Ordinary nested repositories and relative in-workspace submodule gitfiles are supported. External or absolute pointers, linked worktrees with absolute administrative back-pointers, borrowed object databases (alternates), config includes and custom hooks/attributes/excludes paths fail closed with a diagnostic. No pointer, remote, history or source file is rewritten. Pointer-backed storage must be entirely included. Bare repositories are also validated and fail if any storage is excluded.
When `workspace-check` finds a live linked-worktree record, its diagnostics identify the work directory, report whether Git sees a clean tree with no commits ahead of its configured upstream, whether uncommitted, ignored, or unpushed work is present, or whether safety could not be determined, and recommend `git worktree remove <workdir>`. This is read-only guidance: Josh Room never removes a worktree or its metadata. Treat “safe to remove” as a status-time assessment; preserve any work you need before removing it. If repository-local Git filters are configured, safety is reported as unknown rather than running them. Worktrees containing submodules also report unknown without recursively inspecting the submodules or running their filters.


VS Code may suppress `.git` watcher events. Watcher events only mark changes; cached no-op Save requires a fresh native metadata signature. This adds an O(included entries) stat scan, not a content hash or full JAT rebuild. Git config/pointer validation reads only bounded metadata. Restic encryption, deduplication and parent reuse remain in place.

Snapshots created with the old `.git` exclusion remain readable, but cannot restore Git metadata that was never captured. The changed defaults invalidate old policy/no-op identities; a new Save from an intact original workspace is required for complete Git recovery. Do not overwrite a damaged workspace, reinitialize repositories, or infer missing local history from remotes.

`CACHEDIR.TAG` is not interpreted. A tag must not silently hide its containing directory.

## Explicit source-only capture

Full-repository capture remains the default, including for older descriptors
without a `capture_mode` field. To choose source-only capture, record exactly one
of these directives in the selected folder's `.josh-roomignore`:

```text
# josh-room capture-mode: source-only
```

This choice excludes complete `.git` administrative entries at every depth,
including linked-worktree pointer files. Selected project files are saved;
Git history, branches, staging state, and explicitly excluded worktree files are
not backed up. A remote push does not establish recovery of that omitted data.
Working directories remain included unless separately selected for exclusion.
No original repository, administrative registration, or source file is changed.
Unknown or duplicate mode directives fail policy loading. Use
`# josh-room capture-mode: full-repository` (or remove the directive) to restore
the full-repository contract; an ordinary `.git` ignore pattern cannot do so.

Mode changes alter the policy digest and invalidate no-op and deletion-preview
confirmation state. New descriptors record `capture_mode`; old descriptors keep
full-repository semantics. Restore still requires an absent destination and
checks the complete staged payload, rejecting Git metadata in a source-only
snapshot, refusing retained bare Git stores, and retaining filesystem link, special-file, and portable-name checks.
Portable source-only exports use the filtered JAT capability and fail if that
capability is unavailable. Source-only is not relocatable Git-worktree recovery.

## Initialize Room onboarding, inspect and approve exclusions

New Room creation initializes the selected source folder (which can differ from
VS Code's workspace) after its name and destination have been confirmed. It
creates a missing root `AGENTS.md` and, in the default `vscode` editor mode,
creates selected missing `.vscode` files before preview. Discovery is bounded
and reads repository manifests and documentation without running project
commands, tasks, hooks, or installers. The guide links its evidence, preserves
nested guide scopes, and records unknowns. Unsupported or conflicting editor
files are omitted with a reason. Existing files, including JSONC, are never
rewritten. Editor recommendations do not install extensions or run tasks.

Documentation excerpts retain their heading ancestry, including **Current**
and **Proposed**, so a future design is not presented as implemented behavior.
Discovery follows up to eight local Markdown documents per component, starting
from the recognized README and architecture files. Links outside the selected
source, external URLs, and symlinked documents are not read. Git repository
boundaries remain in the guide even when their manifests are unsupported; their
purpose and commands are reported as unknown.

Package-script tasks use the declared `packageManager` (`npm`, `pnpm`, `yarn`,
or `bun`), inherited from enclosing manifests without crossing an independent
repository boundary. Without a declaration, they retain the npm default.
Documented shell blocks can establish a working directory with a standalone
`cd` into an existing directory inside the selected source. Unsupported or
external directory transitions stop command extraction from that block rather
than assigning commands to the wrong directory. Discovery never runs a task
to establish whether its command succeeds.

The `joshRoom.onboarding.editor` VS Code setting selects `vscode` or `none`;
`joshRoom.onboarding.files` selects any subset of `tasks.json`, `launch.json`,
`extensions.json`, and `settings.json` (an empty list disables editor files).
The missing `AGENTS.md` is still generated in `none` mode. These preferences
are VS Code settings and are not stored in `.josh-room.json`.

Initialization also creates the comments-first `.josh-roomignore` starter,
which adds no active exclusions. Existing files retain their bytes and
timestamps; invalid, symlink and nonregular policy files fail safely. The full
initialization command is available explicitly:

```sh
josh-room workspace init --workspace ./selected-project --json
josh-room workspace init --workspace ./selected-project --editor none --json
josh-room workspace init --workspace ./selected-project --editor-file tasks.json --editor-file extensions.json --json
josh-room workspace init --workspace ./selected-project --no-editor-files --json
josh-room workspace-check --workspace ./selected-project --json
```

The JSON result reports onboarding artifact statuses and omissions. `--editor
none` and `--no-editor-files` both retain missing-guide generation; the latter
selects an empty editor-file set. Repeated initialization preserves existing
file bytes and timestamps. Ordinary Enter, status, hydration, and background
activation do not initialize onboarding. Exclusion editing uses policy-only
initialization and never generates a guide or editor files.

Status and scanning never initialize or edit the file. **Edit exclusions** opens
the selected folder's file; save those edits before choosing **Continue**. The
new-Room prompt also offers **Use source-only** with a separate consequence
confirmation, including for healthy repositories. Canceling before initialization
creates nothing; canceling later leaves the initialized file and approved edits.

Before preview, Save runs a bounded diagnostic pass using the Save validators.
The report includes ordered findings with stable codes and relative paths,
collision pairs, a `complete` coverage flag, and explicit truncation/cancellation
state. Default limits are 100,000 directory entries, 200 findings and 15 seconds.
Unreadable subtrees and filesystem boundaries leave coverage incomplete. A
blocked or incomplete check exits nonzero and never authorizes Save; Save still
performs its own current-state checks.

Native Quick Pick offers worktree directories with nothing selected. A second
confirmation lists the exact proposed exclusions and backup consequence. Only
approved directories are appended, with `/**` to keep single directory names
rooted. Unsupported literal glob characters are refused. A policy edit digest
rejects stale reviews; appending preserves existing bytes and detects changes
before writing. Edits made by unrelated programs cannot be locked out, so
concurrent editing should be stopped before applying an exclusion. Review and
rescan after any reported concurrent-change error.

Every approved edit invalidates the cached Save receipt and triggers a fresh
scan before preview. Ignoring a checkout alone does not resolve the main
repository's administrative pointers. Choose source-only explicitly or stop and
inspect the remaining report; Save does not prune or repair Git records.
Diagnostic paths are intended for local review. Do not publish the report or
Output log without removing private filenames and paths.
