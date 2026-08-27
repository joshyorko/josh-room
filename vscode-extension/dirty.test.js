const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const test = require("node:test");

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  WorkspaceBaseline,
  fingerprintFile,
  fingerprintWorkspace,
  isRoomMarker,
  shouldMarkDirty,
} = require("./dirty");

test("dirty tracking notices workspace content and ignores bookkeeping noise", () => {
  assert.equal(shouldMarkDirty("src/app.py"), true);
  assert.equal(shouldMarkDirty(".vscode/tasks.json"), true);
  assert.equal(shouldMarkDirty(".josh-room.json"), false);
  assert.equal(shouldMarkDirty("__pycache__"), false);
  assert.equal(shouldMarkDirty("__pycache__/module.pyc"), false);
  assert.equal(shouldMarkDirty("src/__pycache__"), false);
  assert.equal(shouldMarkDirty(".git/index"), false);
  assert.equal(shouldMarkDirty("src/__pycache__/app.pyc"), false);
  assert.equal(shouldMarkDirty(".pytest_cache/v/cache/nodeids"), false);
  assert.equal(shouldMarkDirty("node_modules/.cache/tool/value"), false);
  assert.equal(shouldMarkDirty("node_modules/package/index.js"), false);
  assert.equal(shouldMarkDirty(".venv/lib/python/site.py"), false);
  assert.equal(shouldMarkDirty("../outside.txt"), false);
});

test("workspace baseline returns to clean when file content is reverted", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-dirty-test-"));
  const source = path.join(root, "source.txt");
  fs.writeFileSync(source, "saved\n");
  const baseline = new WorkspaceBaseline(root);
  await baseline.capture();

  fs.writeFileSync(source, "changed\n");
  assert.equal(await baseline.check("source.txt"), true);
  fs.writeFileSync(source, "saved\n");
  assert.equal(await baseline.check("source.txt"), false);

  fs.rmSync(source);
  assert.equal(await baseline.check("source.txt"), true);
  fs.writeFileSync(source, "saved\n");
  assert.equal(await baseline.check("source.txt"), false);
  fs.rmSync(root, { recursive: true, force: true });
});

test("room marker validation accepts v2 bindings while retaining readable v1", () => {
  assert.equal(isRoomMarker({
    format_version: 2,
    dimension_id: "archive",
    project_id: "demo",
    display_name: "Demo",
    snapshot_id: "jat-1",
    workspace_fingerprint: "a".repeat(64),
    workspace_path_sha256: "b".repeat(64),
  }), true);
  assert.equal(isRoomMarker({
    format_version: 1,
    project_id: "demo",
    display_name: "Demo",
  }), true);
  assert.equal(isRoomMarker({ format_version: 1, project_id: "demo" }), false);
  assert.equal(isRoomMarker({ format_version: 2, project_id: "demo", snapshot_id: "jat-1" }), false);
  assert.equal(isRoomMarker({
    format_version: 2,
    dimension_id: "archive",
    project_id: "demo",
    display_name: "Demo",
    snapshot_id: "jat-1",
  }), false);
});


test("authoritative status fingerprint clears dirty state after a revert", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-authoritative-dirty-test-"));
  const source = path.join(root, "source.txt");
  fs.writeFileSync(source, "saved\n");
  let statusFingerprint = "saved-fingerprint";
  const baseline = new WorkspaceBaseline(root, {
    savedFingerprint: statusFingerprint,
    currentFingerprint: statusFingerprint,
    fingerprintProvider: async () => statusFingerprint,
  });
  await baseline.capture();

  fs.writeFileSync(source, "changed\n");
  statusFingerprint = "changed-fingerprint";
  assert.equal(await baseline.check("source.txt"), true);
  fs.writeFileSync(source, "saved\n");
  statusFingerprint = "saved-fingerprint";
  assert.equal(await baseline.check("source.txt"), false);
  fs.rmSync(root, { recursive: true, force: true });
});

test("large file fingerprints change outside the sampled head, middle, and tail", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-fingerprint-test-"));
  const source = path.join(root, "large.bin");
  const size = 5 * 1024 * 1024 + 17;
  const buffer = Buffer.alloc(size, 0x61);
  fs.writeFileSync(source, buffer);
  const first = await fingerprintFile(source);
  const handle = fs.openSync(source, "r+");
  try {
    fs.writeSync(handle, Buffer.from([0x62]), 0, 1, 128 * 1024);
  } finally {
    fs.closeSync(handle);
  }
  const second = await fingerprintFile(source);
  fs.rmSync(root, { recursive: true, force: true });

  assert.notEqual(second, first);
});

test("authoritative capture fingerprints many entries without retaining the complete workspace map", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-bounded-fingerprint-test-"));
  for (let index = 0; index < 512; index += 1) {
    fs.writeFileSync(path.join(root, `entry-${String(index).padStart(4, "0")}.txt`), `${index}\n`);
  }
  const baseline = new WorkspaceBaseline(root, {
    savedFingerprint: "0".repeat(64),
    fingerprintProvider: async () => "0".repeat(64),
  });

  await baseline.capture();

  assert.ok(baseline.files.size <= 32, `retained ${baseline.files.size} workspace entries`);
  assert.match(baseline.currentFingerprint, /^[0-9a-f]{64}$/);
  fs.rmSync(root, { recursive: true, force: true });
});

test("workspace fingerprint uses Python-compatible UTF-8 byte ordering for Unicode names", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-unicode-fingerprint-test-"));
  const names = ["\uE000.txt", "\u{10000}.txt"];
  for (const [index, name] of names.entries()) fs.writeFileSync(path.join(root, name), `${index}\n`);

  const expected = crypto.createHash("sha256");
  for (const name of [...names].sort((left, right) => Buffer.from(left).compare(Buffer.from(right)))) {
    const fingerprint = await fingerprintFile(path.join(root, name));
    expected.update(name);
    expected.update("\0");
    expected.update(fingerprint);
    expected.update("\n");
  }

  assert.equal(await fingerprintWorkspace(root), expected.digest("hex"));
  assert.equal(await fingerprintWorkspace(root), await fingerprintWorkspace(root));
  fs.rmSync(root, { recursive: true, force: true });
});

test("wide-directory fingerprinting does not collect the complete directory with readdir", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-wide-fingerprint-test-"));
  for (let index = 0; index < 512; index += 1) {
    fs.writeFileSync(path.join(root, `entry-${String(index).padStart(4, "0")}.txt`), `${index}\n`);
  }
  const originalReaddir = fs.promises.readdir;
  fs.promises.readdir = async () => {
    throw new Error("fingerprint traversal collected a complete directory");
  };
  try {
    await fingerprintWorkspace(root);
  } finally {
    fs.promises.readdir = originalReaddir;
    fs.rmSync(root, { recursive: true, force: true });
  }
});
