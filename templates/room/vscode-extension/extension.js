const childProcess = require("child_process");
const fs = require("fs");
const path = require("path");
const util = require("util");
const vscode = require("vscode");

const execFile = util.promisify(childProcess.execFile);

function activeWorkspace() {
  const folder = vscode.workspace.workspaceFolders?.[0];
  if (!folder) throw new Error("Open a workspace folder before using Josh Room.");
  return folder.uri.fsPath;
}

async function runJoshRoom(args, cwd) {
  try {
    const { stdout } = await execFile("josh-room", [...args, "--json"], {
      cwd,
      env: process.env,
      maxBuffer: 1024 * 1024,
    });
    const result = JSON.parse(stdout);
    if (!result.ok) throw new Error(result.error || "Josh Room operation failed.");
    return result;
  } catch (error) {
    const output = error.stdout || error.stderr;
    if (output) {
      try {
        const result = JSON.parse(output);
        throw new Error(result.error || "Josh Room operation failed.");
      } catch (parseError) {
        if (parseError instanceof SyntaxError) throw error;
        throw parseError;
      }
    }
    throw error;
  }
}

async function loadCatalog(cwd, title = "Loading your Rooms…") {
  return vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title, cancellable: false },
    () => runJoshRoom(["projects", "list", "--backend", "r2"], cwd),
  );
}

function currentRoom(cwd) {
  try {
    const marker = JSON.parse(fs.readFileSync(path.join(cwd, ".josh-room.json"), "utf8"));
    return marker.format_version === 1 ? marker : undefined;
  } catch (_error) {
    return undefined;
  }
}

async function saveRoom() {
  const cwd = activeWorkspace();
  const catalog = await loadCatalog(cwd, "Loading saved Rooms…");
  const marker = currentRoom(cwd);
  const selected = await vscode.window.showQuickPick([
    { label: "$(add) Create a new Room…", create: true },
    ...catalog.projects.map((project) => ({
      label: project.display_name,
      description: marker?.project_id === project.id ? "Current Room" : "Save a new latest snapshot",
      project,
    })),
  ], { title: "Josh: Save Room", placeHolder: "Create or update a Room", ignoreFocusOut: true });
  if (!selected) return "cancelled";
  let name = selected.project?.display_name;
  if (selected.create) {
    name = await vscode.window.showInputBox({
      title: "Josh: Save Room",
      prompt: "Name this Room",
      ignoreFocusOut: true,
      validateInput: (value) => value.trim() ? undefined : "Enter a Room name.",
    });
  }
  if (!name) return "cancelled";
  if (selected.project) {
    const existing = path.join(path.dirname(cwd), selected.project.id);
    if (existing !== cwd && currentRoom(existing)?.project_id === selected.project.id) {
      const action = await vscode.window.showInformationMessage(
        `“${selected.project.display_name}” already has a working folder.`,
        "Open Room",
      );
      if (action === "Open Room") {
        await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(existing), false);
      }
      return "opened";
    }
    if (marker?.project_id !== selected.project.id) {
      const confirmed = await vscode.window.showWarningMessage(
        `Replace the latest “${selected.project.display_name}” snapshot with the contents of ${cwd}? The previous snapshot remains recoverable.`,
        { modal: true },
        "Replace Latest",
      );
      if (confirmed !== "Replace Latest") return "cancelled";
    }
  }
  const result = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Saving ${name}…`, cancellable: false },
    () => runJoshRoom(["snapshot", "create", name, "--backend", "r2"], cwd),
  );
  const size = (result.ciphertext_size / (1024 * 1024)).toFixed(1);
  await vscode.window.showInformationMessage(`Saved “${name}” (${size} MiB).`);
  return "saved";
}

async function enterRoom() {
  const cwd = activeWorkspace();
  const catalog = await loadCatalog(cwd);
  const selected = await vscode.window.showQuickPick(
    catalog.projects.map((project) => ({ label: project.display_name, projectId: project.id })),
    { title: "Josh: Enter Room", placeHolder: "What do you want to work on?", ignoreFocusOut: true },
  );
  if (!selected) return "cancelled";
  const history = await runJoshRoom(["snapshots", "list", selected.projectId, "--backend", "r2"], cwd);
  let snapshotId = "latest";
  if (history.snapshots.length > 1) {
    const snapshot = await vscode.window.showQuickPick(
      history.snapshots
        .map((item) => ({
          label: item.snapshot_id === history.latest ? "Latest snapshot" : "Previous snapshot",
          description: item.created_at || item.snapshot_id,
          snapshotId: item.snapshot_id,
        }))
        .sort((left) => left.snapshotId === history.latest ? -1 : 1),
      { title: `Josh: Enter ${selected.label}`, placeHolder: "Choose a recovery point", ignoreFocusOut: true },
    );
    if (!snapshot) return "cancelled";
    snapshotId = snapshot.snapshotId;
  }
  const root = path.basename(cwd) === "room" ? path.dirname(cwd) : cwd;
  const destination = path.join(root, selected.projectId);
  if (currentRoom(destination)?.project_id === selected.projectId) {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Opening existing ${selected.label}…`, cancellable: false },
      () => vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(destination), false),
    );
    return "opened";
  }
  if (fs.existsSync(destination)) {
    throw new Error(`Refusing to replace an unexplained existing folder: ${destination}`);
  }
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Restoring ${selected.label}…`, cancellable: false },
    () => runJoshRoom([
      "hydrate", selected.projectId, "--snapshot", snapshotId, "--destination", destination,
      "--backend", "r2", "--ide", "terminal",
    ], cwd),
  );
  await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(destination), false);
  return "opened";
}

async function removeRoom() {
  const cwd = activeWorkspace();
  const catalog = await loadCatalog(cwd, "Loading saved Rooms…");
  const selected = await vscode.window.showQuickPick(
    catalog.projects.map((project) => ({ label: project.display_name, project })),
    { title: "Josh: Remove Room", placeHolder: "Choose a Room to remove", ignoreFocusOut: true },
  );
  if (!selected) return "cancelled";
  const snapshots = await runJoshRoom(["snapshots", "list", selected.project.id, "--backend", "r2"], cwd);
  const confirmed = await vscode.window.showWarningMessage(
    `Remove “${selected.label}” and its ${snapshots.snapshots.length} snapshot(s)? This cannot be undone.`,
    { modal: true },
    "Remove Room",
  );
  if (confirmed !== "Remove Room") return "cancelled";
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Removing ${selected.label}…`, cancellable: false },
    () => runJoshRoom(["rooms", "remove", selected.project.id, "--backend", "r2"], cwd),
  );
  await vscode.window.showInformationMessage(`Removed “${selected.label}”.`);
  return "removed";
}

function register(context, command, operation) {
  context.subscriptions.push(vscode.commands.registerCommand(command, async () => {
    try {
      return await operation();
    } catch (error) {
      await vscode.window.showErrorMessage(error.message || String(error));
      return "failed";
    }
  }));
}

function activate(context) {
  register(context, "joshRoom.save", saveRoom);
  register(context, "joshRoom.enter", enterRoom);
  register(context, "joshRoom.remove", removeRoom);
}

module.exports = { activate };
