# Device authority

Device enrollment stores only public metadata in the private configuration
directory. R2 credentials, age identities, and the enrollment receipt are
stored in the native OS secret authority. The command accepts secret-bearing
input on stdin; secrets are never command-line arguments, environment values,
logs, repository files, or container-layer fallbacks.

## Supported platform matrix

| Platform | Allowlisted authority | Availability requirement | Typical doctor diagnostics |
| --- | --- | --- | --- |
| Linux desktop/SSH session | Secret Service (GNOME Keyring or KWallet-compatible provider) | `secret-tool` and a reachable D-Bus session | `session-bus-missing`, `locked`, `dbus-unavailable` |
| macOS | Login Keychain via Keychain Services | `security`/Keychain session is reachable and unlocked | `locked`, `probe-failed` |
| Windows | Windows Credential Manager/Credential Locker API | Native credential API is available | `api-unavailable`, `locked` |
| WSL | Host Secret Service only when an explicit session bus is available | WSL bridge/session bus must be configured | `wsl-session-bus-missing` |
| Container | Host-provided Secret Service only; no container-layer store | Explicit D-Bus session and provider | `container-session-bus-missing` |
| Headless SSH | Forwarded/host Secret Service session | Explicit D-Bus session is required | `headless-ssh-session-bus-missing` |

Null, plaintext, file, environment, fail-open, degenerate, and unknown
backends are rejected. A private runtime file remains supported only for the
existing v0.1 ephemeral runtime path; it is never accepted as the device
secret authority.

## Commands

- `josh-room device inspect --json` reports stable, secret-free metadata.
- `josh-room device setup --json` and `device enroll --json` read a JSON
  enrollment document from stdin.
- `josh-room device rotate-new-writes --profile PROFILE --json` reads the new
  public recipient set from stdin. New writes use the new version; historical
  versions remain available for decrypting old ciphertext.
- `josh-room device remove-local --json` removes local state and native-store
  entries. It does not revoke already published ciphertext.
- `josh-room device doctor --json` diagnoses authority, receipt, state, and
  clock problems. Device unavailability blocks prepare/upload, but does not
  block hook enqueue.

Fresh-device transfer is out-of-band. An operator transfers the approved age
identity, recovery identity (if applicable), and current recipient set through
a secure channel, then supplies them on stdin on the fresh device. Do not copy
`device.json` as proof of enrollment: the private receipt is intentionally not
in that file, so a copied config is diagnosed as unavailable.

## Production versus local-only preparation

Private-R2 evidence preparation and remote publication require a ready device
authority. The PCC contract gates preparation by default (or an explicitly
selected production authority check), and the R2 evidence publication boundary
enforces device readiness before remote upload. Local-only contract fixtures
explicitly disable the PCC check and use a local destination; they do not
exercise private-R2 publication and must not be treated as production
acceptance.
