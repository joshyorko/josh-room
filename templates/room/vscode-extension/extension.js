const childProcess = require("child_process");
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

async function saveRoom() {
  const cwd = activeWorkspace();
  const name = await vscode.window.showInputBox({
    title: "Josh: Save Room",
    prompt: "Name this Room",
    ignoreFocusOut: true,
    validateInput: (value) => value.trim() ? undefined : "Enter a Room name.",
  });
  if (!name) return "cancelled";
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
  const catalog = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Loading your Rooms…", cancellable: false },
    () => runJoshRoom(["projects", "list", "--backend", "r2"], cwd),
  );
  const selected = await vscode.window.showQuickPick(
    catalog.projects.map((project) => ({ label: project.display_name, projectId: project.id })),
    { title: "Josh: Enter Room", placeHolder: "What do you want to work on?", ignoreFocusOut: true },
  );
  if (!selected) return "cancelled";
  const root = path.basename(cwd) === "room" ? path.dirname(cwd) : cwd;
  const destination = path.join(root, selected.projectId);
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Restoring ${selected.label}…`, cancellable: false },
    () => runJoshRoom([
      "hydrate", selected.projectId, "--destination", destination,
      "--backend", "r2", "--ide", "terminal",
    ], cwd),
  );
  await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.file(destination), false);
  return "opened";
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
}

module.exports = { activate };
