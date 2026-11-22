# Synthetic Linux composition acceptance

[Sanitized receipt](native-composition-linux.json) records installed source
revision, VSIX digest, tools, exact synthetic pins, observed results and limits.
The installed VSIX source matched `e466ca310d96c2bfda0c8bb73ab042c1ef8da114`.
It used an explicit existing-Python/controller-source runtime override in
`local-build-fallback` mode. **This does not qualify immutable RCC artifacts,
Windows, remote R2/S3, or Josh Room v1.0.0. Release remains on owner HOLD.**

The registered `joshRoom.saveCatalog` action was driven through actual native
source, child, parent-content and Dimension dialogs. Its real OS-native
[approval](native-catalog-review.png) explicitly disclosed source-only capture
and local-only/unprotected content. Its installed
[success notification](native-catalog-saved.png) named the namespace and parent
pin; the full pin was also verified in the actual renderer text.

The parent was then restored through the installed controller CLI into a new
absent destination. The registered `joshRoom.enterChild` action accepted that
parent's exact pin and hydrated only `projects/a`, displayed the exact child
pin, and opened its folder. Reopening the parent and repeating the registered
action reopened the same clean child at the same pin. The fixture driver
asserted original source hashes and legacy snapshot inventory after both entries;
a separate check compared the complete original file set and all four byte
hashes. No provider responses, catalog data or encryption decisions were mocked
in this installed trial. Attempted scripted picker overrides had no effect:
the actual UI was operated on a private Xvfb display with CDP/X11 input.

This reused the isolated native keyring/installed-driver pattern from
[#209's acceptance recipe at `2398f0d`](https://github.com/joshyorko/josh-room/blob/2398f0d0d769f2c9f8d559529849e587fa797aa1/docs/recovery-acceptance.md).
Only a new task-owned loopback MinIO, native Secret Service, VS Code profile,
extension directory and display were used. No existing credentials, services,
snapshots or workspaces were used. Raw keyring/config/runtime files are not
retained here. A harness lifetime error initially stopped Xvfb during folder
switch; the trial resumed from the already saved parent without repeating Save.

## Reproduce the scoped checks

Run in the project Python environment with the repository-pinned Restic 0.19.1,
age and age-keygen. Set `JOSH_ROOM_TEST_RESTIC` to that verified binary when the
host default differs. Tests use synthetic fixtures only:

```sh
python -m pytest tests/test_composition_live_tools.py -q
python -m pytest tests/test_composition_staging.py tests/test_composition_transaction.py tests/test_composition_safety.py tests/test_composition.py tests/test_restic_store.py -q
node --test vscode-extension/catalog-actions.test.js vscode-extension/inventory-picker.test.js
```

The real-tool suite uses durable SQLite **only as the S3 object transport**;
Restic backup/restore and age encryption/decryption are real. It separately
tests bootstrap, child-snapshot, parent-snapshot and parent-commit process loss,
idempotent retry, nested child entry, inherited policies, restrictive directory
modes, namespace guards and isolation from legacy root deletion/real Optimize.
That synthetic transport is not live-provider evidence.

For actual loopback CLI qualification, supply a separately provisioned native
Secret Service socket inside the dedicated task scratch and the exact pinned
MinIO binary, then run:

```sh
python scripts/composition_minio_acceptance.py --scratch /synthetic/task \
  --minio /synthetic/task/minio --secret-service-bus /synthetic/task/keyring-runtime/bus
```

The script generates credentials in memory, stores them in that native keyring,
uses a fresh private fixture directory and dynamic loopback ports, and stops its
owned server on exit. It refuses a socket outside the task scratch and a MinIO
binary whose digest differs from the checked pin. Never point it at the host
keyring or existing provider data. Clean up the owned fixture/keyring container
and task directory after retaining sanitized evidence.

The final producer-metadata correction is separately verified by a real
Save/Enter test that checks the descriptor's actual extension version. The
installed UI receipt remains bound to its named source revision and VSIX digest.
Independent Astra review findings were corrected with regression tests; the
final corrections have not received an independent approval.

The prior CI revision failed its exact command-inventory assertion because it
omitted the two new commands. The inventory is corrected and its named regression
passes. Broad local attempts also exposed read-only default host state and an
incompatible host Restic 0.20.0; qualification uses disposable XDG state and the
verified 0.19.1 binary instead. The receipt retains these failed attempts.
Secretlint scanned 43 changed textual files and reported one pre-existing
synthetic embedded-credential rejection fixture in `tests/test_restic_store.py:885`;
it reported no real credentials. Packaging also runs its bundled secret scanner.
