const assert = require("node:assert/strict");
const test = require("node:test");

const { waitForRegistry } = require("./registry");

test("waitForRegistry waits through startup and returns the live catalog", async () => {
  let attempts = 0;
  const catalog = await waitForRegistry(async () => {
    attempts += 1;
    if (attempts < 3) throw new Error("connection refused");
    return { repositories: ["example/image"] };
  }, { timeoutMs: 100, intervalMs: 0 });

  assert.deepEqual(catalog, { repositories: ["example/image"] });
  assert.equal(attempts, 3);
});

test("waitForRegistry reports the last startup failure at timeout", async () => {
  await assert.rejects(
    waitForRegistry(async () => {
      throw new Error("registry never listened");
    }, { timeoutMs: 0, intervalMs: 0 }),
    /registry never listened/,
  );
});
