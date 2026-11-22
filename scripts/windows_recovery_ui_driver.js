"use strict";
// Development driver for the installed extension. No production callbacks are replaced.
const vscode = require("vscode");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { applyInstalledRuntime } = require("./installed_recovery_runtime");
const base = process.env.JOSH_ROOM_ACCEPTANCE_ROOT;
const hash = value => crypto.createHash("sha256").update(value).digest("hex");
const write = (name, value) => fs.writeFileSync(path.join(base, name), JSON.stringify(value));

exports.activate = async context => {
  const target = vscode.extensions.getExtension("joshyorko.josh-room");
  if (!target) throw new Error("Candidate VSIX was not installed");
  await target.activate();
  const runtimeApis = applyInstalledRuntime(target.extensionPath, {
    command: process.env.JOSH_ROOM_ACCEPTANCE_PYTHON,
    args: args => [path.join(base, "windows_recovery_controller.py"), ...args, "--json"],
    env: { PYTHONPATH: path.join(target.extensionPath, "runtime/controller"),
      JOSH_ROOM_CONFIG_DIR: path.join(base, "provider-config"),
      XDG_STATE_HOME: path.join(base, "provider-state"),
      XDG_CACHE_HOME: path.join(base, "provider-cache"),
      XDG_RUNTIME_DIR: path.join(base, "provider-runtime") },
    mode: "local-build-fallback",
  });
  const enrolled = JSON.parse(fs.readFileSync(path.join(base, "provider-ready.json"), "utf8"));
  const selectedDimension = { id: "recovery-fixture", provider: "minio",
    encryption_domain_id: enrolled.encryption_domain_id, key_generation: enrolled.key_generation };
  const handoff = path.join(base, "provider-runtime/existing-operational.identity");
  let seeded = 0;
  for (const api of runtimeApis) {
    await api.cacheEncryptionMaterial({ ...selectedDimension, encryption_material: handoff }, selectedDimension);
    const cached = await api.readEncryptionMaterial(selectedDimension);
    if (cached?.identity?.trim() === fs.readFileSync(handoff, "utf8").trim()) seeded++;
  }
  if (!seeded) throw new Error("Real native SecretStorage enrollment failed");
  fs.unlinkSync(handoff);
  const pins = JSON.parse(fs.readFileSync(path.join(base, "provider-pins.json"), "utf8"));
  let invocation = 0;
  context.subscriptions.push(vscode.commands.registerCommand("recoveryFixture.start", async () => {
    const current = ++invocation;
    write("ui-command-started.json", { invocation: current, pin: pins[0] });
    const probe = setTimeout(() => {
      const children = process._getActiveHandles().filter(handle => Array.isArray(handle.spawnargs)
        && handle.spawnargs.some(value => value === "josh_room" || String(value).endsWith("windows_recovery_controller.py")));
      write("ui-process-state.json", { invocation: current, native_controller_children: children.map(child => {
        const offset = child.spawnargs.findIndex(value => value === "josh_room" || String(value).endsWith("windows_recovery_controller.py"));
        return { pid: child.pid, command: child.spawnargs.slice(offset + 1, offset + 3) };
      }) });
    }, 5000);
    try {
      const result = await vscode.commands.executeCommand("joshRoom.browseRecoveryPoint", {
        id: "synthetic-room", dimension: selectedDimension, snapshot_id: pins[0],
      });
      write("ui-command-finished.json", { result, pin: pins[0], invocation: current });
    } catch (error) {
      const message = String(error.message || "").toLowerCase();
      write("ui-command-failed.json", { state: "FAIL", boundary: "registered-command",
        invocation: current, error_type: ["TypeError", "Error", "RangeError"].includes(error.name) ? error.name : "other",
        categories: ["private", "authorization", "snapshot", "logical", "path", "provider", "runtime", "trust"]
          .filter(category => message.includes(category)),
        controller_exit: Number.isSafeInteger(error.controller_exit_status) ? error.controller_exit_status : null });
    } finally { clearTimeout(probe); }
  }));
  context.subscriptions.push(vscode.commands.registerCommand("recoveryFixture.focusDiff", async () => {
    try {
      const tab = vscode.window.tabGroups.activeTabGroup.activeTab;
      if (!(tab?.input instanceof vscode.TabInputTextDiff)) throw new Error("Diff tab missing");
      const expected = tab.input.modified.toString();
      await vscode.commands.executeCommand("workbench.action.compareEditor.focusSecondarySide");
      await new Promise((resolve, reject) => {
        const check = () => {
          if (vscode.window.activeTextEditor?.document.uri.toString() === expected) {
            clearTimeout(timer); subscription.dispose(); resolve();
          }
        };
        const subscription = vscode.window.onDidChangeActiveTextEditor(check);
        const timer = setTimeout(() => { subscription.dispose(); reject(new Error("Native focus unavailable")); }, 5000);
        check();
      });
      write("ui-editor-focus.json", { state: "PASS", native_command: "workbench.action.compareEditor.focusSecondarySide",
        modified_diff_editor_active: true });
    } catch { write("ui-editor-focus.json", { state: "FAIL", boundary: "native-secondary-diff-focus" }); }
  }));
  context.subscriptions.push(vscode.commands.registerCommand("recoveryFixture.verifyDiff", async () => {
    let stage = "native-diff-tab";
    let observed;
    try {
      const tab = vscode.window.tabGroups.all.flatMap(group => group.tabs)
        .find(tab => tab.input instanceof vscode.TabInputTextDiff);
      if (!tab || tab.input.original.scheme !== "josh-room-recovery"
        || tab.input.modified.scheme !== "josh-room-recovery") throw new Error("Native readonly diff missing");
      const left = await vscode.workspace.openTextDocument(tab.input.original);
      const right = await vscode.workspace.openTextDocument(tab.input.modified);
      const before = 'def greeting():\n\treturn "first 你好"\n';
      const after = 'def greeting():\n\treturn "third 你好"\n';
      observed = { before_sha256: hash(left.getText()), after_sha256: hash(right.getText()),
        before_dirty: left.isDirty, after_dirty: right.isDirty, before_eol: left.eol, after_eol: right.eol };
      stage = "whitespace-unicode-digests";
      if (left.getText() !== before || right.getText() !== after) throw new Error("Diff bytes changed");
      stage = "opaque-provider-uri";
      if ([left, right].some(doc => doc.uri.toString().includes(pins[0]) || doc.uri.toString().includes(base))) {
        throw new Error("Historical provider URI is not opaque");
      }
      stage = "native-typing-readonly";
      if (left.getText() !== before || right.getText() !== after || left.isDirty || right.isDirty) throw new Error("Readonly edit accepted");
      write("ui-diff.json", { state: "PASS", native_diff_tab: true, readonly_edit_denied: true,
        readonly_probe: "native-CDP-editor-typing",
        whitespace_unicode_preserved: true, before_sha256: hash(before), after_sha256: hash(after),
        opaque_provider_uris: !left.uri.toString().includes(pins[0]) && !left.uri.toString().includes(base) });
    } catch {
      write("ui-diff.json", { state: "FAIL", boundary: stage, observed });
    }
  }));
  write("ui-driver-ready.json", { state: "PASS", installed_extension: true,
    runtime: "source-Python-fallback-not-immutable-RCC", command: "joshRoom.browseRecoveryPoint",
    activated_runtime_modules_matched: runtimeApis.length, real_secret_storage_cache_seeded: seeded });
};
