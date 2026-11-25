const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { followLogFile, stageForLog, waitForRegistry } = require("./registry");
const { validateServeStartup, waitForServeStartup, probeRegistry, probeFileserver } = require("./registry");

function startup(mode, fileserverUrl = null, registryUrl = null) {
  return { schema_version: 1, state: "ready", operation: "serve", pid: process.pid, serve: {
    mode, fileserver_url: fileserverUrl, registry_url: registryUrl,
    fileserver_bind: fileserverUrl ? "loopback" : null,
    registry_bind: registryUrl ? "loopback" : null,
  } };
}

test("startup receipts accept truthful artifact scope but reject unsafe URLs and non-loopback registries", () => {
  const good = startup("files", "http://127.0.0.1:41257");
  assert.equal(validateServeStartup(good, "auto").fileserver_url, good.serve.fileserver_url);
  for (const url of ["http://example.test:8080", "http://127.0.0.1:0", "http://127.0.0.1:99999",
    "http://user@127.0.0.1:8080", "http://127.0.0.1:8080/path", "http://127.0.0.1:8080?token=x"]) {
    assert.throws(() => validateServeStartup(startup("files", url)));
  }
  assert.throws(() => validateServeStartup(good, "registry"));
  assert.throws(() => validateServeStartup({ ...good, serve: { ...good.serve, registry_bind: "all-interfaces" } }));
  assert.throws(() => validateServeStartup({ ...good, serve: { ...good.serve, fileserver_bind: "0.0.0.0" } }));
  assert.throws(() => validateServeStartup(startup("both", good.serve.fileserver_url, good.serve.fileserver_url)));
  const wildcard = { ...good, serve: { ...good.serve, fileserver_bind: "all-interfaces" } };
  assert.equal(validateServeStartup(wildcard, "files").fileserver_bind, "all-interfaces");
  const both = startup("both", good.serve.fileserver_url, "http://127.0.0.1:41258");
  both.serve.fileserver_bind = "all-interfaces";
  assert.equal(validateServeStartup(both).registry_bind, "loopback");
  both.serve.registry_bind = "all-interfaces";
  assert.throws(() => validateServeStartup(both));
});

test("startup waits only for its session file and stops immediately on controller failure", async (t) => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-startup-test-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.writeFileSync(path.join(root, "another-session.json"), JSON.stringify(startup("files", "http://127.0.0.1:41257")));
  const own = path.join(root, "own-session.json");
  await assert.rejects(waitForServeStartup(own, { timeoutMs: 0 }), /did not report/);
  const began = Date.now();
  await assert.rejects(waitForServeStartup(own, {
    checkStopped() { throw new Error("portable-export-failed: insufficient disk"); },
  }), /insufficient disk/);
  assert.ok(Date.now() - began < 1000);
  fs.writeFileSync(own, JSON.stringify(startup("files", "http://127.0.0.1:41259")));
  assert.equal((await waitForServeStartup(own)).fileserver_url, "http://127.0.0.1:41259");
});

test("concurrent sessions probe their reported endpoints with occupied defaults", async (t) => {
  const http = require("node:http");
  const servers = [];
  async function listen(port, marker) {
    const server = http.createServer((request, response) => {
      response.setHeader("Content-Type", "application/json");
      response.end(JSON.stringify({ repositories: [marker], path: request.url }));
    });
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(port, "127.0.0.1", resolve);
    });
    servers.push(server);
    return `http://127.0.0.1:${server.address().port}`;
  }
  t.after(async () => Promise.all(servers.map((server) => new Promise((resolve) => server.close(resolve)))));
  for (const port of [8080, 5000]) {
    try { await listen(port, "wrong-session"); }
    catch (error) { if (error.code !== "EADDRINUSE") throw error; }
  }
  const first = await listen(0, "session-one/image");
  const second = await listen(0, "session-two/image");
  const catalogs = await Promise.all([probeRegistry(first), probeRegistry(second)]);
  assert.deepEqual(catalogs.map((catalog) => catalog.repositories), [["session-one/image"], ["session-two/image"]]);
  assert.deepEqual(await probeFileserver(second), { fileserver: true });
});

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
  assert.equal(stageForLog("level=info msg=\"listening on [::]:5000\""), "Serve listener is ready");
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
  assert.equal(tree[0].children[0].children[0].label, "Empty");
  assert.deepEqual(tree[0].children[0].children[0].children, []);
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
  assert.equal(tree[0].children[0].kind, "connection");
  assert.equal(tree[0].children[0].children[0].kind, "dimension");
  assert.equal(tree[0].children[0].children[0].label, "Archive");
  assert.equal(tree[0].children[0].children[0].children[0].kind, "room");
  assert.equal(tree[0].children[0].children[0].children[0].label, "Demo Room");
  assert.deepEqual(
    tree[0].children[0].children[0].children[0].children.map((item) => [item.kind, item.id]),
    [["jat", "jat-002"], ["jat", "jat-001"]],
  );
  assert.equal(tree[0].children[0].children[0].description, "");
  assert.doesNotMatch(tree[0].children[0].children[0].description, /secret|access.key|identity/i);
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

test("routed history status arguments are accepted by the real controller parser", t => {
  const python = process.env.JOSH_ROOM_TEST_PYTHON || path.join(__dirname, "..", ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  if (!fs.existsSync(python)) return t.skip("Python project runtime unavailable; set JOSH_ROOM_TEST_PYTHON");
  const args = dimensionArgs(["snapshot", "agent-save-status", "demo-room", "--source", ".", "--backend", "minio"], "backup");
  assert.equal(args.includes("--backend"), false);
  const result = require("node:child_process").spawnSync(python, ["-c", [
    "import json,sys",
    "from josh_room.cli import build_parser",
    "args=build_parser().parse_args(json.loads(sys.argv[1]))",
    "print(json.dumps({'command':args.snapshot_command,'dimension':args.dimension,'room':args.project}))",
  ].join(";"), JSON.stringify(args)], {
    encoding: "utf8", env: { ...process.env, PYTHONPATH: path.join(__dirname, "..", "src") },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { command: "agent-save-status", dimension: "backup", room: "demo-room" });
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
  assert.equal(tree[0].children[0].children[0].id, "archive");
  assert.equal(tree[0].children[0].children[0].children[0].id, "demo-room");
  assert.deepEqual(tree[0].children[0].children[0].children[0].children.map((item) => item.id), ["jat-002"]);
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

test("duplicate Room IDs and names retain their Dimension in the hierarchy", () => {
  const tree = buildProviderTree({
    dimensions: [
      { id: "archive", display_name: "Archive", provider: "r2", projects: [{ id: "same-room", display_name: "Same Room" }] },
      { id: "backup", display_name: "Backup", provider: "minio", projects: [{ id: "same-room", display_name: "Same Room" }] },
    ],
  });
  const rooms = tree.flatMap((provider) => provider.children.flatMap((connection) => connection.children.flatMap((dimension) => dimension.children)));
  assert.deepEqual(rooms.map((room) => room.label), ["Same Room", "Same Room"]);
  assert.deepEqual(rooms.map((room) => room.dimension.id), ["archive", "backup"]);
});

test("buildProviderTree renders Provider to reusable Provider Connection to bucket Dimension", () => {
  const tree = buildProviderTree({
    connections: [{ id: "minio-home", display_name: "Home MinIO", provider: "minio" }],
    dimensions: [
      { id: "rooms-a", display_name: "Rooms A", provider: "minio", connection_id: "minio-home", bucket: "rooms-a", projects: [] },
      { id: "rooms-b", display_name: "Rooms B", provider: "minio", connection_id: "minio-home", bucket: "rooms-b", projects: [] },
    ],
  });

  assert.equal(tree[0].kind, "provider");
  assert.equal(tree[0].children[0].kind, "connection");
  assert.equal(tree[0].children[0].label, "Home MinIO");
  assert.deepEqual(tree[0].children[0].children.map((item) => [item.kind, item.id, item.dimension.bucket]), [
    ["dimension", "rooms-a", "rooms-a"],
    ["dimension", "rooms-b", "rooms-b"],
  ]);
});

test("buildProviderTree renders a disconnected connection as unavailable", () => {
  const tree = buildProviderTree({
    connections: [{
      id: "home-minio",
      display_name: "Home MinIO",
      provider: "minio",
      auth_state: "disconnected",
    }],
    dimensions: [{
      id: "rooms",
      display_name: "Rooms",
      provider: "minio",
      connection_id: "home-minio",
      bucket: "rooms",
      projects: [{ id: "room", display_name: "Room" }],
    }],
  });

  const connection = tree[0].children[0];
  assert.equal(connection.state, "disconnected");
  assert.equal(connection.label, "Home MinIO");
  assert.equal(connection.description, "Reconnect");
  assert.deepEqual(connection.children[0].children, []);
});

test("buildProviderTree preserves native encryption states and exposes contextual actions", () => {
  const tree = buildProviderTree({
    dimensions: [
      { id: "ready", display_name: "Ready", provider: "minio", encryption_state: "ready", projects: [] },
      { id: "new", display_name: "New", provider: "minio", encryption_state: "uninitialized", projects: [] },
      { id: "old", display_name: "Old", provider: "minio", encryption_state: "legacy", projects: [] },
      { id: "resume", display_name: "Resume", provider: "minio", encryption_state: "resumable", projects: [] },
      { id: "unsafe", display_name: "Unsafe", provider: "minio", encryption_state: "insecure", projects: [] },
      { id: "failed", display_name: "Failed", provider: "minio", encryption_state: "failed", projects: [] },
    ],
  });

  const dimensions = tree[0].children[0].children;
  assert.deepEqual(dimensions.map((item) => item.state), [
    "ready", "uninitialized", "legacy", "resumable", "insecure", "failed",
  ]);
  assert.deepEqual(dimensions.map((item) => item.action?.command), [
    undefined,
    "joshRoom.initializeEncryption",
    "joshRoom.migrateEncryption",
    "joshRoom.resumeEncryption",
    "joshRoom.initializeEncryption",
    "joshRoom.refresh",
  ]);
});

test("failed legacy and resumable Dimensions keep their native actions", () => {
  const tree = buildProviderTree({
    dimensions: [
      { id: "legacy", display_name: "Legacy", provider: "minio", load_error: { state: "legacy", action: "migrate", label: "Migrate Encryption" } },
      { id: "resume", display_name: "Resume", provider: "minio", load_error: { state: "resumable", action: "resume", label: "Resume Encryption" } },
    ],
  });
  const dimensions = tree[0].children[0].children;

  assert.deepEqual(dimensions.map((item) => item.state), ["legacy", "resumable"]);
  assert.deepEqual(dimensions.map((item) => item.action?.command), [
    "joshRoom.migrateEncryption",
    "joshRoom.resumeEncryption",
  ]);
});

test("buildProviderTree accepts worker connection and bucket records without exposing credentials", () => {
  const tree = buildProviderTree({
    providers: [{
      id: "minio",
      connections: [{
        connection_id: "home",
        name: "Home MinIO",
        endpoint: "https://minio.example",
        buckets: [{ name: "rooms" }],
      }],
    }],
    dimensions: [{
      dimension_id: "rooms",
      display_name: "Rooms",
      provider: "minio",
      connection_id: "home",
      bucket: "rooms",
      projects: [],
    }],
  });

  const connection = tree[0].children[0];
  assert.equal(connection.kind, "connection");
  assert.equal(connection.id, "home");
  assert.equal(connection.children[0].kind, "dimension");
  assert.equal(connection.children[0].children.length, 0);
  assert.doesNotMatch(JSON.stringify(tree), /access.key|secret.key|session.token/i);
});

test("recovery points sort Latest first and show a readable fallback when dates are missing", () => {
  const tree = buildProviderTree({ dimensions: [{ id: "archive", provider: "r2", projects: [{
    id: "room", latest: "latest", snapshots: [
      { snapshot_id: "older", created_at: "2026-01-01T00:00:00Z" },
      { snapshot_id: "latest" },
      { snapshot_id: "newer", created_at: "2026-03-01T00:00:00Z" },
    ],
  }] }] });
  const history = tree[0].children[0].children[0].children[0].children;
  assert.deepEqual(history.map((item) => item.id), ["latest", "newer", "older"]);
  assert.equal(history[0].label, "Recovery point");
});

test("S3 retains explicit provider identity rather than Cloudflare authority", () => {
  const { providerKey, providerLabel, buildProviderTree } = require("./registry");
  assert.equal(providerKey("s3"), "s3");
  assert.equal(providerLabel("s3"), "Amazon S3 / S3-compatible");
  const tree = buildProviderTree({ dimensions: [{ id: "synthetic-s3", provider: "s3", bucket: "synthetic-room", projects: [] }] });
  assert.equal(tree[0].provider, "s3");
});

test("pull references require the reported registry authority and matching immutable digest", () => {
  const { verifiedPull } = require("./registry");
  const serve = { session_id: "test", registry_bind: "loopback", registry_url: "http://127.0.0.1:45123" };
  const digest = "sha256:" + "a".repeat(64);
  const ref = "127.0.0.1:45123/nested/team/api@" + digest;
  const verify = (pull_reference) => verifiedPull(serve, { pull_reference, digest });
  assert.equal(verify(ref), ref);
  for (const unsafe of [null, ref + "\n", ref.replace("45123", "5000"), ref.replace("api", "api;id"),
    ref.replace("team", ".."), ref.replace("api", "api$(id)"), ref.slice(0,-1), "api:latest"]) {
    assert.equal(verify(unsafe), undefined);
  }
});

test("Catalog parents and exact points are separate from ordinary Rooms", () => {
  const tree = buildProviderTree({ dimensions: [{
    id: "synthetic", provider: "minio", display_name: "Synthetic bucket",
    rooms: [{ id: "ordinary", display_name: "Ordinary Room" }],
    catalogs: [{ namespace: "1".repeat(32), parent_room_id: "parent", display_name: "Catalog Room", recovery_points: [
      { snapshot_id: "jat-old", created_at: "2026-01-01T00:00:00Z", child_count: 2, children: ["a", "b"] },
      { snapshot_id: "jat-new", created_at: "2026-01-02T00:00:00Z", child_count: 3, children: ["a", "b", "c"] },
      { snapshot_id: "latest", created_at: "2026-01-03T00:00:00Z", child_count: 4 },
    ] }],
    catalog_discovery_partial: true,
  }] });
  const bucket = tree[0].children[0].children[0];
  assert.deepEqual(bucket.children.map(child => child.kind), ["catalog-parent", "catalog-partial", "room"]);
  const parent = bucket.children[0];
  assert.equal(parent.label, "Catalog Room");
  const [newest, older, childGroup] = parent.children;
  assert.deepEqual([newest, older].map(row => row.point.snapshot_id), ["jat-new", "jat-old"]);
  assert.ok([newest, older].every(row => row.kind === "catalog-point" && row.dimension.id === "synthetic" && !row.children));
  assert.deepEqual(childGroup.children.map(row => [row.kind, row.child, row.point.snapshot_id]),
    [["catalog-child", "a", "jat-new"], ["catalog-child", "b", "jat-new"], ["catalog-child", "c", "jat-new"]]);
});

test("Catalog child history and direct entry share one folder row across parent points", () => {
  const rows = buildProviderTree({ dimensions: [{ id: "synthetic", provider: "minio",
    catalogs: [{ namespace: "1".repeat(32), parent_room_id: "parent", display_name: "Example",
      recovery_points: [
        { snapshot_id: "jat-old", created_at: "2026-01-01T00:00:00Z", child_count: 1, children: ["one"] },
        { snapshot_id: "jat-current", created_at: "2026-01-02T00:00:00Z", child_count: 1, children: ["one"] },
      ],
      child_rooms: [{ room_id: "child", path: "one", recovery_points: [
        { snapshot_id: "jat-child", created_at: "2026-01-01T00:00:00Z", pinned: true },
      ] }] }],
  }] });
  const parent = rows[0].children[0].children[0].children[0];
  const [newest, older, group] = parent.children;
  assert.deepEqual([newest.children, older.children], [undefined, undefined]);
  assert.equal(group.label, "Child Rooms");
  assert.equal(group.children.length, 1);
  const [room] = group.children;
  assert.equal(room.label, "one");
  assert.equal(room.description, "1 recovery point");
  assert.equal(room.point.snapshot_id, "jat-current");
  assert.equal(room.children[0].point.snapshot_id, "jat-child");
});

test("a Catalog with no parent points keeps its child Rooms visible for deletion", () => {
  const rows = buildProviderTree({ dimensions: [{ id: "synthetic", provider: "minio",
    catalogs: [{ namespace: "1".repeat(32), parent_room_id: "parent", display_name: "Example",
      recovery_points: [], child_rooms: [{ room_id: "child", path: "one", recovery_points: [
        { snapshot_id: "jat-child", created_at: "2026-01-02T00:00:00Z", pinned: false },
      ] }] }],
  }] });
  const parent = rows[0].children[0].children[0].children[0];
  assert.equal(parent.label, "Example");
  assert.equal(parent.description, "No recovery points");
  const [room] = parent.children[0].children;
  assert.equal(room.label, "one");
  assert.equal(room.point, undefined);
  assert.equal(room.children[0].point.snapshot_id, "jat-child");
});
