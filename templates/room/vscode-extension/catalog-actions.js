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
  const provider = { minio: "MinIO", s3: "S3", r2: "Cloudflare R2" }[binding.provider] || binding.provider;
  return {
    label: dimension.display_name || id,
    description: provider,
    detail: physical.slice(binding.provider.length + 1),
    connection: binding.connection_id,
    dimension: binding.dimension_id,
  };
}

function reviewCatalog(vscode, name, destination, children) {
  const heading = label => ({ label, kind: vscode.QuickPickItemKind.Separator });
  const parent = {
    label: "$(root-folder) Catalog parent",
    description: "Eligible root files and child Room links",
    detail: "Checked folders include nested content. Unchecked folders stay local.",
    alwaysShow: true,
  };
  const picker = vscode.window.createQuickPick();
  picker.title = `Review Catalog · ${name} → ${destination.label} · ${children.length} child ${children.length === 1 ? "Room" : "Rooms"}`;
  picker.placeholder = "Review the plan, then choose Save catalog, Advanced settings, or Cancel";
  picker.matchOnDescription = true;
  picker.matchOnDetail = true;
  picker.ignoreFocusOut = true;
  picker.items = [
    heading("Destination"), { label: destination.label, description: destination.description, detail: destination.detail },
    { label: "Connection", description: destination.connection },
    { label: "Dimension", description: destination.dimension },
    heading("Capture"), parent,
    heading("Actions"),
    { label: "$(check) Save catalog", action: "Save catalog" },
    { label: "$(settings-gear) Advanced settings", action: "Advanced settings" },
    { label: "$(close) Cancel", action: "Cancel" },
    heading("Child Rooms"), ...children,
  ];
  return new Promise(resolve => {
    let action;
    picker.onDidAccept(() => {
      const selected = picker.selectedItems[0];
      if (!selected?.action) return;
      action = selected.action;
      picker.hide();
    });
    picker.onDidHide(() => {
      picker.dispose();
      resolve(action);
    });
    picker.show();
    picker.activeItems = [parent];
  });
}

async function catalogSettings(vscode, root, assignments, chooseComponents) {
  const rows = assignments.map(row => ({ ...row }));
  for (;;) {
    const action = await vscode.window.showQuickPick([
      { id: "review", label: "Return to review" },
      { id: "git", label: "Change Git settings" },
      { id: "components", label: "Customize optional components" },
    ], { title: "Catalog child settings", placeHolder: "Each checked folder is a whole child Room. Optional components are off by default." });
    if (!action) return undefined;
    if (action.id === "review") return rows;
    if (!rows.length) continue;
    const scope = rows.length === 1 ? [{ row: rows[0] }] : await vscode.window.showQuickPick(
      rows.map(row => ({ label: row.path, row, picked: true })),
      { canPickMany: true, title: "Apply settings to folders", placeHolder: "Choose which child folders receive this setting." });
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
  const validateName = value => !value.trim() || [...value.trim()].length > 100 || /[\p{Cc}\p{Zl}\p{Zp}]/u.test(value)
    ? "Enter a Catalog name on one line, up to 100 characters." : undefined;
  const name = await vscode.window.showInputBox({ title: "Name the Catalog Room",
    value: path.basename(root) || "Catalog Room", validateInput: validateName });
  if (name === undefined) return "cancelled";
  if (validateName(name)) throw new Error(validateName(name));
  let assignments = children.map(path => ({ path, outcome: "child" }));
  const configured = await dimensions(root);
  const choice = await vscode.window.showQuickPick(configured.map((dimension) => ({ label: dimension.display_name || dimension.id, dimension })), { title: "Where should this catalog be saved?" });
  if (!choice) return "cancelled";
  const namespace = crypto.randomUUID().replaceAll("-", "");
  const parentRoom = `catalog-${namespace}`;
  let selection = { root, assignments, namespace, parentRoom, name: name.trim(), dimension: choice.dimension };
  for (;;) {
    let preview;
    try {
      preview = await plan(selection);
    } catch (error) {
      const failure = error.result;
      const filesOnly = failure?.error_code === "composition-child-git-files-only";
      if (!filesOnly && failure?.error_code !== "composition-child-room-metadata") throw error;
      const child = assignments.find(row => crypto.createHash("sha256").update(row.path).digest("hex") === failure.child_path_sha256);
      if (!child || (filesOnly && child.capture_mode === "source-only")) throw error;
      const action = filesOnly ? "Review files only" : "Leave folder local";
      const detail = filesOnly
        ? "This folder's Git history cannot be captured independently. Review a new plan that includes its files without Git history?"
        : "This folder contains reserved Room metadata. Review a new plan that leaves this folder on this device?";
      if (await vscode.window.showWarningMessage(child.path, { modal: true, detail }, action) !== action) return "cancelled";
      assignments = filesOnly
        ? assignments.map(row => row === child ? { ...row, capture_mode: "source-only" } : row)
        : assignments.filter(row => row !== child);
      if (!assignments.length) return "cancelled";
      selection = { ...selection, assignments };
      continue;
    }
    if (preview === undefined) return "cancelled";
    const destination = reviewedDestination(choice.dimension, preview.request?.destination_binding);
    const evidenceByChild = preview.request.child_evidence;
    if (Object.keys(evidenceByChild || {}).length !== assignments.length) {
      throw new Error("The controller returned different Catalog child folders. Review the selection again.");
    }
    const childSummary = assignments.map(row => {
      const evidence = Object.hasOwn(evidenceByChild, row.path) ? evidenceByChild[row.path] : undefined;
      const mode = describeMode(evidence?.capture_mode);
      const git = mode === "Files and Git history" ? "Files + Git if present" : "Files only";
      const components = describeComponents(evidence, row.component_options);
      return {
        label: `$(folder) ${row.path.replaceAll("$(", "\\$(")}`,
        description: components === "Workspace only" ? `${git}  ·  Workspace only` : git,
        ...(components === "Workspace only" ? {} : { detail: components }),
      };
    });
    const approved = await reviewCatalog(vscode, selection.name, destination, childSummary);
    if (approved === "Advanced settings") {
      const edited = await catalogSettings(vscode, root, assignments, chooseComponents);
      if (!edited) return "cancelled";
      assignments = edited;
      selection = { ...selection, assignments };
      continue;
    }
    if (approved !== "Save catalog") return "cancelled";
    const result = await save(selection, preview.approval_sha256);
    const copy = "Copy recovery receipt";
    if (await vscode.window.showInformationMessage(`Catalog saved with ${assignments.length} child Room${assignments.length === 1 ? "" : "s"}.`, copy) === copy) {
      await vscode.env.clipboard.writeText(JSON.stringify({
        dimension_id: preview.request.destination_binding.dimension_id,
        namespace: result.namespace,
        parent_room_id: result.parent_room_id,
        parent_snapshot_id: result.parent_snapshot_id,
      }, null, 2));
    }
    return result;
  }
}

async function pinnedChildChoices(marker, listChildren, refresh) {
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
  return { choices, revision };
}

async function chooseChild(vscode, marker, listChildren, refresh, childPath) {
  const { choices, revision } = await pinnedChildChoices(marker, listChildren, refresh);
  if (childPath !== undefined) {
    const pinned = choices.find(choice => choice.row.path === childPath);
    if (!pinned) throw new Error("The selected child is unavailable at this Catalog recovery point.");
    return { ...pinned.row, revision };
  }
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

async function hydratePinnedChild(root, marker, row, enter) {
  const target = path.resolve(root, row.path);
  relativeSource(root, target);
  const result = await enter({ namespace: marker.composition_namespace, child: row.path, marker });
  if (!result?.ok || !result.marker_verified || result.namespace !== marker.composition_namespace
      || result.room_id !== row.room_id || result.snapshot_id !== row.snapshot_id) {
    throw new Error("Child entry did not verify the selected recovery point. Preserve the destination.");
  }
  return { result, target };
}

async function enterChild(vscode, { root, marker, listChildren, enter, open, childPath }) {
  const row = await chooseChild(vscode, marker, listChildren, false, childPath);
  if (!row) return "cancelled";
  const { result, target } = await hydratePinnedChild(root, marker, row, enter);
  await vscode.window.showInformationMessage(`Child Room verified at exact recovery point ${result.snapshot_id}.`);
  await open(target);
  return result;
}

async function hydrateChildren(vscode, { root, marker, listChildren, enter }) {
  const { choices } = await pinnedChildChoices(marker, listChildren, false);
  const selected = await vscode.window.showQuickPick(choices, { canPickMany: true,
    title: "Restore Catalog child Rooms", placeHolder: "Check the child Rooms to restore, or use Select All." });
  if (!selected?.length) return "cancelled";
  const restored = [];
  for (const choice of selected) {
    const pinned = choices.find(candidate => candidate.label === choice.label);
    if (!pinned) throw new Error("A selected child is unavailable at this Catalog recovery point.");
    try {
      const { result } = await hydratePinnedChild(root, marker, pinned.row, enter);
      restored.push(result);
    } catch (error) {
      throw new Error(`Restored ${restored.length} of ${selected.length} child Rooms. ${pinned.label}: ${error.message}`);
    }
  }
  void vscode.window.showInformationMessage(`Restored ${restored.length} child ${restored.length === 1 ? "Room" : "Rooms"}.`);
  return { ok: true, restored };
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

module.exports = { saveCatalog, enterChild, hydrateChildren, relativeSource, updateRoom };
