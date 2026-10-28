#!/usr/bin/env node
"use strict";
// Prepare a disposable copy of the candidate VSIX with an explicitly test-only
// in-memory catalog injector. Nothing under the repository or installed VSIX is edited.
const fs = require("node:fs");
const path = require("node:path");
const cp = require("node:child_process");

const [vsix, output] = process.argv.slice(2);
if (!vsix || !output) throw new Error("usage: prepare_issue165_ui_fixture.js <candidate.vsix> <output-dir>");
const root = path.resolve(output);
fs.mkdirSync(root, { recursive: true });
cp.execFileSync("unzip", ["-q", "-o", path.resolve(vsix), "-d", root]);
const installed = path.join(root, "extension");
const extensionPath = path.join(installed, "extension.js");
let extension = fs.readFileSync(extensionPath, "utf8");
const anchor = "function activateNative(context) {";
if (extension.split(anchor).length !== 2) throw new Error("candidate activation anchor was not unique");
const fixture = `
// TEST-ONLY issue 165 fixture. In-memory catalog; no provider or controller writes.
const issue165UiCatalog = {
  connections: [{ id: "issue165-connection", display_name: "Synthetic MinIO", provider: "minio", connection_state: "connected" }],
  dimensions: [{
    id: "issue165-dimension", dimension_id: "issue165-dimension", display_name: "Issue 165 Synthetic Dimension",
    provider: "minio", connection_id: "issue165-connection", encryption_state: "ready",
    projects: [{
      id: "issue165-room", display_name: "Issue 165 Synthetic Room", latest: "fixture-jat-new",
      snapshots: [
        { snapshot_id: "fixture-jat-new", created_at: "2026-10-28T12:00:00Z" },
        { snapshot_id: "fixture-jat-old", created_at: "2026-10-27T12:00:00Z" },
      ],
    }],
  }],
};
async function injectIssue165UiCatalog() {
  runtimeLifecycle = { state: "RUNTIME_READY", message: "Issue 165 test-only synthetic catalog" };
  loadCatalog = async () => issue165UiCatalog;
  runSelectedEncryption = async (args) => args.includes("list")
    ? { ok: true, revision: 17, latest: "fixture-jat-new", snapshots: issue165UiCatalog.dimensions[0].projects[0].snapshots }
    : { ok: true };
  destructiveOperation = async (args) => args.includes("list")
    ? { ok: true, revision: 17, latest: "fixture-jat-new", snapshots: issue165UiCatalog.dimensions[0].projects[0].snapshots }
    : { ok: true, status: "success", project_id: "issue165-room", snapshot_count: 2 };
  roomsProvider.reload = async () => roomsProvider.setCatalog(issue165UiCatalog);
  await roomsProvider.setCatalog(issue165UiCatalog);
  return "synthetic-catalog-loaded";
}
`;
extension = extension.replace(anchor, `${fixture}\n${anchor}`);
const providerAnchor = "  roomsProvider = new HierarchyRoomsProvider();";
if (extension.split(providerAnchor).length !== 2) throw new Error("candidate provider anchor was not unique");
extension = extension.replace(providerAnchor, `${providerAnchor}\n  register(context, "joshRoom.testInjectSyntheticCatalog", injectIssue165UiCatalog);\n  register(context, "joshRoom.testShowDeleteRoomConfirmation", () => deleteRoom({ ...issue165UiCatalog.dimensions[0].projects[0], dimension: issue165UiCatalog.dimensions[0], dimension_id: issue165UiCatalog.dimensions[0].id }));`);
fs.writeFileSync(extensionPath, extension);
const manifestPath = path.join(installed, "package.json");
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
manifest.contributes.commands.push({ command: "joshRoom.testShowDeleteRoomConfirmation", title: "Issue 165 Test Fixture: Show Delete Room Confirmation" });
fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);

const driver = path.join(root, "issue165-fixture-driver");
fs.mkdirSync(driver, { recursive: true });
fs.writeFileSync(path.join(driver, "package.json"), JSON.stringify({
  name: "issue165-fixture-driver", displayName: "Issue 165 UI Fixture Driver", version: "0.0.1",
  publisher: "synthetic", engines: { vscode: "^1.80.0" }, main: "./extension.js",
  activationEvents: ["*"], extensionDependencies: ["joshyorko.josh-room"],
}, null, 2));
fs.writeFileSync(path.join(driver, "extension.js"), `"use strict";\nconst fs = require("node:fs");\nconst path = require("node:path");\nconst vscode = require("vscode");\nexports.activate = async () => {\n  const log = path.join(vscode.workspace.workspaceFolders?.[0]?.uri.fsPath || process.env.TMPDIR, ".issue165-fixture-driver.log");\n  const write = (value) => fs.appendFileSync(log, String(value) + "\\n");\n  try {\n    const target = vscode.extensions.getExtension("joshyorko.josh-room");\n    if (!target) throw new Error("candidate Josh Room extension is not installed");\n    await target.activate();\n    write("target activated");\n    await vscode.commands.executeCommand("workbench.view.extension.josh-room");\n    write("Rooms view focused");\n    const result = await vscode.commands.executeCommand("joshRoom.testInjectSyntheticCatalog");\n    write(result);\n  } catch (error) { write(error.stack || error.message); }\n};\n`);
const driverEntry = path.join(driver, "extension.js");
fs.writeFileSync(driverEntry, fs.readFileSync(driverEntry, "utf8").replace(
  "    write(result);",
  "    write(result);\n    void vscode.commands.executeCommand(\"joshRoom.testShowDeleteRoomConfirmation\").then((choice) => write(\"confirmation-dismissed:\" + (choice || \"cancelled\")), (error) => write(error.stack || error.message));",
));
console.log(JSON.stringify({ candidate_extension: path.basename(installed), driver_extension: path.basename(driver), fixture: "two-snapshot in-memory MinIO Dimension; no controller/provider calls", source_sha: cp.execFileSync("git", ["rev-parse", "HEAD"], { cwd: path.resolve(__dirname, ".."), encoding: "utf8" }).trim() }));
