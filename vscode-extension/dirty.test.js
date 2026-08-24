const assert = require("node:assert/strict");
const test = require("node:test");

const { shouldMarkDirty } = require("./dirty");

test("dirty tracking notices workspace content and ignores bookkeeping noise", () => {
  assert.equal(shouldMarkDirty("src/app.py"), true);
  assert.equal(shouldMarkDirty(".vscode/tasks.json"), true);
  assert.equal(shouldMarkDirty(".josh-room.json"), false);
  assert.equal(shouldMarkDirty(".git/index"), false);
  assert.equal(shouldMarkDirty("src/__pycache__/app.pyc"), false);
  assert.equal(shouldMarkDirty(".pytest_cache/v/cache/nodeids"), false);
  assert.equal(shouldMarkDirty("node_modules/.cache/tool/value"), false);
  assert.equal(shouldMarkDirty("../outside.txt"), false);
});
