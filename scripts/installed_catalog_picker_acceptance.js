"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { until } = require("./windows_recovery_installed");

function markerNamespace(root) {
  const namespace = JSON.parse(fs.readFileSync(path.join(root, ".josh-room.json"))).composition_namespace;
  assert.match(namespace, /^[0-9a-f]{32}$/);
  return namespace;
}

async function exercisePicker({ command, vscode, renderer, cli, base, ready, scratch }) {
  const root = fs.realpathSync(vscode.workspace.workspaceFolders[0].uri.fsPath);
  const selected = "projects/a";
  const refreshed = path.join(base, "picker-refreshed-parent-" + (command === "joshRoom.enterChild" ? JSON.parse(fs.readFileSync(path.join(scratch, "installed-picker-refresh.json"))).namespace : markerNamespace(root)));
  const receipt = path.join(scratch, "installed-picker-refresh.json");
  const enterReceipt = path.join(scratch, "installed-picker-enter.json");
  const marker = folder => JSON.parse(fs.readFileSync(path.join(folder, ".josh-room.json")));
  async function choosePath(title) {
    const rows = await until(async () => {
      const widget = await renderer.widget();
      if (!widget) return false;
      assert.ok(widget.text.includes(title), "unexpected typed input or unrelated picker");
      return widget.rows.some(row => row.includes(selected)) && widget.rows;
    }, "saved child path picker");
    const i = rows.findIndex(row => row.includes(selected));
    assert.ok(i >= 0);
    await renderer.key("Home", "Home", 36);
    for (let position = 0; position < i; position++) await renderer.key("ArrowDown", "ArrowDown", 40);
    await renderer.key("Enter", "Enter", 13);
  }
  const message = text => renderer.evaluate(`[...document.querySelectorAll('.notification-list-item')].some(n => n.innerText.includes(${JSON.stringify(text)}))`);
  if (command === "joshRoom.enterChild") {
    const pins = JSON.parse(fs.readFileSync(receipt));
    if (root === path.join(refreshed, selected)) {
      assert.equal(marker(root).snapshot_id, pins.updated_child);
      assert.equal(fs.readFileSync(path.join(root, "picker-update.txt"), "utf8"), "synthetic picker update\n");
      assert.equal(fs.existsSync(path.join(refreshed, "b")), false);
      fs.writeFileSync(enterReceipt, JSON.stringify({ command, child_path: selected, pin: pins.updated_child, opened_child: true, sibling_absent: true }));
      return;
    }
    assert.equal(root, refreshed);
    assert.ok(!fs.existsSync(path.join(root, selected)));
    const result = vscode.commands.executeCommand(command);
    await choosePath("Enter child Room");
    await until(() => message("Child Room verified at exact recovery point " + pins.updated_child), "verified pinned child", 180000);
    await vscode.commands.executeCommand("notifications.clearAll");
    await result;
    await until(() => fs.existsSync(enterReceipt), "opened child editor");
    return;
  }
  assert.equal(command, "joshRoom.refreshCatalogPins");
  assert.equal(path.dirname(root), base);
  assert.ok(root === ready.parent_working || path.basename(root) === "native-cold-" + marker(root).composition_namespace);
  const child = path.join(root, selected);
  const old = marker(child);
  const updateFile = path.join(child, "picker-update.txt");
  if (fs.existsSync(updateFile)) {
    assert.equal(fs.readFileSync(updateFile, "utf8"), "synthetic picker update\n");
  } else {
    fs.writeFileSync(updateFile, "synthetic picker update\n", { flag: "wx" });
  }
  const baseline = JSON.parse(fs.readFileSync(path.join(child, ".josh-room-split.json")));
  const current = cli("composition", "parent-children", "--namespace", old.composition_namespace,
    "--parent-room", marker(root).project_id, "--parent-snapshot", marker(root).snapshot_id,
    "--dimension", old.dimension_id, "--workspace", root);
  const pinned = current.children.find(row => row.path === selected).snapshot_id;
  assert.equal(baseline.snapshot_id, old.snapshot_id);
  const update = ["--workspace", child, "--room", old.project_id, "--namespace", old.composition_namespace,
    "--dimension", old.dimension_id, "--child-snapshot", pinned, "--operation-id", "installed-picker-update"];
  const plan = cli("composition", "plan-child-save", ...update);
  const updated = cli("composition", "save-child", ...update, "--approve", plan.approval_sha256);
  assert.notEqual(pinned, updated.snapshot_id);
  const result = vscode.commands.executeCommand(command);
  await choosePath("Refresh child pin");
  // Select the sole alternative by its visible timestamp label. The UI owns the exact ID.
  const choices = await until(async () => {
    const w = await renderer.widget();
    return w?.text.includes("Choose a saved recovery point") && w.rows;
  }, "saved recovery points");
  assert.equal(choices.length, 1);
  assert.ok(choices[0].includes(updated.snapshot_id));
  await renderer.key("Enter", "Enter", 13);
  const confirmation = await until(async () => {
    const body = await renderer.evaluate("document.querySelector('.monaco-dialog-box')?.innerText");
    return body?.includes("Publish refreshed parent pin") && body;
  }, "explicit parent refresh approval", 180000);
  assert.ok(confirmation.includes(updated.snapshot_id));
  assert.equal(await renderer.evaluate(`(() => {
    const button = [...document.querySelectorAll('.monaco-dialog-box .monaco-button')].find(b => b.textContent.trim() === 'Publish refreshed parent pin');
    if (!button) return false;
    button.click();
    return true;
  })()`), true);
  await until(() => message("Parent pin published"), "refreshed catalog", 180000);
  await vscode.commands.executeCommand("notifications.clearAll");
  const saved = await result;
  assert.ok(saved.ok);
  const parent = marker(root);
  assert.notEqual(parent.snapshot_id, saved.snapshot_id);
  cli("composition", "restore-parent", "--namespace", parent.composition_namespace, "--dimension", parent.dimension_id,
    "--parent-room", parent.project_id, "--parent-snapshot", saved.snapshot_id, "--destination", refreshed);
  assert.ok(!fs.existsSync(path.join(refreshed, selected)));
  fs.writeFileSync(receipt, JSON.stringify({ command, namespace: parent.composition_namespace, child_path: selected, baseline_child: pinned,
    updated_child: updated.snapshot_id, baseline_parent: parent.snapshot_id, refreshed_parent: saved.snapshot_id }));
}
module.exports = { exercisePicker };
