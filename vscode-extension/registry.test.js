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
const { buildProviderTree, dimensionArgs, flattenDimensionRooms, snapshotCopyArgs } = require("./registry");
test("empty Dimensions remain visible even when a legacy top-level project list exists", () => {
  const tree = buildProviderTree({
    projects: [{ id: "legacy-room", display_name: "Legacy Room" }],
    dimensions: [{ id: "empty", display_name: "Empty", provider: "r2" }],
  });

  assert.equal(tree[0].children.length, 1);
  assert.equal(tree[0].children[0].label, "Empty");
  assert.deepEqual(tree[0].children[0].children, []);
});

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
  assert.equal(tree[0].children[0].children[0].label, "Demo Room · Archive");
  assert.deepEqual(
    tree[0].children[0].children[0].children.map((item) => [item.kind, item.id]),
    [["jat", "jat-002"], ["jat", "jat-001"]],
  );
  assert.equal(tree[0].children[0].description, "");
  assert.doesNotMatch(tree[0].children[0].description, /secret|access.key|identity/i);
});

test("dimensionArgs routes storage operations through the selected Dimension", () => {
  assert.deepEqual(
    dimensionArgs(["snapshot", "create", "demo-room", "--backend", "r2"], "archive"),
    ["snapshot", "create", "demo-room", "--dimension", "archive"],
  );
  assert.deepEqual(
    dimensionArgs(["snapshots", "list", "demo-room", "--dimension", "old"], "archive"),
    ["snapshots", "list", "demo-room", "--dimension", "archive"],
  );
  assert.deepEqual(dimensionArgs(["doctor", "--ide", "terminal"], undefined), ["doctor", "--ide", "terminal"]);
});

test("buildProviderTree reads v2 dimension catalogs and populates JAT children", () => {
  const tree = buildProviderTree({
    dimensions: {
      archive: {
        dimension_id: "archive",
        display_name: "Archive",
        provider: "r2",
        bucket: "private-room",
        projects: {
          "demo-room": {
            display_name: "Demo Room",
            snapshots: {
              "jat-002": { snapshot_id: "jat-002", created_at: "2026-08-26T12:00:00Z" },
            },
          },
        },
      },
    },
  });
  assert.equal(tree[0].children[0].id, "archive");
  assert.equal(tree[0].children[0].children[0].id, "demo-room");
  assert.deepEqual(tree[0].children[0].children[0].children.map((item) => item.id), ["jat-002"]);
});

test("snapshotCopyArgs matches the native copy contract", () => {
  assert.deepEqual(snapshotCopyArgs({
    kind: "jat", project: { id: "room" }, id: "jat-7", dimension: { id: "source" },
  }, { kind: "room", id: "destination", dimension: { id: "target" } }), [
    "snapshot", "copy", "room", "--snapshot", "jat-7",
    "--source-dimension", "source", "--destination-dimension", "target",
    "--destination-room", "destination",
  ]);
  assert.deepEqual(snapshotCopyArgs({
    kind: "folder", path: "/tmp/workspace", dimension_id: "source",
  }, { destination_room: "destination", id: "target" }), [
    "snapshot", "copy", "--source-folder", "/tmp/workspace",
    "--destination-dimension", "target", "--destination-room", "destination",
  ]);
});
test("flattenDimensionRooms decorates command-palette Rooms with their Dimension", () => {
  const rooms = flattenDimensionRooms({
    dimensions: [
      {
        id: "archive",
        display_name: "Archive",
        provider: "r2",
        projects: [{ id: "demo-room", display_name: "Demo Room" }],
      },
      { id: "backup", display_name: "Backup", provider: "minio", projects: [] },
    ],
  });

  assert.deepEqual(rooms.map((room) => ({
    id: room.id,
    display_name: room.display_name,
    dimension_id: room.dimension_id,
    dimension: room.dimension.display_name,
  })), [{
    id: "demo-room",
    display_name: "Demo Room",
    dimension_id: "archive",
    dimension: "Archive",
  }]);
});

test("duplicate Room IDs and names remain Dimension-qualified in the hierarchy", () => {
  const tree = buildProviderTree({
    dimensions: [
      { id: "archive", display_name: "Archive", provider: "r2", projects: [{ id: "same-room", display_name: "Same Room" }] },
      { id: "backup", display_name: "Backup", provider: "minio", projects: [{ id: "same-room", display_name: "Same Room" }] },
    ],
  });
  const rooms = tree.flatMap((provider) => provider.children.flatMap((dimension) => dimension.children));
  assert.deepEqual(rooms.map((room) => room.label), ["Same Room · Archive", "Same Room · Backup"]);
  assert.deepEqual(rooms.map((room) => room.dimension.id), ["archive", "backup"]);
});
