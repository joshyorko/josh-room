"use strict";
// Development driver for the installed extension. No production callbacks are replaced.
const vscode = require("vscode");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const base = process.env.JOSH_ROOM_ACCEPTANCE_ROOT;
const hash = value => crypto.createHash("sha256").update(value).digest("hex");
const write = (name, value) => fs.writeFileSync(path.join(base, name), JSON.stringify(value));

exports.activate = async context => {
  const target = vscode.extensions.getExtension("joshyorko.josh-room");
  if (!target) throw new Error("Candidate VSIX was not installed");
  const extension = require(path.join(target.extensionPath, "extension"));
  extension.__test__.setRuntimeForTests({
    command: process.env.JOSH_ROOM_ACCEPTANCE_PYTHON,
    args: args => ["-m", "josh_room", ...args, "--json"],
    env: { PYTHONPATH: path.join(target.extensionPath, "runtime/controller"),
      JOSH_ROOM_CONFIG_DIR: path.join(base, "provider-config"),
      XDG_STATE_HOME: path.join(base, "provider-state"),
      XDG_CACHE_HOME: path.join(base, "provider-cache"),
      XDG_RUNTIME_DIR: path.join(base, "provider-runtime") },
    mode: "local-build-fallback",
  });
  await target.activate();
  const pins = JSON.parse(fs.readFileSync(path.join(base, "provider-pins.json"), "utf8"));
  context.subscriptions.push(vscode.commands.registerCommand("recoveryFixture.start", async () => {
    try {
      const result = await vscode.commands.executeCommand("joshRoom.browseRecoveryPoint", {
        id: "synthetic-room", dimension: { id: "recovery-fixture", provider: "minio" }, snapshot_id: pins[0],
      });
      write("ui-command-finished.json", { result, pin: pins[0] });
    } catch {
      write("ui-command-failed.json", { state: "FAIL", boundary: "registered-command" });
    }
  }));
  context.subscriptions.push(vscode.commands.registerCommand("recoveryFixture.verifyDiff", async () => {
    try {
      const tab = vscode.window.tabGroups.all.flatMap(group => group.tabs)
        .find(tab => tab.input instanceof vscode.TabInputTextDiff);
      if (!tab || tab.input.original.scheme !== "josh-room-recovery"
        || tab.input.modified.scheme !== "josh-room-recovery") throw new Error("Native readonly diff missing");
      const left = await vscode.workspace.openTextDocument(tab.input.original);
      const right = await vscode.workspace.openTextDocument(tab.input.modified);
      const before = 'def greeting():\n\treturn "first 你好"\n';
      const after = 'def greeting():\n\treturn "third 你好"\n';
      if (left.getText() !== before || right.getText() !== after) throw new Error("Diff bytes changed");
      const edit = new vscode.WorkspaceEdit();
      edit.insert(left.uri, new vscode.Position(0, 0), "synthetic forbidden edit");
      const accepted = await vscode.workspace.applyEdit(edit);
      if (accepted || left.getText() !== before || left.isDirty || right.isDirty) throw new Error("Readonly edit accepted");
      write("ui-diff.json", { state: "PASS", native_diff_tab: true, readonly_edit_denied: true,
        whitespace_unicode_preserved: true, before_sha256: hash(before), after_sha256: hash(after),
        opaque_provider_uris: !left.uri.toString().includes(pins[0]) && !left.uri.toString().includes(base) });
    } catch {
      write("ui-diff.json", { state: "FAIL", boundary: "native-diff-or-readonly" });
    }
  }));
  write("ui-driver-ready.json", { state: "PASS", installed_extension: true,
    runtime: "source-Python-fallback-not-immutable-RCC", command: "joshRoom.browseRecoveryPoint" });
};
