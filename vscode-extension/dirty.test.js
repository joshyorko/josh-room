const assert = require("node:assert/strict");
const test = require("node:test");

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { WorkspaceBaseline, shouldMarkDirty } = require("./dirty");

test("dirty tracking notices workspace content and ignores bookkeeping noise", () => {
  assert.equal(shouldMarkDirty("src/app.py"), true);
  assert.equal(shouldMarkDirty(".vscode/tasks.json"), true);
  assert.equal(shouldMarkDirty(".josh-room.json"), false);
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
