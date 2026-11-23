"use strict";
// Invoke run() from a startup development driver in a disposable installed profile.
// VS Code refuses modal dialogs when launched with --extensionTestsPath.
// Use the synthetic provider prepared by composition_minio_acceptance --hold-for-native.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const vscode = require("vscode");
const { applyInstalledRuntime } = require("./installed_recovery_runtime");
const { connect, until } = require("./windows_recovery_installed");

exports.run = async () => {
  const scratch = fs.realpathSync(process.env.JOSH_ROOM_ACCEPTANCE_ROOT);
  const ready = JSON.parse(fs.readFileSync(path.join(scratch, "native-ready.json")));
  const base = fs.realpathSync(ready.fixture_root);
  assert.equal(path.dirname(base), scratch);
  assert.equal(ready.receipt.provider, "loopback-minio");
  const command = process.env.JOSH_ROOM_ACCEPTANCE_ENTRY || "joshRoom.saveCatalog";
  assert.ok(["joshRoom.saveCatalog", "joshRoom.new"].includes(command));
  const source = ready.native_sources?.[command] || path.join(base, "provider-source");
  assert.equal(path.dirname(fs.realpathSync(source)), base);
  assert.equal(fs.existsSync(path.join(source, ".josh-room.json")), false);
  const gitPath = "projects/a";
  const installed = vscode.extensions.getExtension("joshyorko.josh-room");
  assert.ok(installed);
  assert.equal(path.dirname(fs.realpathSync(installed.extensionPath)), fs.realpathSync(process.env.JOSH_ROOM_ACCEPTANCE_EXTENSIONS || path.join(scratch, "extensions")));
  await installed.activate();
  const env = { PYTHONPATH: path.join(installed.extensionPath, "runtime/controller"),
    DBUS_SESSION_BUS_ADDRESS: "unix:path=" + path.join(scratch, "keyring-runtime/bus"),
    JOSH_ROOM_CONFIG_DIR: path.join(base, "provider-config"), JOSH_ROOM_INSTANCE: path.join(base, "provider-instance"),
    XDG_STATE_HOME: path.join(base, "provider-state"), XDG_CACHE_HOME: path.join(base, "provider-cache"),
    XDG_RUNTIME_DIR: path.join(base, "provider-runtime"), JOSH_ROOM_EXTENSION_VERSION: installed.packageJSON.version };
  const handoffPath = process.env.JOSH_ROOM_TEST_REFERENCE_JAT_RUNTIME;
  if (handoffPath) {
    const { rcc, jatRcc, jat, paths } = JSON.parse(fs.readFileSync(handoffPath));
    Object.assign(env, { JOSH_ROOM_JAT_ROOT: jat.jatRoot, JOSH_ROOM_JAT_RCC_EXE: (jatRcc || rcc).executable,
      JOSH_ROOM_JAT_ARTIFACT: jat.artifact, JOSH_ROOM_RCC_EXE: rcc.executable,
      JOSH_ROOM_EXTENSION_MODE: "1", JOSH_ROOM_RCC_HOME: paths.rccHome, ROBOCORP_HOME: paths.rccHome });
  }
  const python = process.env.JOSH_ROOM_ACCEPTANCE_PYTHON;
  const args = values => ["-m", "josh_room", ...values, "--json"];
  const cli = (...values) => JSON.parse(execFileSync(python, args(values), { cwd: base, env: { ...process.env, ...env }, timeout: 240000 }));
  const runtimeApis = applyInstalledRuntime(installed.extensionPath, { command: python, args, env, mode: "local-build-fallback", jatArtifact: env.JOSH_ROOM_JAT_ARTIFACT, jatRoot: env.JOSH_ROOM_JAT_ROOT || base });
  if (ready.receipt.uncached_domain_verified) {
    const status = cli("encryption", "status", "--dimension", "composition-fixture");
    assert.ok(status.encryption_domain_id && status.key_generation);
    for (const api of runtimeApis) {
      assert.ok(!(await api.readEncryptionMaterial(status)), "expected fresh editor SecretStorage");
    }
  }
  const renderer = await connect(Number(process.env.JOSH_ROOM_ACCEPTANCE_CDP_PORT));
  const actions = require(path.join(installed.extensionPath, "catalog-actions.js"));
  const originalSave = actions.saveCatalog;
  // Supply paths to the native OS file chooser. All Quick Picks, the approval,
  // the registered command, and the installed production callbacks are exercised.
  let manualDomainPrompt = false;
  const syntheticDialogs = api => ({ QuickPickItemKind: api.QuickPickItemKind, window: {
    showQuickPick: api.window.showQuickPick, showWarningMessage: api.window.showWarningMessage,
    showInputBox: options => {
      if (/encryption domain/i.test(options.title || "")) {
        manualDomainPrompt = true;
        throw new Error("Catalog creation requested a manual encryption domain");
      }
      return api.window.showInputBox(options);
    },
    showInformationMessage: api.window.showInformationMessage,
    showOpenDialog: async options => {
      const locations = {
        "Save Catalog Room — source": source,
        "Capture as independent child Rooms": path.join(source, gitPath),
        "Select Brewfile-only recipe directory": path.join(scratch, "component-input/Brew"),
        "Select additional Hauler file": path.join(scratch, "component-input/hauler.txt"),
      };
      assert.ok(Object.hasOwn(locations, options.title), "Unexpected catalog file chooser");
      return [api.Uri.file(locations[options.title])];
    },
  } });
  let verifiedDomain;
  actions.saveCatalog = (api, callbacks) => originalSave(syntheticDialogs(api), {
    ...callbacks,
    plan: async selection => {
      if (ready.receipt.uncached_domain_verified) assert.ok(!selection.dimension.encryption_domain_id);
      const preview = await callbacks.plan(selection);
      verifiedDomain = preview.request.destination_binding.encryption_domain_id;
      assert.notEqual(verifiedDomain.replaceAll("-", ""), selection.namespace);
      return preview;
    },
  });
  const picker = require(path.join(installed.extensionPath, "component-picker.js"));
  const originalPick = picker.pickComponents;
  picker.pickComponents = (api, ...inputs) => originalPick(syntheticDialogs(api), ...inputs);
  try {
    const result = vscode.commands.executeCommand(command);
    if (command === "joshRoom.new") await renderer.choose("Configure Catalog Room");
    await renderer.choose("Use workspace policy");
    // Component selection first appeared after the Git-only writer.
    const next = await until(async () => {
      const rows = (await renderer.widget())?.rows || [];
      return rows.find(row => row.includes("Workspace only") || row.includes("Metadata only parent"));
    }, "child components or parent content");
    if (handoffPath) {
      await renderer.choose("Choose RCC, Homebrew or Hauler components");
      await until(async () => (await renderer.widget())?.rows.some(row => row.includes("RCC exact environment")), "component picker");
      for (const label of ["RCC exact environment", "Homebrew recovery export", "OCI images / additional Hauler content"]) {
        assert.equal(await renderer.evaluate(`(() => {
          const row = [...document.querySelectorAll(".quick-input-widget .monaco-list-row")].find(row => row.innerText.includes(${JSON.stringify(label)}));
          const box = row?.querySelector("[role=checkbox]");
          if (!box) return false;
          if (box.getAttribute("aria-checked") !== "true") box.click();
          return true;
        })()`), true);
      }
      await vscode.commands.executeCommand("workbench.action.acceptSelectedQuickOpenItem");
      await renderer.choose("Select Brewfile-only recipe directory");
      await renderer.choose("Select additional Hauler file");
      await until(async () => (await renderer.widget())?.text.includes("Hauler file name"), "attachment name");
      await renderer.text("synthetic-file");
      await renderer.key("Enter", "Enter", 13);
    } else if (next.includes("Workspace only")) await renderer.choose("Workspace only");
    await renderer.choose("Metadata only parent");
    await renderer.choose("Synthetic Composition");
    const approvalText = () => renderer.evaluate("document.querySelector('.monaco-dialog-box')?.innerText");
    const approval = await until(async () => {
      assert.equal(manualDomainPrompt, false, "Catalog creation requested a manual encryption domain");
      const text = await approvalText();
      return text?.includes("Save this Catalog Room") && text;
    }, "visible approval", 300000);
    assert.match(approval, /Synthetic Composition/);
    assert.match(approval, /minio/i);
    const names = ["rcc_environment", "homebrew_recovery", "hauler_content"];
    if (handoffPath) {
      for (const name of names) assert.ok(approval.includes(name + ": refresh"), name);
    }
    assert.match(approval, /projects\/a[^\n]*full-repository/i);
    assert.equal(await renderer.evaluate(`(() => {
      const button = [...document.querySelectorAll('.monaco-dialog-box .monaco-button')].find(b => b.textContent.includes('Save and acknowledge'));
      if (!button) return false;
      button.click();
      return true;
    })()`), true);
    await until(async () => renderer.evaluate(`[...document.querySelectorAll('.notification-list-item')].some(n=>n.innerText.includes("Catalog saved."))`), "published catalog", 240000);
    await vscode.commands.executeCommand("notifications.clearAll");
    const saved = await result;
    assert.equal(saved.ok, true);
    if (handoffPath) {
      const child = saved.children.find(row => row.path === gitPath);
      assert.deepEqual(child.components.toSorted(), names.toSorted());
    }
    const common = ["--namespace", saved.namespace, "--parent-room", "catalog-" + saved.namespace,
      "--dimension", "composition-fixture", "--parent-snapshot", saved.parent_snapshot_id];
    const target = path.join(base, "native-cold-" + saved.namespace);
    cli("composition", "restore-parent", ...common, "--destination", target);
    for (const child of [gitPath, "b"]) assert.equal(fs.existsSync(path.join(target, child)), false);
    const entered = cli("composition", "enter-child", ...common, "--workspace", target, "--child-path", gitPath);
    assert.equal(entered.marker_verified, true);
    const domain = cli("encryption", "status", "--dimension", "composition-fixture").encryption_domain_id;
    assert.equal(verifiedDomain, domain);
    for (const folder of [target, path.join(target, gitPath)]) {
      const marker = JSON.parse(fs.readFileSync(path.join(folder, ".josh-room.json")));
      assert.equal(marker.encryption_domain_id, domain);
    }
    const git = (root, ...values) => execFileSync("git", ["-C", path.join(root, gitPath), ...values], {
      encoding: "utf8", env: { ...process.env, GIT_OPTIONAL_LOCKS: "0", GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null" } });
    for (const probe of [["fsck", "--full"], ["show-ref"], ["rev-parse", "HEAD"], ["ls-files", "--stage"], ["stash", "list"], ["status", "--porcelain=v1"]]) {
      assert.equal(git(target, ...probe), git(source, ...probe));
    }
    assert.equal(fs.existsSync(path.join(target, "b")), false);
    const receiptName = command === "joshRoom.new" ? "installed-new-catalog-receipt.json" : "installed-catalog-receipt.json";
    fs.writeFileSync(path.join(scratch, receiptName), JSON.stringify({
      command, empty_encryption_cache_verified: Boolean(ready.receipt.uncached_domain_verified),
      uncached_dimension: Boolean(ready.receipt.uncached_domain_verified),
      verified_plan_and_restored_descriptor_domains: true, manual_domain_input: false, runtime: "packaged-controller-existing-Python", file_chooser: "synthetic paths",
      visible_full_repository_approval: true, provider: ready.receipt.provider, parent_pin: saved.parent_snapshot_id,
      child_pin: entered.snapshot_id, git_verified: true, sibling_absent: true,
      selected_components: handoffPath ? names : [],
    }, null, 2));
  } finally {
    actions.saveCatalog = originalSave;
    picker.pickComponents = originalPick;
    renderer.socket.close();
  }
};
