"use strict";
const path = require("path");
const crypto = require("crypto");

const SPLIT_CHILDREN = "Split folders into Rooms…";

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

function reviewCatalog(vscode, name, destination, children, preservation) {
  const heading = label => ({ label, kind: vscode.QuickPickItemKind.Separator });
  const parent = {
    label: "$(root-folder) Catalog parent",
    description: preservation ? "Eligible files and folders" : "Eligible root files and child Room links",
    detail: preservation ? preservation.detail : "Checked folders include nested content. Unchecked folders stay local.",
    alwaysShow: true,
  };
  const picker = vscode.window.createQuickPick();
  picker.title = `Review Catalog · ${name} → ${destination.label} · ${children.length} child ${children.length === 1 ? "Room" : "Rooms"}`;
  picker.placeholder = "Review what will be saved";
  picker.matchOnDescription = true;
  picker.matchOnDetail = true;
  picker.ignoreFocusOut = true;
  picker.items = [
    heading("Destination"), { label: destination.label, description: destination.description, detail: destination.detail },
    { label: "Connection", description: destination.connection },
    { label: "Dimension", description: destination.dimension },
    heading("Capture"), parent,
    ...(preservation?.localOnly.length ? [
      heading("Stays on this device"),
      ...preservation.localOnly.map(relative => ({ label: relative.replaceAll("$(", "\\$("), description: "Left local by request" })),
    ] : []),
    heading("Actions"),
    { label: "$(check) Save catalog", action: "Save catalog" },
    ...(preservation ? [{ label: `$(split-horizontal) ${SPLIT_CHILDREN}`, action: SPLIT_CHILDREN }] : []),
    ...(children.length ? [{ label: "$(settings-gear) Advanced settings", action: "Advanced settings" }] : []),
    { label: "$(close) Cancel", action: "Cancel" },
    ...(children.length ? [heading("Child Rooms"), ...children] : []),
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

async function saveCatalog(vscode, { dimensions, selectChildren, chooseComponents, plan, save, preserveByDefault = false, onSaved }) {
  const source = await vscode.window.showOpenDialog({ title: "Save Catalog Room — source", canSelectFolders: true, canSelectFiles: false, canSelectMany: false });
  if (!source?.length) return "cancelled";
  const root = source[0].fsPath;
  const children = preserveByDefault ? [] : await selectChildren(root);
  if (!children || (!preserveByDefault && !children.length)) return "cancelled";
  const validateName = value => !value.trim() || [...value.trim()].length > 100 || /[\p{Cc}\p{Zl}\p{Zp}]/u.test(value)
    ? "Enter a Catalog name on one line, up to 100 characters." : undefined;
  const name = await vscode.window.showInputBox({ title: "Name the Catalog Room",
    value: path.basename(root) || "Catalog Room", validateInput: validateName });
  if (name === undefined) return "cancelled";
  if (validateName(name)) throw new Error(validateName(name));
  let assignments = children.map(path => ({ path, outcome: "child" }));
  const editSplits = async () => {
    const selected = await selectChildren(root, assignments.filter(row => row.outcome === "child").map(row => row.path));
    if (selected === undefined) return;
    const old = new Map(assignments.filter(row => row.outcome === "child").map(row => [row.path, row]));
    assignments = [
      ...selected.map(path => old.get(path) || { path, outcome: "child" }),
      ...assignments.filter(row => row.outcome === "local-only" && !selected.includes(row.path)),
    ];
  };
  const configured = await dimensions(root);
  const choice = await vscode.window.showQuickPick(configured.map((dimension) => ({ label: dimension.display_name || dimension.id, dimension })), { title: "Where should this catalog be saved?" });
  if (!choice) return "cancelled";
  const namespace = crypto.randomUUID().replaceAll("-", "");
  const parentRoom = `catalog-${namespace}`;
  const sourceSelection = { root, namespace, parentRoom, name: name.trim(), dimension: choice.dimension };
  for (;;) {
    const selection = { ...sourceSelection, assignments };
    let preview;
    try {
      preview = await plan(selection);
    } catch (error) {
      const failure = error.result;
      if (preserveByDefault && ["composition-preservation-git", "composition-preservation-boundary"].includes(failure?.error_code)) {
        const retry = "Check again";
        const decision = await vscode.window.showWarningMessage("Catalog needs attention", {
          modal: true, detail: error.message,
        }, SPLIT_CHILDREN, retry);
        if (decision === SPLIT_CHILDREN) await editSplits();
        else if (decision !== retry) return "cancelled";
        continue;
      }
      const filesOnly = failure?.error_code === "composition-child-git-files-only";
      if (!filesOnly && failure?.error_code !== "composition-child-room-metadata") throw error;
      const child = assignments.find(row => row.outcome === "child"
        && crypto.createHash("sha256").update(row.path).digest("hex") === failure.child_path_sha256);
      if (!child || (filesOnly && child.capture_mode === "source-only")) throw error;
      const action = filesOnly ? "Review files only" : "Leave folder local";
      const detail = filesOnly
        ? "This folder's Git history cannot be captured independently. Review a new plan that includes its files without Git history?"
        : "This folder contains reserved Room metadata. Review a new plan that leaves this folder on this device?";
      if (await vscode.window.showWarningMessage(child.path, { modal: true, detail }, action) !== action) return "cancelled";
      assignments = filesOnly
        ? assignments.map(row => row === child ? { ...row, capture_mode: "source-only" } : row)
        : preserveByDefault
          ? assignments.map(row => row === child ? { path: row.path, outcome: "local-only" } : row)
          : assignments.filter(row => row !== child);
      if (!preserveByDefault && !assignments.length) return "cancelled";
      continue;
    }
    if (preview === undefined) return "cancelled";
    const destination = reviewedDestination(choice.dimension, preview.request?.destination_binding);
    const childAssignments = assignments.filter(row => row.outcome === "child");
    const evidenceByChild = preview.request.child_evidence;
    if (Object.keys(evidenceByChild || {}).length !== childAssignments.length) {
      throw new Error("The controller returned different Catalog child folders. Review the selection again.");
    }
    const childSummary = childAssignments.map(row => {
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
    let preservation;
    if (preserveByDefault) {
      const expected = new Map([[".", "parent"], ...assignments.map(row => [row.path, row.outcome])]);
      const reviewed = preview.request.assignments;
      if (preview.request.preserve_content !== true || !Array.isArray(reviewed) || reviewed.length !== expected.size
          || !reviewed.every(row => row && expected.get(row.path) === row.outcome && expected.delete(row.path))) {
        throw new Error("The controller did not verify the whole-folder capture plan.");
      }
      const mode = describeMode(preview.request.capture_mode);
      const count = preview.request.parent_git_count ?? preview.parent_git_count;
      const git = mode === "Files and Git history"
        ? Number.isSafeInteger(count) && count >= 0
          ? `${count} Git ${count === 1 ? "repository" : "repositories"} in the Catalog parent.`
          : "Eligible Git repositories stay with their folders."
        : "The source policy excludes Git history.";
      preservation = {
        detail: `${git} Capture policy exclusions stay local.`,
        localOnly: assignments.filter(row => row.outcome === "local-only").map(row => row.path),
      };
    }
    const approved = await reviewCatalog(vscode, selection.name, destination, childSummary, preservation);
    if (approved === SPLIT_CHILDREN) {
      await editSplits();
      continue;
    }
    if (approved === "Advanced settings") {
      const edited = await catalogSettings(vscode, root, childAssignments, chooseComponents);
      if (!edited) return "cancelled";
      assignments = [...edited, ...assignments.filter(row => row.outcome === "local-only")];
      continue;
    }
    if (approved !== "Save catalog") return "cancelled";
    const result = await save(selection, preview.approval_sha256);
    if (onSaved && (result?.ok !== true || result.namespace !== selection.namespace
        || result.parent_room_id !== selection.parentRoom || typeof result.parent_snapshot_id !== "string"
        || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(result.parent_snapshot_id)
        || result.parent_snapshot_id.toLowerCase() === "latest")) {
      throw new Error("The controller could not verify the saved Catalog recovery point.");
    }
    const receipt = {
      dimension_id: preview.request.destination_binding.dimension_id,
      namespace: result.namespace,
      parent_room_id: result.parent_room_id,
      parent_snapshot_id: result.parent_snapshot_id,
    };
    if (onSaved) {
      await onSaved(receipt);
      return result;
    }
    const copy = "Copy recovery receipt";
    if (await vscode.window.showInformationMessage(`Catalog saved with ${childAssignments.length} child Room${childAssignments.length === 1 ? "" : "s"}.`, copy) === copy) {
      await vscode.env.clipboard.writeText(JSON.stringify(receipt, null, 2));
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
  if (!matchesPinnedChild(marker, row, result)) {
    throw new Error("Child entry did not verify the selected recovery point. Preserve the destination.");
  }
  return { result, target };
}

function matchesPinnedChild(marker, row, result) {
  return result?.ok && result.marker_verified && result.namespace === marker.composition_namespace
    && result.room_id === row.room_id && result.snapshot_id === row.snapshot_id;
}

async function enterChild(vscode, { root, marker, listChildren, enter, open, childPath, notify = true, localState = () => "missing" }) {
  const row = await chooseChild(vscode, marker, listChildren, false, childPath);
  if (!row) return "cancelled";
  const state = localState(row);
  if (state === "restored") {
    const target = path.resolve(root, row.path);
    relativeSource(root, target);
    await open(target);
    return "opened";
  }
  if (state === "occupied") throw new Error("The child folder is occupied. Choose a new Catalog restore location.");
  const { result, target } = await hydratePinnedChild(root, marker, row, enter);
  if (notify || result.local_changes) {
    void vscode.window.showInformationMessage(result.local_changes
      ? "Child Room restored. Local files changed after restore."
      : `Child Room verified at exact recovery point ${result.snapshot_id}.`);
  }
  await open(target);
  return result;
}

async function hydrateChildren(vscode, { root, marker, listChildren, enterBatch, childPaths, notify = true, localState = () => "missing" }) {
  const { choices } = await pinnedChildChoices(marker, listChildren, false);
  const missing = choices.filter(choice => localState(choice.row) === "missing");
  if (childPaths === undefined && !missing.length) {
    void vscode.window.showInformationMessage("No child Rooms are available to restore here. Check Open Workspace for folder status.");
    return "cancelled";
  }
  const selected = childPaths ?? (await vscode.window.showQuickPick(missing, { canPickMany: true,
    title: "Restore Catalog child Rooms", placeHolder: "Check the missing child Rooms to restore, or use Select All." }))?.map(choice => choice.label);
  if (!selected?.length) return "cancelled";
  const pinned = [...new Set(selected)].map(label => missing.find(choice => choice.label === label));
  if (pinned.some(choice => !choice)) throw new Error("A selected child is unavailable at this Catalog recovery point.");
  const children = pinned.map(choice => choice.row);
  for (const row of children) relativeSource(root, path.resolve(root, row.path));
  const batch = await enterBatch(children.map(row => row.path));
  const restored = batch?.restored;
  if (!batch?.ok || batch.namespace !== marker.composition_namespace || batch.parent_snapshot_id !== marker.snapshot_id
      || !Array.isArray(restored) || restored.length > children.length
      || !restored.every((result, index) => matchesPinnedChild(marker, children[index], result))) {
    throw new Error("The controller could not verify the batch restore. Check Open Workspace for folder status.");
  }
  if (batch.status === "partial" && batch.failed_index === restored.length && restored.length < children.length) {
    const reason = {
      "child-destination-present": "The folder already exists. Open it to check its contents.",
      "child-insufficient-space": "There is insufficient disk space.",
      "parent-binding-changed": "The Catalog binding or capture policy changed.",
      "child-validation-failed": "The controller could not verify the child payload.",
    }[batch.failure];
    throw new Error(`Restored ${restored.length} of ${children.length} child Rooms. Restore stopped at ${children[batch.failed_index].path}.${reason ? ` ${reason}` : ""}`);
  }
  if (batch.status !== "hydrated" || restored.length !== children.length) {
    throw new Error("The controller returned an incomplete batch restore receipt. Check Open Workspace for folder status.");
  }
  if (notify) {
    const changed = restored.filter(result => result.local_changes === true).length;
    const summary = `Restored ${restored.length} child ${restored.length === 1 ? "Room" : "Rooms"}.`;
    void vscode.window.showInformationMessage(changed ? `${summary} Local files changed after restore in ${changed}.` : summary);
  }
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
  const action = kind === "catalog" ? "Save Catalog" : kind === "child" ? "Save child Room" : "Publish refreshed parent pin";
  if (kind === "catalog" && !preview.changed && !pending) {
    const result = await save({ ...ticket, approval: preview.approval_sha256 });
    if (!result?.ok || result.status !== "unchanged" || !result.marker_verified) {
      throw new Error("The saved Catalog baseline could not be verified.");
    }
    return result;
  }
  const approved = await vscode.window.showWarningMessage(`${action}?`, { modal: true,
    detail: kind === "catalog" ? `Save the Catalog files as a new recovery point.${
      preview.children_to_update?.length ? ` Update child Rooms: ${preview.children_to_update.join(", ")}.` : " Child recovery points stay as they are."}`
      : `${kind === "child" ? `Child capture from disk: ${describeMode(preview.request?.plan?.capture_mode)}. ${describeComponents(preview.request?.plan, ticket.component_options)}. Editor buffers are not captured. Parent pins remain unchanged.` : "Only the explicitly selected child reference changes. Existing parent versions and working folders remain unchanged."}\nBaseline: ${ticket.baseline}\n${ticket.child ? `Child: ${ticket.child}\nSelected pin: ${ticket.pin}\n` : ""}Operation: ${ticket.operationId}\n${preview.changed ? "A new immutable recovery point will be published." : "Content is unchanged; no new recovery point will be published."}` }, action);
  if (approved !== action) return "cancelled";
  ticket = { ...ticket, approval: preview.approval_sha256, output: preview.snapshot_id };
  const { component_options, ...retryTicket } = ticket;
  await remember(retryTicket);
  const result = await save(ticket);
  if (!result?.ok || (kind !== "parent" && !result.marker_verified)) throw new Error("Composition update was not verified. Retain the pending operation for retry.");
  await remember(undefined);
  if (kind === "catalog") return result;
  await vscode.window.showInformationMessage(`${kind === "child" ? "Child saved; parent pins unchanged" : "Parent pin published; working folders unchanged"}. Exact recovery point ${result.snapshot_id}.${kind === "child" ? ` ${savedComponents(result.components)}.` : ""}`);
  return result;
}

module.exports = { saveCatalog, enterChild, hydrateChildren, relativeSource, updateRoom };
