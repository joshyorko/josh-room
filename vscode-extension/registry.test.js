const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { followLogFile, stageForLog, waitForRegistry } = require("./registry");

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

test("stageForLog turns RCC and Hauler output into useful progress", () => {
  assert.equal(stageForLog("Progress: 14/15 Restore space from library"), "Preparing RCC environment");
  assert.equal(stageForLog("INF hauler/example:latest"), "Inspecting haul contents");
  assert.equal(stageForLog("INF copied artifacts to [127.0.0.1:41257]"), "Loading registry images");
  assert.equal(stageForLog("INF starting registry on port [5000]"), "Starting read-only registry");
  assert.equal(stageForLog("level=info msg=\"listening on [::]:5000\""), "Registry is ready");
  assert.equal(stageForLog("redis not configured"), undefined);
});

test("followLogFile ignores stale output and streams replacement lines", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-registry-test-"));
  const logPath = path.join(directory, "stdout.log");
  fs.writeFileSync(logPath, "stale output\n");
  const lines = [];
  const follower = followLogFile(logPath, (line) => lines.push(line), { intervalMs: 5 });
  fs.writeFileSync(logPath, "starting registry on port [5000]\nlistening on [::]:5000\n");
  await new Promise((resolve, reject) => {
    const deadline = Date.now() + 500;
    const timer = setInterval(() => {
      if (lines.length === 2) {
        clearInterval(timer);
        resolve();
      } else if (Date.now() >= deadline) {
        clearInterval(timer);
        reject(new Error(`timed out waiting for log lines: ${JSON.stringify(lines)}`));
      }
    }, 5);
  });
  follower.dispose();
  fs.rmSync(directory, { recursive: true, force: true });

  assert.deepEqual(lines, ["starting registry on port [5000]", "listening on [::]:5000"]);
});
const { buildProviderTree, dimensionArgs } = require("./registry");

test("buildProviderTree renders Provider to Dimension to Room to JAT", () => {
  const tree = buildProviderTree({
    dimensions: [
      {
        id: "archive",
        display_name: "Archive",
        provider: "r2",
        endpoint: "https://objects.example.test",
        bucket: "private-room",
        projects: [
          {
            id: "demo-room",
            display_name: "Demo Room",
            snapshots: [
              { snapshot_id: "jat-002", created_at: "2026-08-26T12:00:00Z" },
              { snapshot_id: "jat-001", created_at: "2026-08-25T12:00:00Z" },
            ],
          },
        ],
      },
    ],
  });

  assert.equal(tree.length, 1);
  assert.equal(tree[0].kind, "provider");
  assert.equal(tree[0].label, "Cloudflare R2");
  assert.equal(tree[0].children[0].kind, "dimension");
  assert.equal(tree[0].children[0].label, "Archive");
  assert.equal(tree[0].children[0].children[0].kind, "room");
  assert.equal(tree[0].children[0].children[0].label, "Demo Room");
  assert.deepEqual(
    tree[0].children[0].children[0].children.map((item) => [item.kind, item.id]),
    [["jat", "jat-002"], ["jat", "jat-001"]],
  );
  assert.match(tree[0].children[0].description, /private-room/);
  assert.doesNotMatch(tree[0].children[0].description, /secret|access.key|identity/i);
});

test("dimensionArgs routes storage operations through the selected Dimension", () => {
  assert.deepEqual(
    dimensionArgs(["snapshot", "create", "demo-room", "--backend", "r2"], "archive"),
    ["snapshot", "create", "demo-room", "--backend", "r2", "--dimension", "archive"],
  );
  assert.deepEqual(
    dimensionArgs(["snapshots", "list", "demo-room", "--dimension", "old"], "archive"),
    ["snapshots", "list", "demo-room", "--dimension", "archive"],
  );
  assert.deepEqual(dimensionArgs(["doctor", "--ide", "terminal"], undefined), ["doctor", "--ide", "terminal"]);
});
