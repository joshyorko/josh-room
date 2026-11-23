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
  if (mode === "full-repository") return "Files and Git history";
  if (mode === "source-only") return "Files only (Git history excluded)";
  throw new Error("The controller did not provide a supported child capture mode.");
}

function describeComponents(evidence, requested) {
  const reviewed = evidence?.component_plan?.selection?.components;
  if (!reviewed && !requested) return "Workspace only";
  if (!reviewed || Object.entries(requested?.component_selection.components || {}).some(([key, value]) => reviewed[key] !== value)) {
    throw new Error("The controller could not corroborate the selected components.");
  }
  const names = { rcc_environment: "RCC environment", homebrew_recovery: "Homebrew recovery", hauler_content: "OCI / Hauler content" };
  const choices = Object.entries(reviewed).filter(([, mode]) => mode !== "omit")
    .map(([name, mode]) => `${names[name]} (${mode === "refresh" ? "capture" : "keep if saved"})`);
  return choices.length ? choices.join(", ") : "Workspace only";
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

async function catalogSettings(vscode, root, children, chooseComponents) {
  const rows = children.map(child => ({ path: child, outcome: "child" }));
  let parent = [];
  for (;;) {
    const action = await vscode.window.showQuickPick([
      { id: "continue", label: "Continue to destination" },
      { id: "git", label: "Change Git settings" },
      { id: "components", label: "Customize optional components" },
      { id: "parent", label: "Choose files for the catalog itself", description: parent.map(row => row.path).join(", ") },
    ], { title: `Save ${rows.length} selected folders as Rooms`, placeHolder: "Each folder uses its Git capture policy. Optional components are off by default." });
    if (!action) return undefined;
    if (action.id === "continue") return [{ path: ".", outcome: "local-only" }, ...rows, ...parent];
    if (action.id === "parent") {
      const owned = await vscode.window.showOpenDialog({ title: "Include in parent", defaultUri: vscode.Uri.file(root),
        canSelectFolders: true, canSelectFiles: true, canSelectMany: true });
      if (owned) parent = owned.map(item => ({ path: relativeSource(root, item.fsPath), outcome: "parent" }));
      continue;
    }
    const scope = rows.length === 1 ? [{ row: rows[0] }] : await vscode.window.showQuickPick(
      rows.map(row => ({ label: row.path, row, picked: true })),
      { canPickMany: true, title: "Apply settings to folders", placeHolder: "All selected by default. Uncheck folders to override a subset." });
    if (!scope?.length) continue;
    if (action.id === "git") {
      const mode = await vscode.window.showQuickPick([
        { id: "policy", label: "Use each folder's capture policy", description: "Preserve Git history unless its policy excludes it" },
        { id: "source-only", label: "Files only", description: "Exclude Git history" },
      ], { title: `Git capture for ${scope.length} folders` });
      if (!mode) continue;
      for (const { row } of scope) {
        if (mode.id === "policy") delete row.capture_mode;
        else row.capture_mode = "source-only";
      }
    } else if (action.id === "components") {
      const options = await chooseComponents(scope.map(({ row }) => path.join(root, row.path)));
      if (options === undefined) continue;
      for (const { row } of scope) {
        if (options) row.component_options = options;
        else delete row.component_options;
      }
    }
  }
}

async function saveCatalog(vscode, { dimensions, selectChildren, chooseComponents, plan, save }) {
  const source = await vscode.window.showOpenDialog({ title: "Save Catalog Room — source", canSelectFolders: true, canSelectFiles: false, canSelectMany: false });
  if (!source?.length) return "cancelled";
  const root = source[0].fsPath;
  const children = await selectChildren(root);
  if (!children?.length) return "cancelled";
  const assignments = await catalogSettings(vscode, root, children, chooseComponents);
  if (!assignments) return "cancelled";
  const configured = await dimensions(root);
  const choice = await vscode.window.showQuickPick(configured.map((dimension) => ({ label: dimension.display_name || dimension.id, dimension })), { title: "Where should this catalog be saved?" });
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
  const approved = await vscode.window.showWarningMessage("Save this catalog?", {
    modal: true,
    detail: `Destination: ${destination}\n${children.length} folders will be saved as independent Rooms. Other files stay on this device and are unprotected.\n\n${[...childSummary, ...parentSummary].join("\n")}`,
  }, "Save catalog");
  if (approved !== "Save catalog") return "cancelled";
  const result = await save(selection, preview.approval_sha256);
  const copy = "Copy recovery receipt";
  if (await vscode.window.showInformationMessage(`Catalog saved with ${children.length} child Room${children.length === 1 ? "" : "s"}.`, copy) === copy) {
    await vscode.env.clipboard.writeText(JSON.stringify({
      dimension_id: preview.request.destination_binding.dimension_id,
      namespace: result.namespace,
      parent_room_id: result.parent_room_id,
      parent_snapshot_id: result.parent_snapshot_id,
    }, null, 2));
  }
  return result;
}

async function chooseChild(vscode, marker, listChildren, refresh) {
  if (!marker?.composition_namespace || !marker?.project_id || !marker?.snapshot_id || !marker?.dimension_id) {
    throw new Error("Open a restored Catalog parent first.");
  }
  const choices = [];
  let revision, offset = 0;
  do {
    const page = await listChildren(marker, { revision, offset });
    checkPickerPage(page, marker, revision, offset, refresh);
    if (!Array.isArray(page.children) || page.children.length > 32 || offset + page.children.length > 32) {
      throw new Error("Invalid Catalog child inventory.");
    }
    revision = page.revision;
    for (const row of page.children) {
      if (!refresh || row.other_point_count > 0) choices.push({ label: row.path, description: `Pinned to ${row.snapshot_id}`, row });
    }
    offset = page.next_offset;
  } while (offset !== null);
  if (!choices.length) {
    await vscode.window.showInformationMessage(refresh ? "All child Rooms are pinned to their only saved recovery point." : "This parent has no child Rooms.");
    return undefined;
  }
  const selected = await vscode.window.showQuickPick(choices, { title: refresh ? "Refresh child pin" : "Enter child Room" });
  return selected && { ...selected.row, revision };
}

function checkPickerPage(page, marker, revision, offset, refresh) {
  if (!page?.ok || page.namespace !== marker.composition_namespace || page.parent_room_id !== marker.project_id
      || page.parent_snapshot_id !== marker.snapshot_id || !Number.isSafeInteger(page.revision)
      || (revision !== undefined && revision !== page.revision)
      || (page.next_offset !== null && (!Number.isSafeInteger(page.next_offset) || page.next_offset <= offset))) {
    throw new Error("The verified parent changed. Reopen its recovery point.");
  }
  if (refresh && !page.current_parent) throw new Error("Enter the current parent recovery point before refreshing its pins.");
}

async function chooseRecoveryPoint(vscode, marker, child, listChildren) {
  let offset = 0;
  for (;;) {
    const page = await listChildren(marker, { revision: child.revision, offset, childPath: child.path });
    checkPickerPage(page, marker, child.revision, offset, true);
    if (page.child_path !== child.path || page.child_room_id !== child.room_id || page.pinned_snapshot_id !== child.snapshot_id
        || !Array.isArray(page.recovery_points)) throw new Error("The selected child changed. Reopen its recovery points.");
    const choices = page.recovery_points.map(point => ({ label: point.created_at, description: point.snapshot_id, point }));
    if (page.next_offset !== null) choices.push({ label: "More recovery points…", more: true });
    const selected = await vscode.window.showQuickPick(choices, {
      title: `Choose a saved recovery point for ${child.path}`, matchOnDescription: true });
    if (!selected?.more) return selected?.point;
    offset = page.next_offset;
  }
}

async function enterChild(vscode, { root, marker, listChildren, enter, open }) {
  const row = await chooseChild(vscode, marker, listChildren, false);
  if (!row) return "cancelled";
  const target = path.resolve(root, row.path);
  relativeSource(root, target);
  const result = await enter({ namespace: marker.composition_namespace, child: row.path, marker });
  if (!result?.ok || !result.marker_verified || result.namespace !== marker.composition_namespace
      || result.room_id !== row.room_id || result.snapshot_id !== row.snapshot_id) {
    throw new Error("Child entry did not verify the selected recovery point. Preserve the destination.");
  }
  await vscode.window.showInformationMessage(`Child Room verified at exact recovery point ${result.snapshot_id}.`);
  await open(target);
  return result;
}

function savedComponents(components) {
  if (!components) return "Component details unavailable";
  const names = Array.isArray(components) ? components : Object.entries(components || {}).filter(([, ref]) => ref != null).map(([name]) => name);
  return names.length ? names.join(", ") : "Workspace only";
}

async function updateRoom(vscode, { marker, kind, pending, remember, chooseComponents, listChildren, plan, save }) {
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
      const child = await chooseChild(vscode, marker, listChildren, true);
      if (!child) return "cancelled";
      const selected = await chooseRecoveryPoint(vscode, marker, child, listChildren);
      if (!selected) return "cancelled";
      ticket = { ...ticket, child: child.path, pin: selected.snapshot_id };
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
