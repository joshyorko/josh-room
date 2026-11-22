# Disposable native recovery acceptance

This Linux-only recipe uses a real loopback MinIO server, an isolated native
Secret Service, the installed VSIX, and the registered
`joshRoom.browseRecoveryPoint` command. It never uses the host keyring or real
provider credentials. It deliberately overrides runtime selection to an
existing Python environment plus the installed controller source in existing
`local-build-fallback` mode. **It does not qualify an immutable RCC artifact.**

Run host commands on the Linux workstation, not in the Secret Service
container. Use existing project Python dependencies, Docker, age/age-keygen,
VS Code Insiders, and Xvfb. Check disk space first. Keep all generated state in
one `mktemp -d` task directory and delete it after recording sanitized results.
Use distinct ports and a distinct container name per parallel implementer.

## Prepare the isolated native keyring

The tested container Dockerfile is:

```dockerfile
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends gnome-keyring dbus libsecret-tools ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd -u 1000 -m fixture
USER 1000:1000
ENV HOME=/home/fixture
```

The observed Debian image content ID was
`sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc`;
the built task image manifest digest was
`sha256:2f2d1590f27201bd1a4f6bce5bef8903eac7540939a31bb17f9d7c05de0ad5f3`.
These identify this run, not a promise that future distro packages are identical.
Record the image IDs from your own build. This workstation's UID/GID were
1000; adjust the container user to match the workstation user when necessary.
Do not install packages on the host.

In a script under the task directory, start the service as follows (the task
root must be mounted at the **same absolute path** inside the container):

```sh
#!/bin/sh
set -eu
export XDG_RUNTIME_DIR="$JOSH_ROOM_ACCEPTANCE_ROOT/keyring-runtime"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork
printf 'synthetic-disposable-unlock\n' | gnome-keyring-daemon --unlock --components=secrets --control-directory="$XDG_RUNTIME_DIR/control"
exec tail -f /dev/null
```

Create the task root and `keyring-runtime` with mode 0700. Run the task-built
image with `--rm`, a unique `--name`, `--network=none`, `--cap-drop=ALL`,
`--security-opt=no-new-privileges`, and an explicit bind mount of only the task
root. Pass the nonsecret `JOSH_ROOM_ACCEPTANCE_ROOT` environment variable.
Set the same private `DBUS_SESSION_BUS_ADDRESS` on the host test processes.
Never mount the host home, real D-Bus socket, or real keyring directories.
The fixed unlock input is a synthetic test value, never a real password.

Before enrollment, run the production `josh_room.keyring.backend_status()`
probe through the chosen Python environment and verify `available: true` and
`locked: false`. A normal isolated `dbus-run-session` without this service
failed with `org.freedesktop.secrets was not provided by any .service files`.

## Enroll a synthetic provider and keep it alive

Download MinIO's official
`RELEASE.2025-09-07T16-13-09Z/minio.linux-amd64.RELEASE.2025-09-07T16-13-09Z`
asset from the `minio/minio` GitHub release into `TASK_ROOT/minio`. Verify
SHA-256 `7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f`
and make the file executable. This is the same pin recorded in
[the earlier provider acceptance](receipts/issue-165/real-minio-controller-receipt.json).

Run [`scripts/synthetic_recovery_provider.py`](../scripts/synthetic_recovery_provider.py)
with `JOSH_ROOM_ACCEPTANCE_ROOT`, optional `JOSH_ROOM_ACCEPTANCE_PORT` (default
19400; console uses the next port), and `PYTHONPATH` pointing at the controller
being tested. The script requires a fresh task fixture, verifies MinIO's hash,
generates credentials in memory, stores them only via the disposable native
keyring, writes only nonsecret Dimension configuration, creates the bucket,
initializes encryption with a transient private recovery handoff, and creates
three snapshots of synthetic files through the normal CLI. The transient
handoff is removed after initialization. Recovery identities, provider keys
and keyring contents must never be committed or included in receipts.

The script prints a sanitized readiness receipt and then waits for the MinIO
child. **Keep that execution session active during UI testing.** An initial
setup-only launcher exited and its process cleanup killed MinIO, yielding an
empty hierarchy; this was an acceptance harness lifetime failure, not a pass.
The server listens only on `127.0.0.1`, and the container has no network.
Stop the owned server session after testing; do not restart somebody else's
instance or use another lane's keyring/data.

## Exercise the installed registered command

Package the VSIX with `npx @vscode/vsce package --no-dependencies`, install it
using disposable `--user-data-dir` and `--extensions-dir`, and verify its
controller/helper source bytes against the source revision. Installation adds
`__metadata` to package JSON; compare that manifest semantically excluding
this installation field. Do not overwrite the user's installed extension.

Create a tiny development-driver extension in the task directory, with
`main` pointing at a copy of
[`scripts/recovery_acceptance_driver.js`](../scripts/recovery_acceptance_driver.js),
`activationEvents: ["onStartupFinished"]`, and a unique synthetic package ID.
Pass these nonsecret environment variables to the isolated VS Code process:

- `JOSH_ROOM_ACCEPTANCE_ROOT`: task root.
- `JOSH_ROOM_ACCEPTANCE_PYTHON`: existing Python executable with project dependencies.
- `JOSH_ROOM_CONFIG_DIR`: task root's `provider-config`.
- `XDG_STATE_HOME`, `XDG_CACHE_HOME`, `XDG_RUNTIME_DIR`: task root's
  `provider-state`, `provider-cache`, `provider-runtime`.
- `DBUS_SESSION_BUS_ADDRESS`: task root's private keyring socket.

Use `--extensionDevelopmentPath` for the driver, open only `provider-workspace`,
and use an isolated Xvfb display (`-nolisten tcp`). The tested profile enabled
`files.simpleDialog.enable` and disabled Workspace Trust prompts for this
synthetic workspace only. It also disabled telemetry and automatic updates.
The driver uses the installed extension's existing runtime test hook; it does
not inject catalog/provider/keyring results. It executes the **registered**
command with the oldest real snapshot ID from `provider-pins.json`. The
controller still performs normal ciphertext/catalog/Dimension/keyset checks.
Keep the override explicit in every evidence report.

Observe the native pinned tree, select `changed.txt`, choose current comparison,
and inspect the modified digest result. Then recover the selection through
the native Save Dialog to a new absent path outside the Room. Confirm the
historical bytes are `first\n`, every workspace hash still matches
`provider-before.json`, `.josh-room.json` is unchanged, the selected snapshot ID
matches the first pin, and no `.room-recovery-*` staging remains. Cancel the
next picker and verify no additional extraction. Record native screens and
sanitized receipts; do not publish raw keyring/config-runtime files or logs.

## Native Windows installed qualification

The ordinary `CI / windows-installed-recovery` job builds a separate candidate
VSIX, installs it in a fresh profile, verifies every installed package byte
(excluding installation metadata), and runs
`scripts/windows_recovery_installed.js`. This is independent of release,
staging and publication workflows. It does not change the product runtime.
Installation invokes the archive's official `bin/code.cmd` wrapper: recent VS
Code archives place app resources under a revision directory. The harness must
not assume the older `resources/app/out/cli.js` location.

Dependencies are exact official Windows MinIO, age and VS Code archives with
SHA-256 pins in `scripts/windows_recovery_dependencies.json`. Each acquisition
has a two-minute deadline and no download retry loop. Restic uses the installed
controller's existing verified installer and manifest. The fixture creates an
owned loopback MinIO server, a synthetic Dimension and keyset, and three actual
snapshots through the installed controller CLI. No storage emulator is used.

On the ephemeral Windows runner, generated provider credentials, operational
identity and Room Store secret use production Windows Credential Manager APIs.
The fixture predeclares unique target names, checks they do not exist, verifies
roundtrips, and deletes only those targets in `finally`. It never enumerates or
changes unrelated credentials. Task directories have the production private ACL
applied. Provider keys remain in memory/native custody; the generated recovery
identity is discarded after deriving its public recipient.

Controller-generated operational identity files now receive the existing native
private-file ACL before any identity bytes are written on Windows. Encryption
readiness verifies that native ACL instead of applying POSIX permission bits to
Windows `stat` emulation. Unverifiable, inherited or broad ACLs fail closed;
POSIX admission is unchanged. The ordinary Windows recovery tests include a
real native ACL readiness/inheritance-denial check, independently of the
installed UI/provider job. Existing explicit material handoffs keep their
current admission; this does not relax legacy handoff checks.

A small development driver selects the installed controller source using the
existing `local-build-fallback` test hook after activation, matching the loaded
entry module by canonical path (case-insensitively on Windows). It fails closed
if the activated module hook cannot be found, rather than silently starting an
unintended artifact import. **This explicitly does not qualify an
immutable RCC feature artifact.** It registers harness entry points which call
the actual `joshRoom.browseRecoveryPoint`; it does not replace pickers, provider
calls, CLI results or recovery callbacks. Loopback Electron CDP sends real key
input to native QuickPick, command palette and the simple Save Dialog.

The intended acceptance covers the pinned tree, current comparison in an
actual native readonly diff tab, exact tab/Unicode text preservation, rejected
editor writes, one-file extraction to an absent destination and picker
cancellation. Separate actual CLI overwrite denial verifies existing target
bytes. A complete before/after workspace inventory includes the Room marker
and unrelated synthetic files. Only sanitized JSON evidence is uploaded; raw
profile, runtime, server data, logs and credentials are removed. Missing
dependencies or native UI fail closed and leave a failure receipt, never a
fixture substitute or a passing Windows UI claim.
Windows `LOCALAPPDATA` and `APPDATA`, as well as the explicit controller paths,
point inside the owned task root. Partial failure receipts separately report
completed public CLI stages and native credential cleanup. CLI failures retain
only their error code and a bounded shared-policy diagnostic with generated
keys and Windows paths removed; raw controller output is never uploaded.

To reproduce on a disposable native Windows runner with existing project
dependencies, set `JOSH_ROOM_ACCEPTANCE_PYTHON` to its Python executable and
`JOSH_ROOM_WINDOWS_RECOVERY_EVIDENCE` to an absent/owned evidence directory,
then run `node scripts/windows_recovery_installed.js`. Never use a production
profile or a shared Windows vault. Linux execution intentionally refuses this
qualification rather than simulating Windows custody or UI.

The [native Windows provider/CLI receipt](receipts/issue-209-windows-provider-cli.json)
records partial qualification at source `32048dd`: installed package parity,
real provider/native custody, three production snapshots, exact historical
inspection, overwrite refusal, workspace/marker preservation and cleanup
passed. The separate native ACL test job also passed. The installed UI trial
failed at picker navigation after renderer/driver activation; Browse, native
diff and selective recovery UI acceptance remain **unproven**. Existing Windows
recovery tests do not substitute for that UI chain. Other providers and
immutable runtime promotion remain separate gates.

## Cleanup and remaining gates

Close only the isolated VS Code process, stop the observed MinIO session and
the named task Secret Service container, remove only the image/build cache
created for this task, then remove the task directory. Preserve shared base
images while another lane depends on them. Never use broad Docker pruning on
shared workstations.

A passing trial proves the registered native action, real provider/age/Restic
and native keyring chain with a **source-Python runtime override**. It does not
prove immutable RCC packaging, Windows, wire transfer counts, text-preview
policy, syntax diff editors, or production acceptance. Those remain separate
qualification gates; v1.0.0 stays on owner hold.
