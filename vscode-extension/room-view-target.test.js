const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const { roomViewTargets, matchingRoomViewTargets } = require("./room-view-target");

test("Room selection binds open roots to Dimension and Room, never cwd or history", t => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "room-view-target-"));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const folders = ["alpha", "beta"].map(dimension => {
    const root = path.join(base, dimension); fs.mkdirSync(root);
    fs.writeFileSync(path.join(root, ".josh-room.json"), JSON.stringify({
      format_version: 2, project_id: "same-room", dimension_id: dimension, display_name: "Synthetic",
      snapshot_id: "jat-1", workspace_fingerprint: "a".repeat(64),
      workspace_path_sha256: crypto.createHash("sha256").update(root).digest("hex"),
    }));
    return { uri: { scheme: "file", fsPath: root } };
  });
  const targets = roomViewTargets(folders);
  assert.equal(targets.length, 2);
  assert.equal(matchingRoomViewTargets(targets, { kind: "room", project: { id: "same-room" }, dimension: { id: "beta" } })[0].root, folders[1].uri.fsPath);
  assert.deepEqual(matchingRoomViewTargets(targets, { kind: "jat", project: { id: "same-room" }, dimension: { id: "beta" } }), []);
  assert.deepEqual(matchingRoomViewTargets(targets, { id: "same-room" }), []);
  fs.renameSync(path.join(folders[0].uri.fsPath, ".josh-room.json"), path.join(base, "marker"));
  fs.symlinkSync(path.join(base, "marker"), path.join(folders[0].uri.fsPath, ".josh-room.json"));
  assert.equal(roomViewTargets(folders).length, 1);
  fs.writeFileSync(path.join(folders[1].uri.fsPath, ".josh-room.json"), "x".repeat(65537));
  assert.deepEqual(roomViewTargets(folders), []);
});

test("symlinked OS ancestors resolve to the bound root, but a symlinked workspace is rejected", t => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), "room-view-home-"));
  t.after(() => fs.rmSync(base, { recursive: true, force: true }));
  const realHome = path.join(base, "var-home"); fs.mkdirSync(realHome);
  const root = path.join(realHome, "room"); fs.mkdirSync(root);
  const alias = path.join(base, "home"); fs.symlinkSync(realHome, alias);
  fs.writeFileSync(path.join(root, ".josh-room.json"), JSON.stringify({ format_version: 2,
    project_id: "room", dimension_id: "dimension", display_name: "Synthetic", snapshot_id: "jat-1",
    workspace_fingerprint: "a".repeat(64), workspace_path_sha256: crypto.createHash("sha256").update(root).digest("hex") }));
  assert.equal(roomViewTargets([{ uri: { scheme: "file", fsPath: path.join(alias, "room") } }])[0].root, root);
  const directAlias = path.join(base, "workspace-alias"); fs.symlinkSync(root, directAlias);
  assert.deepEqual(roomViewTargets([{ uri: { scheme: "file", fsPath: directAlias } }]), []);
});

test("sidebar selection requires the catalog encryption domain and composition namespace", () => {
  const marker = { project_id: "demo", dimension_id: "work", encryption_domain_id: "domain-a" };
  const target = { marker };
  const item = { kind: "room", project: { id: "demo" }, dimension: { id: "work", encryption_domain_id: "domain-b" } };
  assert.deepEqual(matchingRoomViewTargets([target], item), []);
  item.dimension.encryption_domain_id = "domain-a";
  assert.deepEqual(matchingRoomViewTargets([target], item), [target]);
  marker.composition_namespace = "a".repeat(32);
  assert.deepEqual(matchingRoomViewTargets([target], item), []);
  item.project.composition_namespace = marker.composition_namespace;
  assert.deepEqual(matchingRoomViewTargets([target], item), [target]);
  delete marker.encryption_domain_id;
  assert.deepEqual(matchingRoomViewTargets([target], item), []);
});
