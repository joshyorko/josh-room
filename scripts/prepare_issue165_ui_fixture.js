#!/usr/bin/env node
"use strict";
// Presentation-only fixture: preserve the VSIX and its public command manifest.
// Only a disposable extension copy receives in-memory controller boundaries.
const fs = require("node:fs");
const path = require("node:path");
const cp = require("node:child_process");
const crypto = require("node:crypto");

const [vsix, output] = process.argv.slice(2);
if (!vsix || !output) throw new Error("usage: prepare_issue165_ui_fixture.js <candidate.vsix> <output-dir>");
const root = path.resolve(output);
fs.mkdirSync(root, { recursive: true });
const digest = () => crypto.createHash("sha256").update(fs.readFileSync(vsix)).digest("hex");
const originalDigest = digest();
cp.execFileSync(process.env.ISSUE165_PYTHON || "python", ["-c", `
import pathlib,sys,zipfile
root=pathlib.Path(sys.argv[2]).resolve()
with zipfile.ZipFile(sys.argv[1]) as archive:
    for name in archive.namelist():
        if not (root/name).resolve().is_relative_to(root):
            raise ValueError('unsafe candidate archive member')
    archive.extractall(root)
`, path.resolve(vsix), root]);
const unpacked = path.join(root, "extension");
const manifest = JSON.parse(fs.readFileSync(path.join(unpacked, "package.json"), "utf8"));
const extensions = path.join(root, "extensions");
fs.mkdirSync(extensions, { recursive: true });
const installed = path.join(extensions, `${manifest.publisher}.${manifest.name}-${manifest.version}`);
fs.renameSync(unpacked, installed);
const extensionPath = path.join(installed, "extension.js");
let source = fs.readFileSync(extensionPath, "utf8");
const fixture = `
// TEST ONLY: no controller, provider, keyring, network, or user storage calls.
const issue165StateFile = ${JSON.stringify(path.join(root, "state.json"))};
const issue165Catalog = {
  connections: [{ id: "issue165-connection", display_name: "Synthetic MinIO", provider: "minio", connection_state: "connected" }],
  dimensions: [{ id: "issue165-dimension", display_name: "Issue165 Synthetic Dimension", provider: "minio",
    connection_id: "issue165-connection", encryption_state: "ready", projects: [{ id: "issue165-room",
      display_name: "Issue165 Synthetic Room", latest: "fixture-jat-new", snapshots: [
        { snapshot_id: "fixture-jat-old", created_at: "2026-10-27T12:00:00Z" },
        { snapshot_id: "fixture-jat-new", created_at: "2026-10-28T12:00:00Z", payload_kind: "room-store-v1" },
      ] }] }],
};
const issue165State = { revision: 17, mutations: [], reloads: 0, initialized: false };
function issue165WriteState() {
  const project = issue165Catalog.dimensions[0].projects[0];
  fs.writeFileSync(issue165StateFile, JSON.stringify({ ...issue165State,
    snapshot_ids: project?.snapshots.map(row => row.snapshot_id) || [], latest: project?.latest || null }));
}
async function injectIssue165UiCatalog() {
  runtimeLifecycle = { state: "RUNTIME_READY", message: "Synthetic in-memory UI fixture" };
  loadCatalog = async () => structuredClone(issue165Catalog);
  runSelectedEncryption = async (args) => {
    if (args[0] !== "snapshots" || args[1] !== "list" || args[2] !== "issue165-room"
      || args[args.indexOf("--dimension") + 1] !== "issue165-dimension") throw new Error("Unexpected fixture read");
    const project = issue165Catalog.dimensions[0].projects[0];
    return { ok: true, revision: issue165State.revision, latest: project.latest, snapshots: structuredClone(project.snapshots) };
  };
  destructiveOperation = async (args) => {
    const project = issue165Catalog.dimensions[0].projects[0];
    const inventory = project.snapshots.map(row => row.snapshot_id).join(",");
    if (args[1] !== "remove" || args[2] !== "issue165-room"
      || args[args.indexOf("--dimension") + 1] !== "issue165-dimension"
      || args[args.indexOf("--expected-revision") + 1] !== String(issue165State.revision)
      || args[args.indexOf("--expected-snapshots") + 1] !== inventory) throw new Error("Unexpected or stale fixture mutation");
    if (args[0] === "snapshots") {
      const selected = args[3];
      if (!project.snapshots.some(row => row.snapshot_id === selected)) throw new Error("Unknown fixture recovery point");
      const wasLatest = project.latest === selected;
      project.snapshots = project.snapshots.filter(row => row.snapshot_id !== selected);
      project.latest = project.snapshots.at(-1)?.snapshot_id || null;
      if (!project.snapshots.length) issue165Catalog.dimensions[0].projects = [];
      issue165State.mutations.push({ operation: "snapshots remove", snapshot_id: selected });
      issue165State.revision++;
      issue165WriteState();
      return { ok: true, status: "removed", room_removed: !project.snapshots.length,
        project_id: project.id, snapshot_id: selected, latest_promoted: wasLatest, latest: project.latest };
    }
    if (args[0] === "rooms") {
      issue165State.mutations.push({ operation: "rooms remove" });
      issue165State.revision++;
      issue165Catalog.dimensions[0].projects = [];
      issue165WriteState();
      return { ok: true, status: "removed", project_id: project.id, snapshot_count: project.snapshots.length };
    }
    throw new Error("Unexpected fixture operation");
  };
  const issue165Reload = roomsProvider.reload.bind(roomsProvider);
  roomsProvider.reload = async () => {
    const result = await issue165Reload();
    issue165State.reloads++;
    issue165WriteState();
    return result;
  };
  await roomsProvider.setCatalog(structuredClone(issue165Catalog));
  issue165State.initialized = true;
  issue165WriteState();
}
`;
const anchor = "function activateNative(context) {";
if (source.split(anchor).length !== 2) throw new Error("Candidate activation anchor is not unique");
source = source.replace(anchor, fixture + "\n" + anchor);
const providerAnchor = "  roomsProvider = new HierarchyRoomsProvider();";
if (source.split(providerAnchor).length !== 2) throw new Error("Candidate provider anchor is not unique");
source = source.replace(providerAnchor, providerAnchor + '\n  register(context, "joshRoom.testInjectSyntheticCatalog", injectIssue165UiCatalog);');
fs.writeFileSync(extensionPath, source);
const driver = path.join(extensions, "synthetic.issue165-fixture-driver-0.0.1");
fs.mkdirSync(driver, { recursive: true });
fs.writeFileSync(path.join(driver, "package.json"), JSON.stringify({ name: "issue165-fixture-driver",
  displayName: "Issue165 Synthetic UI Fixture", publisher: "synthetic", version: "0.0.1", engines: { vscode: "^1.90.0" },
  main: "./extension.js", activationEvents: ["*"], extensionDependencies: ["joshyorko.josh-room"] }));
fs.writeFileSync(path.join(driver, "extension.js"), `"use strict";
const vscode = require("vscode");
exports.activate = async () => {
  const target = vscode.extensions.getExtension("joshyorko.josh-room");
  if (!target) throw new Error("Candidate extension missing");
  await target.activate();
  await vscode.commands.executeCommand("workbench.view.extension.josh-room");
  await vscode.commands.executeCommand("joshRoom.testInjectSyntheticCatalog");
};
`);
if (digest() !== originalDigest) throw new Error("Pristine candidate VSIX changed");
console.log(JSON.stringify({ candidate_extension: path.relative(root, installed), driver_extension: path.relative(root, driver),
  candidate_vsix_sha256: originalDigest, extension_version: manifest.version,
  evidence_level: "modified disposable candidate copy; in-memory controller boundaries; UI presentation only",
  fixture: "two recovery points; no external providers or user storage",
  source_sha: cp.execFileSync("git", ["rev-parse", "HEAD"], { cwd: path.resolve(__dirname, ".."), encoding: "utf8" }).trim() }));
