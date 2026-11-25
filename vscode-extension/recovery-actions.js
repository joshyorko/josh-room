"use strict";

const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { isRoomMarker } = require("./dirty");
const { confirmDestructive } = require("./messaging");

function readLocalBinding(root, name) {
  const fd = fs.openSync(path.join(root, name), fs.constants.O_RDONLY
    | (fs.constants.O_NOFOLLOW || 0) | (fs.constants.O_NONBLOCK || 0));
  try {
    const info = fs.fstatSync(fd);
    if (!info.isFile() || info.nlink !== 1 || info.size > 64 * 1024) throw new Error("Invalid current child metadata.");
    const bytes = Buffer.alloc(info.size + 1);
    if (fs.readSync(fd, bytes) !== info.size) throw new Error("Current child metadata changed.");
    return JSON.parse(bytes.subarray(0, info.size).toString("utf8"));
  } finally { fs.closeSync(fd); }
}

function isChildSnapshotId(id) {
  return typeof id === "string" && /^jat-[a-f0-9]{32}$/.test(id);
}

function currentChildPin(root) {
  let marker;
  try { marker = readLocalBinding(root, ".josh-room.json"); }
  catch (error) { if (error.code === "ENOENT") return undefined; throw error; }
  if (marker.format_version !== 4) return undefined;
  const real = fs.realpathSync(root);
  const digest = crypto.createHash("sha256").update(real).digest("hex");
  if (process.platform !== "linux" || real !== path.resolve(root) || !isRoomMarker(marker)
      || !isChildSnapshotId(marker.snapshot_id)
      || marker.workspace_path_sha256 !== digest) throw new Error("Current namespace marker binding differs.");
  const guard = readLocalBinding(root, ".josh-room-split.json");
  if (!guard || Object.keys(guard).length !== 3 || guard.namespace !== marker.composition_namespace
      || guard.room_id !== marker.project_id || guard.snapshot_id !== marker.snapshot_id) {
    throw new Error("Current child guard binding differs.");
  }
  return { project_id: marker.project_id, snapshot_id: marker.snapshot_id, dimension_id: marker.dimension_id,
    encryption_domain_id: marker.encryption_domain_id, namespace: marker.composition_namespace };
}

// Keep the original authenticated point through navigation, paging and cancellation.
async function browseRecovery(vscode, context, run, workspace) {
  const pin = Object.freeze({ ...context });
  if (!pin.project_id || !pin.dimension_id || !pin.snapshot_id || pin.snapshot_id === "latest") {
    throw new Error("Select an exact historical recovery point.");
  }
  const invoke = async (action, path, extra = []) => {
    const result = await run(["recovery", action, pin.project_id, "--snapshot", pin.snapshot_id,
      ...(pin.namespace ? ["--namespace", pin.namespace, "--expected-domain", pin.encryption_domain_id] : []), "--path", path, ...extra]);
    if (!result || result.snapshot_id !== pin.snapshot_id || result.dimension_id !== pin.dimension_id
      || result.room_id !== pin.project_id) throw new Error("Historical recovery identity changed.");
    if (pin.namespace) {
      const matches = (row, snapshot) => row?.snapshot_id === snapshot && row.room_id === pin.project_id
        && row.dimension_id === pin.dimension_id && row.namespace === pin.namespace
        && row.encryption_domain_id === pin.encryption_domain_id;
      const compare = extra.indexOf("--compare-snapshot");
      if (!matches(result, pin.snapshot_id) || (compare >= 0 && !matches(result.compare_to, extra[compare + 1]))) {
        throw new Error("Historical child recovery identity changed.");
      }
    }
    return result;
  };
  const textViews = require("./recovery-text");
  const show = async (result) => {
    result = textViews.metadataOnly(result);
    const document = await vscode.workspace.openTextDocument({ language: "json", content: JSON.stringify(result, null, 2) });
    await vscode.window.showTextDocument(document, { preview: true });
  };
  const showComparison = async (result, selected) => {
    if (!await textViews.showTextPreview(vscode, result, selected, `${pin.snapshot_id} · ${selected}`)) await show(result);
  };
  let path = ".";
  let offset = 0;
  while (true) {
    const result = await invoke("tree", path, ["--offset", String(offset), "--limit", "100"]);
    const choices = result.entries.map((entry) => ({ label: entry.name, description: entry.type,
      entry, path: path === "." ? entry.name : `${path}/${entry.name}` }));
    if (path !== ".") choices.unshift({ label: "..", action: "parent" });
    if (result.next_offset !== null) choices.push({ label: "Next page…", action: "next" });
    if (offset > 0) choices.unshift({ label: "First page…", action: "first" });
    const chosen = await vscode.window.showQuickPick(choices, {
      title: `Recovery ${pin.snapshot_id} · ${path}`, placeHolder: "Browse metadata without hydrating the Room",
    });
    if (!chosen) return "cancelled";
    if (chosen.action === "next") { offset = result.next_offset; continue; }
    if (chosen.action === "first") { offset = 0; continue; }
    if (chosen.action === "parent") { path = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "."; offset = 0; continue; }
    const directory = chosen.entry.type === "dir";
    const actions = directory ? ["Browse", "Recover selection…", "Compare with recovery point…"]
      : ["Preview text", "Inspect checksum", "Recover selection…", "Compare with current workspace", "Compare with recovery point…"];
    const action = await vscode.window.showQuickPick(actions, { title: chosen.path });
    if (!action) continue;
    if (action === "Browse") { path = chosen.path; offset = 0; continue; }
    if (action === "Inspect checksum") { await show(await invoke("inspect", chosen.path)); continue; }
    if (!vscode.workspace.isTrusted) throw new Error("Trust this workspace before recovery or comparison.");
    if (action === "Preview text") {
      await showComparison(await invoke("inspect", chosen.path, ["--include-text"]), chosen.path);
    } else if (action === "Recover selection…") {
      const destination = await vscode.window.showSaveDialog({ title: "Recover to a new, absent destination", saveLabel: "Recover selection" });
      if (!destination) continue;
      if (destination.scheme !== "file") throw new Error("Recovery requires a local filesystem destination.");
      await show(await invoke("extract", chosen.path, ["--destination", destination.fsPath]));
    } else if (action === "Compare with current workspace") {
      if (!workspace) throw new Error("Open an explicit workspace to compare.");
      await showComparison(await invoke("diff", chosen.path, ["--workspace", workspace, "--include-text"]), chosen.path);
    } else {
      const other = await vscode.window.showInputBox({ title: "Exact recovery point ID to compare", ignoreFocusOut: true });
      if (!other) continue;
      if (other === "latest") throw new Error("Comparison requires an exact recovery point.");
      await showComparison(await invoke("diff", chosen.path, ["--compare-snapshot", other, "--include-text"]), chosen.path);
    }
  }
}
module.exports = { browseRecovery, currentChildPin, isChildSnapshotId, confirmDestructive };
