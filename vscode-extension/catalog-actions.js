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

function describeMode(mode) {
  if (mode === "full-repository") return "full-repository (preserves supported Git storage)";
  if (mode === "source-only") return "source-only (excludes Git history)";
  throw new Error("The controller did not provide a supported child capture mode.");
}

function describeComponents(evidence, requested) {
  const reviewed = evidence?.component_plan?.selection?.components;
  if (!reviewed && !requested) return "Workspace only";
  if (!reviewed || Object.entries(requested?.component_selection.components || {}).some(([key, value]) => reviewed[key] !== value)) {
    throw new Error("The controller could not corroborate the selected components.");
  }
  const modes = Object.entries(reviewed).map(([name, mode]) => `${name}: ${mode}`).join(", ");
  const digest = evidence.component_plan.artifacts_sha256;
  return digest ? `${modes} · reviewed artifacts ${digest}` : modes;
}

function reviewedDestination(dimension, binding) {
  const id = dimension.id || dimension.dimension_id;
  const physical = binding?.physical_bucket;
  if (!binding || binding.dimension_id !== id || binding.provider !== dimension.provider
      || !binding.connection_id || !binding.encryption_domain_id || typeof physical !== "string"
      || !physical.startsWith(`${binding.provider}:`) || !physical.slice(physical.lastIndexOf(":") + 1)
      || (dimension.connection_id && binding.connection_id !== dimension.connection_id)
      || (dimension.encryption_domain_id && binding.encryption_domain_id !== dimension.encryption_domain_id)
      || (dimension.physical_bucket && physical !== dimension.physical_bucket)
      || (dimension.bucket && !physical.endsWith(`:${dimension.bucket}`))) {
    throw new Error("The reviewed Catalog destination changed. Select the Dimension again.");
  }
  if (dimension.endpoint) {
    const endpoint = new URL(dimension.endpoint);
    // Composition accepts root endpoints. The controller's physical identity
    // uses the hostname without IPv6 brackets and omits the default port.
    const host = endpoint.hostname.replace(/^\[|\]$/g, "");
    const authority = host + (endpoint.port ? ":" + endpoint.port : "");
    const prefix = `${binding.provider}:${endpoint.protocol}//${authority}:`;
    if (endpoint.pathname !== "/" || endpoint.username || endpoint.password || endpoint.search || endpoint.hash
        || physical.slice(0, physical.lastIndexOf(":") + 1) !== prefix) {
      throw new Error("The reviewed Catalog destination endpoint changed. Select the Dimension again.");
    }
  }
  return `${dimension.display_name || id} (${binding.dimension_id}) · ${binding.provider} · ${binding.connection_id} · ${physical}`;
}

async function saveCatalog(vscode, { dimensions, chooseComponents, plan, save }) {
  const source = await vscode.window.showOpenDialog({ title: "Save Catalog Room — source", canSelectFolders: true, canSelectFiles: false, canSelectMany: false });
  if (!source?.length) return "cancelled";
  const root = source[0].fsPath;
  const children = await vscode.window.showOpenDialog({ title: "Capture as independent child Rooms", defaultUri: source[0], canSelectFolders: true, canSelectFiles: false, canSelectMany: true });
  if (!children?.length) return "cancelled";
  const assignments = [{ path: ".", outcome: "local-only" }, ...children.map((item) => ({ path: relativeSource(root, item.fsPath), outcome: "child" }))];
  for (const child of assignments.filter(row => row.outcome === "child")) {
    const mode = await vscode.window.showQuickPick([
      { id: "policy", label: "Use workspace policy", description: "Defaults to full repository" },
      { id: "source-only", label: "Source only", description: "Exclude this child's Git history" },
    ], { title: `Capture mode for ${child.path}` });
    if (!mode) return "cancelled";
    if (mode.id === "source-only") child.capture_mode = mode.id;
    if (chooseComponents) {
      const options = await chooseComponents(path.join(root, child.path), child.path, false);
      if (options === undefined) return "cancelled";
      if (options) child.component_options = options;
    }
  }
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
  const namespace = crypto.randomUUID().replaceAll("-", "");
  const parentRoom = `catalog-${namespace}`;
  const selection = { root, assignments, namespace, parentRoom, dimension: choice.dimension };
  const preview = await plan(selection);
  if (preview === undefined) return "cancelled";
  const destination = reviewedDestination(choice.dimension, preview.request?.destination_binding);
  const childSummary = assignments.filter(row => row.outcome === "child").map(row => {
    const evidence = preview.request?.child_evidence?.[row.path];
    return `${row.path} — ${describeMode(evidence?.capture_mode)}. ${describeComponents(evidence, row.component_options)}`;
  });
  const parentSummary = assignments.filter(row => row.outcome === "parent").map(row => `Parent content: ${row.path}`);
  const approved = await vscode.window.showWarningMessage("Save this Catalog Room and its children?", {
    modal: true,
    detail: `Destination: ${destination}\n${children.length} independent child Rooms. ${assignments.filter((row) => row.outcome === "parent").length} parent-owned selections. All other content stays local and unprotected. Existing source files remain in place.\n\n${[...childSummary, ...parentSummary].join("\n")}\n\nNamespace: ${namespace}`,
  }, "Save and acknowledge local-only content");
  if (approved !== "Save and acknowledge local-only content") return "cancelled";
  const result = await save(selection, preview.approval_sha256);
  const savedChildren = result.children?.map(row => `${row.path} — ${row.snapshot_id} · ${row.capture_mode} · ${savedComponents(row.components)}`);
  await vscode.window.showInformationMessage(`Catalog saved. Namespace ${result.namespace}; parent recovery point ${result.parent_snapshot_id}. Source files remain in place.\n${(savedChildren || childSummary).join("\n")}`);
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

function savedComponents(components) {
  if (!components) return "Component details unavailable";
  const names = Array.isArray(components) ? components : Object.entries(components || {}).filter(([, ref]) => ref != null).map(([name]) => name);
  return names.length ? names.join(", ") : "Workspace only";
}

async function updateRoom(vscode, { marker, kind, pending, remember, chooseComponents, plan, save }) {
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
  // Re-select explicit external inputs on retry. Only the approval and operation
  // binding persist in workspace state; the controller verifies the same inputs.
  if (kind === "child" && chooseComponents && (!pending || ticket.customComponents)) {
    const options = await chooseComponents();
    if (options === undefined) return "cancelled";
    if (options) ticket = { ...ticket, customComponents: true, component_options: options };
  }
  const preview = await plan(ticket);
  if (ticket.approval && ticket.approval !== preview.approval_sha256) throw new Error("Pending approval changed. Preserve the operation receipt; do not start a blind retry.");
  const action = kind === "child" ? "Save child Room" : "Publish refreshed parent pin";
  const approved = await vscode.window.showWarningMessage(`${action}?`, { modal: true,
    detail: `${kind === "child" ? `Child capture from disk: ${describeMode(preview.request?.plan?.capture_mode)}. ${describeComponents(preview.request?.plan, ticket.component_options)}. Editor buffers are not captured. Parent pins remain unchanged.` : "Only the explicitly selected child reference changes. Existing parent versions and working folders remain unchanged."}\nBaseline: ${ticket.baseline}\n${ticket.child ? `Child: ${ticket.child}\nSelected pin: ${ticket.pin}\n` : ""}Operation: ${ticket.operationId}\n${preview.changed ? "A new immutable recovery point will be published." : "Content is unchanged; no new recovery point will be published."}` }, action);
  if (approved !== action) return "cancelled";
  ticket = { ...ticket, approval: preview.approval_sha256, output: preview.snapshot_id };
  const { component_options, ...retryTicket } = ticket;
  await remember(retryTicket);
  const result = await save(ticket);
  if (!result?.ok || (kind === "child" && !result.marker_verified)) throw new Error("Composition update was not verified. Retain the pending operation for retry.");
  await remember(undefined);
  await vscode.window.showInformationMessage(`${kind === "child" ? "Child saved; parent pins unchanged" : "Parent pin published; working folders unchanged"}. Exact recovery point ${result.snapshot_id}.${kind === "child" ? ` ${savedComponents(result.components)}.` : ""}`);
  return result;
}

module.exports = { saveCatalog, enterChild, relativeSource, updateRoom };
