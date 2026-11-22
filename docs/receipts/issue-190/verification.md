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
`installed-picker.json` records that limit explicitly. Full installed Save needs
an isolated enrolled synthetic provider/Secret Service and qualified managed
runtime. Coordination with #209 is in progress; never use the real host keyring.

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
