# Native component selection checkpoint evidence

Implementation base: `23c80716b8a646cbf74f6eacb04eb639af87c9c2`.
First pushed checkpoint: `15cbd0ee1d89fc31a5c35f4e6c44c772255951c7`.
This receipt records Linux x64 evidence, not release qualification or issue closure.

| Check | Observed result | Scope |
| --- | --- | --- |
| `python -m pytest -q tests/test_component_selection.py tests/test_room_store_homebrew.py tests/test_room_store_components.py` | PASS 41 | Selection, inert recipe, existing component capture fixtures |
| Pinned Restic 0.19.1 bridge + component suite | PASS 85, SKIP 2 | Real local Restic + age, synthetic fake S3 authority; not live provider |
| Affected selection/token/explicit-save tests after review | PASS 13 | Prior-snapshot/token and fd-relative recipe review fixes |
| Security/lifecycle/references/native CLI/controller suite | PASS 165 | Existing strict references, material security and mirrored controller contracts |
| `node --test vscode-extension/component-picker.test.js vscode-extension/extension.test.js` | PASS 262, SKIP 2 | Shared chooser, explicit image/file details, durable non-secret modes, cancellation and Save regressions |
| Post-checkpoint Python selection/native CLI/public safety/controller suite | PASS 59 | Follow-up package and controller contracts |
| `python -m josh_room snapshot preview --help` | PASS | Actual checked-out CLI import/parser, not Save |
| Scoped Ruff and `git diff --check` | PASS | Changed Python and whitespace |
| `npx --yes @vscode/vsce@4.0.0 package --no-dependencies` | PASS | Local test VSIX only; no publication |
| `code --user-data-dir <disposable-profile> --extensions-dir <disposable-extensions> --install-extension <test.vsix> --force` | PASS | Installed isolated profile; user profile untouched |
| Installed VS Code 1.147.0 activation + actual native QuickPick | PASS | Registered New/Save/SaveFolderAs; rendered mandatory workspace and unavailable rows; accepted default keep plan; deselected all optional rows with mandatory workspace intact; cancellation |

The installed native test uses Xvfb, a disposable synthetic workspace and the
real VS Code API. `installed-picker-driver.cjs` is the test runner; set
`ROOM190_ACCEPTANCE_ROOT` to the disposable task root. Launch Code with
`--wait --no-sandbox --disable-gpu --disable-workspace-trust`, isolated
`--user-data-dir` / `--extensions-dir`, `--extensionTestsPath` pointing to that
runner, a minimal separate test-driver extension as `--extensionDevelopmentPath`,
and loopback `--remote-debugging-port=19390`. The screenshot is of the actual
installed native UI. This exercises the installed picker module and activation;
it does **not** invoke a complete Save through the registered commands.
`installed-picker.json` records that limit explicitly. That initial picker trial did not perform Save. The later registered Save trial
below uses isolated real provider/Secret Service; managed RCC artifact execution
is still separate. Never use the real host keyring.

The native run exposed that trailing separator-only unavailable rows disappear
in VS Code. They are now visible explicitly unavailable entries; selecting one
cannot authorize capture and returns to the chooser with a refusal. There is
no second harness-session picker.

## Homebrew footprint feasibility

Read-only ELF inspection of an installed public jq 1.8.2 binary found:

- Interpreter at the exact default Linuxbrew prefix's `lib/ld.so`.
- Runtime dependencies on libjq, libonig and libc.
- Absolute RPATH references to its own keg, opt/gcc, opt/oniguruma and prefix/lib.
- Binary SHA256 `e38b63aefcf56199bde16a4d5059aaa39397de67f20ac483c9eb5708fa8be32e`.

A read-only bind of only that binary into its identical prefix in a disposable
clean Linux x64 container failed to execute with exit 255 (missing interpreter).
The container had no network, no capabilities, a read-only root, and public base
`debian@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc`.
No host prefix was changed or archived. This proves a binary-only transfer is
insufficient; it says nothing about a correctly closed whole-prefix restore.
A separate fresh disposable-prefix declared-runtime-closure trial is in progress.
Installed-prefix selection remains unavailable pending that trial and broader
provenance, containment and no-clobber qualification. Recipe recovery is separate.

## Failures and limits retained

- Initial Python invocation failed: system Python lacked pytest. Reused existing
  project development runtime rather than creating another environment.
- Host Restic 0.20.0 caused seven bridge failures. The checked-in installer
  acquired and verified 0.19.1; pinned rerun passed.
- Old OCI-only extension mocks caused 33 failures. Migrated response fixtures
  and obsolete local-cache expectations; affected full Node rerun passed.
- Initial native launch returned before window startup; `--wait` kept Xvfb alive.
  Disabling the installed extension hid it from the API; corrected runner enabled
  it. Neither failed attempt is counted as installed acceptance.
- Initial installed UI test failed because separator rows were hidden; fixed
  and verified through actual rendering.
- Offline packaging could not find a cached vsce; used the repo-pinned package
  command. No host OS package or user extension profile was modified.

SKIP / NOT RUN: full installed Save, Windows x64/macOS arm64, live provider
acceptance, managed JAT Homebrew archive roundtrip, native package reinstall,
full cold-host binary restore, #188 Dimension-authorized capture and native new
turns. These are open gates, not passing tests. Strong immutable input staging
against adversarial change-and-revert is not established by repeated token checks.
No release, merge, tag, release staging/publication or hosted model call occurred.

## Broader follow-up checks

Full extension suite: `node --test vscode-extension/*.test.js`, PASS 379,
SKIP 2. Repository-wide `ruff check src tests` PASS after fixing two unused
new test variables identified by CI at a3f65f1.

Full Python suite with `JOSH_ROOM_TEST_RESTIC` set: FAIL 4, PASS 2327, SKIP 10,
47 subtests passed. Four real native tests use `shutil.which("restic")` instead
of that override and picked host 0.20.0. Affected rerun with pinned 0.19.1 first in PATH: PASS 4. The full suite was
not repeated afterward; the original full result remains FAIL.

Additional fail-closed CLI token/no-selection regression and recipe/native CLI suite: PASS 40.

Registered installed Save trial initially FAIL: public component_plan_token was
redacted by CLI JSON serialization, so create correctly refused the stale token.
The fix preserves only an exact 64-character lowercase hexadecimal digest under
that program-owned field; authentication and arbitrary token fields remain
redacted. Regression crosses the emit/JSON boundary. Affected serializer/CLI/
safety run: 106 PASS, 1 FAIL from a test using a read-only default host state
path. That single test rerun with task-local XDG_STATE_HOME/instance: PASS.

Actual pinned local Restic regression with synthetic Homebrew archive bytes:
PASS unchanged-workspace + changed-selected-archive creates a new logical point;
unchanged archive and explicit keep return already-saved; explicit omit retains
historical encrypted objects. JAT validation is a fixture here, not managed JAT.

## Registered installed Save with real disposable provider

PASS: actual installed `joshRoom.save`, native chooser and coherent preview,
normal controller encryption/catalog/Restic publication against real pinned
loopback MinIO and production Secret Service on its own task-only D-Bus/socket.
The sequence saved a new point, repeated as already-saved, then cancelled a
preview after another source edit. A subsequent normal CLI catalog read verified
exactly four points (three seeded plus one registered Save), preserving the
three earlier points and the same latest/marker on repeat and cancellation.
See `registered-save.json`, `registered-save-preview.png` and the driver.

Runtime limitation: the installed registered handler uses its existing
`__test__.setRuntimeForTests` with an explicit `local-build-fallback` Python
command loading the installed VSIX's controller sources. This bypasses managed
RCC artifact selection only. Native keyring, provider, CLI, capture policy,
preview token, Restic and publication paths are real. An empty task-local JAT
log root satisfies the adapter's log follower contract; no JAT binary or artifact
was represented as present and no JAT-owned component capture was tested.
This is **not managed RCC artifact acceptance**.

The fixture followed #209's isolated recipe: a no-network unprivileged
Secret Service container, generated provider credentials kept only in its
production keyring, hash-verified official MinIO on task-specific loopback
ports, normal bucket/encryption enrollment, transient recovery handoff removed
after enrollment, and an observed foreground MinIO runner for test lifetime.
A too-permissive task runtime directory was refused and fixed before seeding.
The initial adapter omitted JAT's log-root field; adding an empty log root fixed
the test adapter without changing product security. The first real registered
create then exposed the CLI public-token redaction bug, which was repaired and
retested successfully. No failed attempt is counted as success.

Remaining gates: managed RCC execution, managed JAT Homebrew roundtrip, external
component real-provider selection, fresh installed Homebrew closure safety,
Windows/macOS native UI and #188 Dimension-authorized agent capture.

Final exact-case public field hardening: `component_plan_token` only, with an
exact lowercase SHA256 shape. Uppercase field aliases, malformed/non-string/
uppercase/short/long values, unrelated token/credential fields and sensitive
parents retain default redaction. Independent read-only narrow review found no
redaction bypass. Preview → bounded JSON → create success and replay after a
new latest snapshot rejection: PASS with pinned local Restic. Exact-key
serializer/CLI/security suite: PASS 115. Final repackaged registered Save
sequence passed again; catalog count rose by exactly one (4 → 5) and retained
all prior points, with repeat/cancel adding none.

Actual installed `joshRoom.new` and `joshRoom.saveFolderAs` also PASS against
the same isolated real provider/keyring in a separate synthetic source folder.
They traversed actual native folder/name/Room/onboarding controls and the same
component chooser/preview. New Room created the project; Save Folder As wrote
a second distinct point bound to that Room/Dimension. The built-in native
simple folder dialog setting was enabled only in the disposable VS Code
profile. No picker API was mocked. See `registered-new-folder.json` and driver.
Both retain the explicit source-Python override limitation.

## Installed Homebrew closure checkpoint (2026-11-22)

Supersedes the earlier blanket unavailable statement only for the qualified
Hello 2.12.3 / Debian 12 x86_64 / exact-prefix executable and loader/libc hashes.
The existing earlier jq and recipe results are retained, not generalized.

- PASS: real installed wheel plus explicitly supplied unchanged repository Restic
  runtime assets; task-only Secret Service and pinned real MinIO; normal Preview,
  Save, unchanged repeat, stale preview rejection and immutable component export.
  The standalone wheel alone did not contain its external runtime assets and is
  not claimed to be a complete standalone managed-runtime distribution.
- PASS: unchanged pinned JAT source validates the exact Store-exported archive.
  This is not a managed RCC artifact run.
- PASS: actual offline target CLI restores that export in a second clean container.
  All 60 regular files, safe file modes, five contained aliases and the complete
  171-entry topology match. Hello version and custom greeting execute; loader
  tracing confirms captured libc is loaded. A second restore refuses the occupied
  prefix and leaves every file unchanged.
- PASS: 127 Python bridge/lifecycle/logical-descriptor/prefix tests; 2 SKIP (optional
  RCC fixture inputs unavailable). Uses pinned `JOSH_ROOM_TEST_RESTIC=.../0.19.1/...`;
  the initial host Restic 0.20.0 run failed 10 tests and is retained as a setup failure.
- PASS: 196 focused component, CLI, archive/selection tests after one CLI test's
  XDG state was isolated. The initial test tried a sandbox-blocked host state path;
  no host state was written.
- PASS: 110 controller artifact/material security/public storage/Dimension schema
  tests; canonical/controller/template bytes synchronized.
- PASS: 264 Node chooser/extension tests; 2 SKIP (optional Python contract environment).
- PASS: scoped Ruff, whitespace check and local-only VSIX packaging.
- Ordinary independent correctness review found input snapshot-binding, internal
  links, negative tar sizes, executable-mode and cleanup issues; fixes and focused
  regressions landed. Independent security-review invocation was unavailable:
  that separate review gate is SKIP, not a passing security assessment.

The detailed synthetic artifact/provenance/result boundary is
[homebrew-installed-prefix.json](homebrew-installed-prefix.json). The target
runner is [homebrew-prefix-target-driver.py](homebrew-prefix-target-driver.py);
it requires the test wheel installed in the task user base and a previously
Store-exported archive at `/task/component-from-store.tar.zst`, plus an absent
private exact prefix. It must only run inside the disposable target container.

PASS: the packaged VSIX was installed in a disposable profile. Its real native
picker rendered the conditional installed row using the actual container CLI
capability, then cancelled cleanly. See installed-prefix-picker.json/.png and
the committed driver. Full registered Save with this new installed row remains
NOT RUN; this native UI proof does not replace the separate actual CLI closure test.

Retained CI setup failure: Windows first failed because this repository has no
uv.lock but the added command used --locked; removed only that unsupported flag.
The exact-head rerun remains separate from local Python and Linux proof.

NOT RUN/SKIP: managed RCC controller/JAT artifact, native Windows installed restore
(deliberately unavailable; refusal/schema CI added), macOS, other formula/runtime
hashes, locales/NSS/services, cross-distro/prefix portability, whole Homebrew
manager, full work-jatt binary capture and verified native reinstallation.
No merge, tag, release staging, release publication or paid call occurred.
