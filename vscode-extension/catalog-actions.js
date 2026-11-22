"use strict";
const path = require("path");
const crypto = require("crypto");

function relativeSource(root, value) {
  const relative = path.relative(root, value);
  if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error("Select content inside the source folder.");
  }
  return relative.split(path.sep).join("/");
}

async function saveCatalog(vscode, { dimensions, plan, save }) {
  const source = await vscode.window.showOpenDialog({ title: "Save Catalog Room — source", canSelectFolders: true, canSelectFiles: false, canSelectMany: false });
  if (!source?.length) return "cancelled";
  const root = source[0].fsPath;
  const children = await vscode.window.showOpenDialog({ title: "Capture as independent child Rooms", defaultUri: source[0], canSelectFolders: true, canSelectFiles: false, canSelectMany: true });
  if (!children?.length) return "cancelled";
  const assignments = [{ path: ".", outcome: "local-only" }, ...children.map((item) => ({ path: relativeSource(root, item.fsPath), outcome: "child" }))];
  const parentMode = await vscode.window.showQuickPick([{ label: "Metadata only parent", id: "metadata" }, { label: "Include selected parent-owned content", id: "content" }], { title: "Parent content — everything else stays local only" });
  if (!parentMode) return "cancelled";
  if (parentMode.id === "content") {
    const owned = await vscode.window.showOpenDialog({ title: "Include in parent", defaultUri: source[0], canSelectFolders: true, canSelectFiles: true, canSelectMany: true });
    if (!owned) return "cancelled";
    assignments.push(...owned.map((item) => ({ path: relativeSource(root, item.fsPath), outcome: "parent" })));
  }
  const configured = await dimensions(root);
  const choice = await vscode.window.showQuickPick(configured.map((dimension) => ({ label: dimension.display_name || dimension.id, dimension })), { title: "Explicit Catalog Room destination" });
  if (!choice) return "cancelled";
  if (!choice.dimension.encryption_domain_id) {
    const domain = await vscode.window.showInputBox({ title: "Encryption domain from the existing enrollment receipt", prompt: "Non-secret domain ID. The provider will corroborate it after approval.",
      validateInput: (value) => /^[a-f0-9-]{36}$/i.test(value) ? undefined : "Enter the enrollment domain UUID." });
    if (!domain) return "cancelled";
    choice.dimension = { ...choice.dimension, encryption_domain_id: domain };
  }
  const namespace = crypto.randomUUID().replaceAll("-", "");
  const parentRoom = `catalog-${namespace}`;
  const selection = { root, assignments, namespace, parentRoom, dimension: choice.dimension };
  const preview = await plan(selection);
  const approved = await vscode.window.showWarningMessage("Save this Catalog Room and its children?", {
    modal: true,
    detail: `${children.length} independent child Rooms. ${assignments.filter((row) => row.outcome === "parent").length} parent-owned selections. All other content stays local and unprotected. All captures are source-only: Git history is excluded. Existing source files remain in place.\n\n${assignments.filter((row) => row.path !== ".").map((row) => `${row.outcome}: ${row.path}`).join("\n")}\n\nNamespace: ${namespace}`,
  }, "Save and acknowledge local-only content");
  if (approved !== "Save and acknowledge local-only content") return "cancelled";
  const result = await save(selection, preview.approval_sha256);
  await vscode.window.showInformationMessage(`Catalog saved. Namespace ${result.namespace}; parent recovery point ${result.parent_snapshot_id}. Source files remain in place.`);
  return result;
}

async function enterChild(vscode, { root, marker, enter, open }) {
  if (!marker?.snapshot_id || !marker?.dimension_id) throw new Error("Open a restored catalog parent first.");
  const namespace = await vscode.window.showInputBox({ title: "Composition namespace from the saved receipt", validateInput: (value) => /^[a-f0-9]{32}$/.test(value) ? undefined : "Use the exact 32-character namespace." });
  if (!namespace) return "cancelled";
  const child = await vscode.window.showInputBox({ title: "Enter whole child Room — exact relative path", prompt: "The destination must be absent. Existing contents are never replaced or unloaded." });
  if (!child) return "cancelled";
  const target = path.resolve(root, child);
  relativeSource(root, target);
  const result = await enter({ namespace, child, marker });
  if (!result?.ok || !result.marker_verified) throw new Error("Child entry was not verified. Preserve the destination.");
  await vscode.window.showInformationMessage(`Child Room verified at exact recovery point ${result.snapshot_id}.`);
  await open(target);
  return result;
}

async function updateRoom(vscode, { marker, kind, pending, remember, plan, save }) {
  if (!marker?.composition_namespace || !marker?.snapshot_id) throw new Error("Open a verified namespace Room first.");
  let ticket = pending;
  if (ticket && (ticket.namespace !== marker.composition_namespace || ticket.room !== marker.project_id
    || ![ticket.baseline, ticket.output].includes(marker.snapshot_id))) {
    throw new Error("A pending composition operation has a different binding. Preserve its receipt for reconciliation.");
  }
  if (!ticket) {
    ticket = { namespace: marker.composition_namespace, room: marker.project_id, baseline: marker.snapshot_id,
      operationId: crypto.randomUUID().replaceAll("-", "") };
    if (kind === "parent") {
      const child = await vscode.window.showInputBox({ title: "Refresh parent — exact child relative path" });
      if (!child) return "cancelled";
      const pin = await vscode.window.showInputBox({ title: "Exact saved child recovery point", prompt: "Use the immutable logical snapshot ID, never latest." });
      if (!pin) return "cancelled";
      ticket = { ...ticket, child, pin };
    }
  }
  const preview = await plan(ticket);
  if (ticket.approval && ticket.approval !== preview.approval_sha256) throw new Error("Pending approval changed. Preserve the operation receipt; do not start a blind retry.");
  const action = kind === "child" ? "Save child Room" : "Publish refreshed parent pin";
  const approved = await vscode.window.showWarningMessage(`${action}?`, { modal: true,
    detail: `${kind === "child" ? "Source-only child capture from disk. Editor buffers are not captured. Parent pins remain unchanged." : "Only the explicitly selected child reference changes. Existing parent versions and working folders remain unchanged."}\nBaseline: ${ticket.baseline}\n${ticket.child ? `Child: ${ticket.child}\nSelected pin: ${ticket.pin}\n` : ""}Operation: ${ticket.operationId}\n${preview.changed ? "A new immutable recovery point will be published." : "Content is unchanged; no new recovery point will be published."}` }, action);
  if (approved !== action) return "cancelled";
  ticket = { ...ticket, approval: preview.approval_sha256, output: preview.snapshot_id };
  await remember(ticket);
  const result = await save(ticket);
  if (!result?.ok || (kind === "child" && !result.marker_verified)) throw new Error("Composition update was not verified. Retain the pending operation for retry.");
  await remember(undefined);
  await vscode.window.showInformationMessage(`${kind === "child" ? "Child saved; parent pins unchanged" : "Parent pin published; working folders unchanged"}. Exact recovery point ${result.snapshot_id}.`);
  return result;
}

module.exports = { saveCatalog, enterChild, relativeSource, updateRoom };
