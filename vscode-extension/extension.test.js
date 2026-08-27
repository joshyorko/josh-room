const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { EventEmitter } = require("node:events");
const Module = require("node:module");
const { buildProviderTree, flattenDimensionRooms } = require("./registry");

function sha256Hex(value) {
  return crypto.createHash("sha256").update(String(value)).digest("hex");
}

function writeMarker(root, {
  dimension_id = "archive",
  project_id = "demo-room",
  display_name = "Demo Room",
  snapshot_id = "jat-1",
  workspace_fingerprint = "a".repeat(64),
  workspace_path_sha256 = sha256Hex(root),
} = {}) {
  fs.writeFileSync(path.join(root, ".josh-room.json"), `${JSON.stringify({
    format_version: 2,
    dimension_id,
    project_id,
    display_name,
    snapshot_id,
    workspace_fingerprint,
    workspace_path_sha256,
  })}\n`);
}

function createSpawnHarness(respond) {
  const calls = [];
  function spawn(command, args, options) {
    const child = new EventEmitter();
    child.pid = 1000 + calls.length;
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.kill = () => {};
    child.closeWith = ({ stdout = "", stderr = "", code = 0 } = {}) => {
      if (stdout) child.stdout.emit("data", Buffer.from(stdout));
      if (stderr) child.stderr.emit("data", Buffer.from(stderr));
      child.emit("close", code);
    };
    calls.push({ command, args, options, child });
    const response = respond({ command, args, options, child, calls });
    if (response?.autoClose !== false) {
      setImmediate(() => child.closeWith(response));
    }
    return child;
  }
  return { calls, spawn };
}

function createVscodeMock(workspaceFolder, textDocuments = []) {
  const quickPickCalls = [];
  const openDialogCalls = [];
  const inputBoxCalls = [];
  const infoCalls = [];
  const warningCalls = [];
  const openExternalCalls = [];
  const executeCalls = [];
  const commandCallbacks = new Map();
  const watcherCallbacks = [];
  const quickPickResponses = [];
  const openDialogResponses = [];
  const inputBoxResponses = [];
  const infoResponses = [];
  const warningResponses = [];
  const statusItem = {
    text: "",
    tooltip: "",
    command: "",
    show() {},
    hide() {},
  };
  const outputChannel = {
    info() {},
    warn() {},
    error() {},
    appendLine() {},
    show() {},
  };
  return {
    vscode: {
      Uri: {
        file: (fsPath) => ({ fsPath }),
        parse: (value) => ({ value }),
      },
      StatusBarAlignment: { Left: 0 },
      ProgressLocation: { Notification: 0 },
      RelativePattern: class RelativePattern {
        constructor(base, pattern) {
          this.base = base;
          this.pattern = pattern;
        }
      },
      TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
      ThemeIcon: class ThemeIcon {
        constructor(id) {
          this.id = id;
        }
      },
      TreeItem: class TreeItem {
        constructor(label, collapsibleState) {
          this.label = label;
          this.collapsibleState = collapsibleState;
        }
      },
      MarkdownString: class MarkdownString {
        appendCodeblock() {}
        appendMarkdown() {}
      },
      DataTransferItem: class DataTransferItem {
        constructor(value) {
          this.value = value;
        }
        async asString() {
          return this.value;
        }
      },
      EventEmitter: class VscodeEventEmitter {
        constructor() {
          this.listeners = [];
          this.event = (listener) => {
            this.listeners.push(listener);
            return { dispose: () => { this.listeners = this.listeners.filter((entry) => entry !== listener); } };
          };
        }
        fire(value) {
          for (const listener of this.listeners) listener(value);
        }
      },
      env: {
        clipboard: { writeText: async () => {} },
        openExternal: async (uri) => {
          openExternalCalls.push(uri);
          return true;
        },
      },
      workspace: {
        workspaceFolders: workspaceFolder ? [{ uri: { fsPath: workspaceFolder } }] : [],
        textDocuments,
        createFileSystemWatcher() {
          const watcher = {
            didChange: undefined,
            didCreate: undefined,
            didDelete: undefined,
            dispose() {},
            onDidChange(cb) { watcher.didChange = cb; },
            onDidCreate(cb) { watcher.didCreate = cb; },
            onDidDelete(cb) { watcher.didDelete = cb; },
          };
          watcherCallbacks.push(watcher);
          return watcher;
        },
        onDidChangeTextDocument() { return { dispose() {} }; },
        onDidSaveTextDocument() { return { dispose() {} }; },
        onDidCloseTextDocument() { return { dispose() {} }; },
        onDidRenameFiles() { return { dispose() {} }; },
      },
      window: {
        showOpenDialog: async (options) => {
          openDialogCalls.push(options);
          return openDialogResponses.shift();
        },
        showQuickPick: async (items, options) => {
          quickPickCalls.push({ items, options });
          return quickPickResponses.shift();
        },
        showInputBox: async (options) => {
          inputBoxCalls.push(options);
          return inputBoxResponses.shift();
        },
        showInformationMessage: async (...args) => {
          infoCalls.push(args);
          return infoResponses.shift();
        },
        showWarningMessage: async (...args) => {
          warningCalls.push(args);
          return warningResponses.shift();
        },
        withProgress: async (_options, task) => task({ report() {} }, { onCancellationRequested() { return { dispose() {} }; } }),
        createStatusBarItem: () => statusItem,
        createOutputChannel: () => outputChannel,
        createTreeView: () => ({ dispose() {} }),
        createTerminal: () => ({ show() {}, sendText() {}, dispose() {} }),
        onDidCloseTerminal() { return { dispose() {} }; },
      },
      commands: {
        registerCommand(name, callback) {
          commandCallbacks.set(name, callback);
          return { dispose() {} };
        },
        executeCommand: async (name, ...args) => {
          executeCalls.push([name, ...args]);
          if (name === "vscode.openFolder") return undefined;
          return undefined;
        },
      },
      quickPickResponses,
      openDialogResponses,
      inputBoxResponses,
      infoResponses,
      warningResponses,
    },
    statusItem,
    outputChannel,
    quickPickCalls,
    openDialogCalls,
    inputBoxCalls,
    infoCalls,
    warningCalls,
    openExternalCalls,
    executeCalls,
    commandCallbacks,
    watcherCallbacks,
    quickPickResponses,
    openDialogResponses,
    inputBoxResponses,
    infoResponses,
    warningResponses,
  };
}

function loadExtension(vscodeMock, spawnMock) {
  const originalLoad = Module._load;
  const targets = [
    require.resolve("./extension"),
    require.resolve("./dirty"),
    require.resolve("./registry"),
    require.resolve("./progress"),
  ];
  for (const target of targets) delete require.cache[target];
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "vscode") return vscodeMock;
    if (request === "child_process") return { spawn: spawnMock };
    return originalLoad(request, parent, isMain);
  };
  try {
    return require("./extension");
  } finally {
    Module._load = originalLoad;
  }
}

test("enter Room keeps the selected Dimension when the project id already exists elsewhere", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-test-"));
  writeMarker(root, { dimension_id: "archive", project_id: "demo-room", display_name: "Demo Room" });
  const { vscode, statusItem, executeCalls, commandCallbacks } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "projects") {
      return {
        stdout: JSON.stringify({
          dimensions: [
            {
              id: "archive",
              display_name: "Archive",
              provider: "r2",
              projects: [{ id: "demo-room", display_name: "Demo Room" }],
            },
            {
              id: "backup",
              display_name: "Backup",
              provider: "minio",
              projects: [{ id: "demo-room", display_name: "Demo Room" }],
            },
          ],
        }),
      };
    }
    if (args[0] === "snapshots") {
      return {
        stdout: JSON.stringify({
          ok: true,
          latest: "jat-2",
          snapshots: [{ snapshot_id: "jat-2", created_at: "2026-08-26T12:00:00Z" }],
        }),
      };
    }
    if (args[0] === "hydrate") {
      return { stdout: JSON.stringify({ ok: true, project_id: args[1], dimension_id: "backup" }) };
    }
    return { stdout: JSON.stringify({ ok: true }) };
  });

  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  extension.__test__.setSelectedDimensionId(undefined);
  await extension.__test__.enterRoom({
    id: "demo-room",
    display_name: "Demo Room",
    dimension: { id: "backup", display_name: "Backup", provider: "minio" },
  });

  assert.equal(spawnHarness.calls.filter((entry) => entry.args[0] === "hydrate").length, 1);
  assert.match(spawnHarness.calls.at(-1).args.join(" "), /--dimension backup/);
  assert.equal(executeCalls.some(([name]) => name === "vscode.openFolder"), true);
});

test("Save Room asks for a Dimension before creating when no selection exists", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-save-test-"));
  const source = path.join(root, "source-room");
  fs.mkdirSync(source);
  const { vscode, statusItem, quickPickCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") {
      return {
        stdout: JSON.stringify({
          ok: true,
          dimensions: [
            { id: "archive", display_name: "Archive", provider: "r2", projects: [] },
            { id: "backup", display_name: "Backup", provider: "minio", projects: [] },
          ],
        }),
      };
    }
    if (args[0] === "projects") {
      return {
        stdout: JSON.stringify({
          dimensions: [
            { id: "archive", display_name: "Archive", provider: "r2", projects: [] },
            { id: "backup", display_name: "Backup", provider: "minio", projects: [] },
          ],
        }),
      };
    }
    if (args[0] === "snapshot" && args[1] === "create") {
      return { stdout: JSON.stringify({ ok: true, project_id: args[2], ciphertext_size: 1048576 }) };
    }
    return { stdout: JSON.stringify({ ok: true }) };
  });

  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  extension.__test__.setSelectedDimensionId(undefined);
  vscode.openDialogResponses.push([{ fsPath: source }]);
  vscode.inputBoxResponses.push("New Room");
  vscode.quickPickResponses.push(
    { create: true },
    { dimension: { id: "backup", display_name: "Backup", provider: "minio" } },
    { label: "Workspace only", allImages: false },
  );

  await extension.__test__.saveRoom();

  assert.equal(quickPickCalls.length >= 3, true);
  assert.deepEqual(quickPickCalls[1].items.map((item) => item.display_name || item.label), ["Archive", "Backup"]);
  assert.match(spawnHarness.calls.at(-1).args.join(" "), /--dimension backup/);
});

test("startup refuses to mark a copied workspace as Saved when path binding is stale", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-path-test-"));
  writeMarker(root, { dimension_id: "archive", project_id: "demo-room", display_name: "Demo Room" });
  const { vscode, statusItem, inputBoxCalls, openExternalCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "status") {
      return {
        stdout: JSON.stringify({
          ok: false,
          path_matches: false,
          fingerprint_matches: true,
          state: "changed",
          current_workspace_fingerprint: "a".repeat(64),
          saved_workspace_fingerprint: "a".repeat(64),
        }),
      };
    }
    return { stdout: JSON.stringify({ ok: true }) };
  });

  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  await extension.__test__.startDirtyTracking({ subscriptions: [] });

  assert.match(statusItem.text, /Save/);
  assert.doesNotMatch(statusItem.text, /Saved/);
});

test("startup refuses to mark a workspace Saved when authoritative status is not clean", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-status-test-"));
  writeMarker(root, { dimension_id: "archive", project_id: "demo-room", display_name: "Demo Room" });
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "status") {
      return {
        stdout: JSON.stringify({
          ok: true,
          path_matches: true,
          fingerprint_matches: true,
          state: "changed",
          current_workspace_fingerprint: "a".repeat(64),
          saved_workspace_fingerprint: "a".repeat(64),
        }),
      };
    }
    return { stdout: JSON.stringify({ ok: true }) };
  });

  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  await extension.__test__.startDirtyTracking({ subscriptions: [] });

  assert.match(statusItem.text, /Save/);
  assert.doesNotMatch(statusItem.text, /Saved/);
});

test("startup queues workspace changes that happen before the first authoritative status returns", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-race-test-"));
  const changed = path.join(root, "src.txt");
  fs.writeFileSync(changed, "before\n");
  writeMarker(root, { dimension_id: "archive", project_id: "demo-room", display_name: "Demo Room" });
  const { vscode, statusItem, watcherCallbacks } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args, child, calls }) => {
    if (args[0] === "status") {
      const statusCount = spawnHarness.calls.filter((entry) => entry.args[0] === "status").length;
      if (statusCount === 1) {
        child.statusPayload = {
          stdout: JSON.stringify({
            ok: true,
            path_matches: true,
            fingerprint_matches: true,
            state: "clean",
            current_workspace_fingerprint: "a".repeat(64),
            saved_workspace_fingerprint: "a".repeat(64),
          }),
        };
        return { autoClose: false };
      }
      return {
        stdout: JSON.stringify({
          ok: false,
          path_matches: true,
          fingerprint_matches: false,
          state: "changed",
          current_workspace_fingerprint: "b".repeat(64),
          saved_workspace_fingerprint: "a".repeat(64),
        }),
      };
    }
    return { stdout: JSON.stringify({ ok: true }) };
  });

  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const started = extension.__test__.startDirtyTracking({ subscriptions: [] });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(watcherCallbacks.length > 0, true);
  fs.writeFileSync(changed, "after\n");
  watcherCallbacks[0].didChange({ fsPath: changed });
  const statusCall = spawnHarness.calls.find((entry) => entry.args[0] === "status");
  statusCall.child.closeWith(statusCall.child.statusPayload);
  await started;

  assert.match(statusItem.text, /Save/);
  assert.doesNotMatch(statusItem.text, /Saved/);
});

function catalogResponse() {
  return {
    stdout: JSON.stringify({
      ok: true,
      dimensions: [{
        id: "trusted-dimension",
        display_name: "Trusted Dimension",
        provider: "r2",
        projects: [{
          id: "trusted-room",
          display_name: "Trusted Room",
        }],
      }],
    }),
  };
}

function snapshotResponse() {
  return {
    stdout: JSON.stringify({
      ok: true,
      latest: "new-snapshot",
      snapshots: [
        { snapshot_id: "new-snapshot", created_at: "2026-08-26T12:00:00Z" },
        { snapshot_id: "old-snapshot", created_at: "2026-08-25T12:00:00Z" },
      ],
    }),
  };
}

test("Link and Repair use an explicit trusted Dimension, Room, and JAT instead of a stale v2 marker", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-trusted-link-test-"));
  writeMarker(root, {
    dimension_id: "stale-dimension",
    project_id: "stale-room",
    snapshot_id: "stale-snapshot",
    display_name: "Stale Room",
  });
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return catalogResponse();
    if (args[0] === "snapshots") return snapshotResponse();
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.quickPickResponses.push({
    project: { id: "trusted-room", display_name: "Trusted Room", dimension_id: "trusted-dimension" },
    snapshot: { snapshot_id: "old-snapshot" },
    snapshotId: "old-snapshot",
    dimension_id: "trusted-dimension",
  });
  await extension.__test__.linkRoom();
  vscode.quickPickResponses.push({
    project: { id: "trusted-room", display_name: "Trusted Room", dimension_id: "trusted-dimension" },
    snapshot: { snapshot_id: "new-snapshot" },
    snapshotId: "new-snapshot",
    dimension_id: "trusted-dimension",
  });
  await extension.__test__.repairRoom();

  const operations = spawnHarness.calls.filter((entry) => ["link", "repair"].includes(entry.args[0]));
  assert.equal(operations.length, 2);
  for (const operation of operations) {
    assert.deepEqual(
      [operation.args[operation.args.indexOf("--dimension") + 1], operation.args[operation.args.indexOf("--project") + 1]],
      ["trusted-dimension", "trusted-room"],
    );
    assert.notEqual(operation.args[operation.args.indexOf("--snapshot") + 1], "stale-snapshot");
  }
});

test("clicking a historical JAT and pressing Enter hydrates that exact old snapshot", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-historical-jat-test-"));
  writeMarker(root, { snapshot_id: "new-snapshot" });
  const { vscode, statusItem, executeCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return catalogResponse();
    if (args[0] === "snapshots") return snapshotResponse();
    if (args[0] === "hydrate") return { stdout: JSON.stringify({ ok: true }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  await extension.__test__.enterRoom({
    kind: "jat",
    id: "old-snapshot",
    snapshot: { snapshot_id: "old-snapshot" },
    project: { id: "trusted-room", display_name: "Trusted Room" },
    dimension: { id: "trusted-dimension", display_name: "Trusted Dimension" },
  });

  const hydrate = spawnHarness.calls.find((entry) => entry.args[0] === "hydrate");
  assert.ok(hydrate);
  assert.equal(hydrate.args[hydrate.args.indexOf("--snapshot") + 1], "old-snapshot");
  assert.equal(executeCalls.some(([name]) => name === "vscode.openFolder"), true);
});

test("historical JAT Enter does not treat the same Room's newer snapshot as already open", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-same-room-historical-jat-test-"));
  writeMarker(root, {
    dimension_id: "trusted-dimension",
    project_id: "trusted-room",
    snapshot_id: "new-snapshot",
  });
  const { vscode, statusItem, executeCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return catalogResponse();
    if (args[0] === "snapshots") return snapshotResponse();
    if (args[0] === "hydrate") return { stdout: JSON.stringify({ ok: true }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  await extension.__test__.enterRoom({
    kind: "jat",
    id: "old-snapshot",
    snapshot_id: "old-snapshot",
    snapshot: { snapshot_id: "old-snapshot" },
    project: { id: "trusted-room", display_name: "Trusted Room" },
    dimension: { id: "trusted-dimension", display_name: "Trusted Dimension" },
  });

  const hydrate = spawnHarness.calls.find((entry) => entry.args[0] === "hydrate");
  assert.ok(hydrate);
  assert.equal(hydrate.args[hydrate.args.indexOf("--snapshot") + 1], "old-snapshot");
  assert.equal(executeCalls.some(([name]) => name === "vscode.openFolder"), true);
});

test("historical JAT Enter rejects a destination linked to a newer snapshot", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-destination-historical-jat-test-"));
  const destination = path.join(root, "trusted-room");
  fs.mkdirSync(destination);
  writeMarker(destination, {
    dimension_id: "trusted-dimension",
    project_id: "trusted-room",
    snapshot_id: "new-snapshot",
  });
  const { vscode, statusItem, executeCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return catalogResponse();
    if (args[0] === "snapshots") return snapshotResponse();
    if (args[0] === "hydrate") return { stdout: JSON.stringify({ ok: true }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  await assert.rejects(
    extension.__test__.enterRoom({
      kind: "jat",
      id: "old-snapshot",
      snapshot_id: "old-snapshot",
      snapshot: { snapshot_id: "old-snapshot" },
      project: { id: "trusted-room", display_name: "Trusted Room" },
      dimension: { id: "trusted-dimension", display_name: "Trusted Dimension" },
    }),
    /unexplained existing folder/,
  );
  assert.equal(executeCalls.some(([name]) => name === "vscode.openFolder"), false);
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "hydrate"), false);
});

test("a v1 marker requires explicit selection for Serve instead of guessing a Dimension", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-v1-serve-test-"));
  fs.writeFileSync(path.join(root, ".josh-room.json"), JSON.stringify({
    format_version: 1,
    project_id: "same-room",
    display_name: "Legacy Room",
  }));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") {
      return { stdout: JSON.stringify({ dimensions: [
        { id: "archive", display_name: "Archive", provider: "r2", projects: [{ id: "same-room", display_name: "Legacy Room" }] },
        { id: "backup", display_name: "Backup", provider: "minio", projects: [{ id: "same-room", display_name: "Legacy Room" }] },
      ] }) };
    }
    if (args[0] === "snapshots") return snapshotResponse();
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  assert.equal(extension.__test__.roomLabel({
    id: "same-room", display_name: "Legacy Room", dimension_id: "archive",
  }), "Legacy Room · archive");
  const projects = [
    { id: "same-room", display_name: "Legacy Room", dimension_id: "archive" },
    { id: "same-room", display_name: "Legacy Room", dimension_id: "backup" },
  ];
  vscode.quickPickResponses.push({ project: projects[1] });
  const selected = await extension.__test__.chooseServeProject(projects, {
    format_version: 1,
    project_id: "same-room",
    display_name: "Legacy Room",
  });
  assert.equal(selected.dimension_id, "backup");
});

test("same-named Dimensions produce stable command-palette Room labels", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-same-named-dimensions-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const extension = loadExtension(vscode, createSpawnHarness(() => ({
    stdout: JSON.stringify({ ok: true }),
  })).spawn);
  extension.__test__.setStatusItem(statusItem);
  const rooms = flattenDimensionRooms({
    dimensions: [
      { id: "archive", display_name: "Shared", projects: [{ id: "same-room", display_name: "Same Room" }] },
      { id: "backup", display_name: "Shared", projects: [{ id: "same-room", display_name: "Same Room" }] },
    ],
  });
  const labels = rooms.map((room) => extension.__test__.roomLabel(room));
  assert.deepEqual(labels, ["Same Room · Shared (archive)", "Same Room · Shared (backup)"]);
});

test("status JSON remains consumable when the status child exits with code 2", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-status-exit-test-"));
  const { vscode } = createVscodeMock(root);
  const payload = {
    ok: false,
    state: "changed",
    path_matches: true,
    fingerprint_matches: false,
  };
  const extension = loadExtension(vscode, createSpawnHarness(() => ({
    stdout: JSON.stringify(payload),
    code: 2,
  })).spawn);
  assert.deepEqual(await extension.__test__.runJoshRoom(["status"], root), payload);
});

test("startup seeds dirtyBuffers from an already-open dirty editor", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-open-editor-test-"));
  const changed = path.join(root, "already-open.txt");
  fs.writeFileSync(changed, "saved\n");
  writeMarker(root);
  const { vscode, statusItem } = createVscodeMock(root, [{
    uri: { fsPath: changed },
    isDirty: true,
  }]);
  const extension = loadExtension(vscode, createSpawnHarness(({ args }) => {
    if (args[0] === "status") {
      return { stdout: JSON.stringify({
        ok: true,
        path_matches: true,
        fingerprint_matches: true,
        state: "clean",
        current_workspace_fingerprint: "a".repeat(64),
        saved_workspace_fingerprint: "a".repeat(64),
      }) };
    }
    return { stdout: JSON.stringify({ ok: true }) };
  }).spawn);
  extension.__test__.setStatusItem(statusItem);
  await extension.__test__.startDirtyTracking({ subscriptions: [] });
  assert.match(statusItem.text, /Save/);
});

test("a failed Dimension loader leaves the healthy Dimension usable", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-dimension-loader-test-"));
  const { vscode } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return { stdout: JSON.stringify({ ok: true, dimensions: [
      { id: "unavailable", display_name: "Unavailable", provider: "r2" },
      { id: "healthy", display_name: "Healthy", provider: "minio" },
    ] }) };
    if (args[0] === "projects" && args.includes("unavailable")) return { stderr: "unavailable", code: 1 };
    if (args[0] === "projects") return { stdout: JSON.stringify({ ok: true,
      dimension_id: "healthy",
      projects: [{ id: "healthy-room", display_name: "Healthy Room" }],
    }) };
    if (args[0] === "snapshots") return { stdout: JSON.stringify({ ok: true, latest: "healthy-jat", snapshots: [{ snapshot_id: "healthy-jat" }] }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(vscode.window.createStatusBarItem());
  const catalog = await extension.__test__.loadCatalog(root, "Loading test catalog");
  assert.deepEqual(catalog.dimensions.map((dimension) => dimension.id), ["unavailable", "healthy"]);
  assert.deepEqual(catalog.projects.map((project) => project.id), ["healthy-room"]);
});

test("a minimal legacy catalog keeps its top-level Rooms in the native loader", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-minimal-catalog-test-"));
  const { vscode } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return { stdout: JSON.stringify({
      ok: true,
      dimension_id: "archive",
      dimension_name: "Archive",
      provider: "r2",
      projects: [{ id: "legacy-room", display_name: "Legacy Room" }],
    }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(vscode.window.createStatusBarItem());

  const catalog = await extension.__test__.loadCatalog(root, "Loading minimal catalog");

  assert.deepEqual(catalog.projects.map((project) => project.id), ["legacy-room"]);
  assert.equal(catalog.projects[0].dimension_id, "archive");
});

test("direct handleDrop parses a CRLF text/uri-list folder drop", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-drop-test-"));
  const source = path.join(root, "folder with spaces");
  fs.mkdirSync(source);
  const { vscode } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "snapshot") return { stdout: JSON.stringify({ ok: true }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(vscode.window.createStatusBarItem());
  vscode.inputBoxResponses.push("destination-room");
  const drop = new extension.__test__.RoomDragAndDropController();
  await drop.handleDrop(
    { kind: "dimension", id: "archive", dimension: { id: "archive" } },
    { get(type) {
      return type === "text/uri-list"
        ? { asString: async () => `file://${encodeURI(source)}\r\n\r\n` }
        : undefined;
    } },
  );
  const copy = spawnHarness.calls.find((entry) => entry.args[0] === "snapshot");
  assert.ok(copy);
  assert.equal(copy.args[copy.args.indexOf("--source-folder") + 1], source);
});

test("deleting the active JAT is blocked so its marker cannot remain Saved", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-active-delete-test-"));
  writeMarker(root, { dimension_id: "archive", project_id: "room", snapshot_id: "active" });
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return { stdout: JSON.stringify({ ok: true, dimensions: [{
      id: "archive", display_name: "Archive", projects: [{ id: "room", display_name: "Room" }],
    }] }) };
    if (args[0] === "snapshots") return { stdout: JSON.stringify({ latest: "active", snapshots: [{ snapshot_id: "active" }] }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const result = await extension.__test__.removeSnapshot({
    kind: "jat",
    id: "active",
    project: { id: "room", display_name: "Room" },
    dimension: { id: "archive", display_name: "Archive" },
    snapshot: { snapshot_id: "active" },
  });
  assert.equal(result, "blocked");
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "snapshots" && entry.args[1] === "remove"), false);
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, ".josh-room.json"))).snapshot_id, "active");
});

test("fresh configured R2 shows Connect Cloudflare without probing an empty catalog", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-r2-connect-state-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return { stdout: JSON.stringify({
      ok: true,
      dimensions: [{ id: "r2", display_name: "Default", provider: "r2" }],
    }) };
    if (args[0] === "auth" && args[1] === "status") return { stdout: JSON.stringify({
      ok: true, state: "missing",
    }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  const catalog = await extension.__test__.loadCatalog(root, "Loading storage");
  const tree = buildProviderTree(catalog);
  const dimension = tree[0].children[0];
  const connection = dimension.children[0];
  const treeProvider = new extension.__test__.HierarchyRoomsProvider();
  const connectionItem = treeProvider.getTreeItem(connection);

  assert.equal(tree[0].label, "Cloudflare R2");
  assert.equal(dimension.label, "Default");
  assert.equal(connection.label, "⚠ Not connected");
  assert.equal(connection.description, "Connect Cloudflare");
  assert.equal(connectionItem.command.command, "joshRoom.connectCloudflare");
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "projects"), false);
});

test("Add Storage sends Cloudflare directly to OAuth without R2 questionnaires", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-add-storage-r2-test-"));
  const { vscode, statusItem, inputBoxCalls, openExternalCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return { stdout: JSON.stringify({
      ok: true,
      dimensions: [{ id: "r2", display_name: "Default", provider: "r2" }],
    }) };
    if (args[0] === "auth" && args[1] === "status") return { stdout: JSON.stringify({ ok: true, state: "missing" }) };
    if (args[0] === "auth" && args[1] === "start") return { stdout: JSON.stringify({
      ok: true,
      session_id: "session-1",
      authorization_url: "https://dash.cloudflare.example/oauth",
    }) };
    if (args[0] === "auth" && args[1] === "poll") return { stdout: JSON.stringify({ ok: true, status: "authorized" }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.quickPickResponses.push({ label: "Cloudflare R2", provider: "r2" });

  assert.equal(await extension.__test__.addStorage(), "connected");
  assert.equal(inputBoxCalls.length, 0);
  assert.deepEqual(openExternalCalls.map((uri) => uri.value), ["https://dash.cloudflare.example/oauth"]);
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "dimensions" && entry.args[1] === "add"), false);
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "auth" && entry.args[1] === "start"), true);
});

test("Cloudflare authorization refreshes the hierarchy and loads Rooms and JATs", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-r2-authorized-test-"));
  const { vscode, statusItem, openExternalCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "auth" && args[1] === "start") return { stdout: JSON.stringify({
      ok: true,
      session_id: "session-2",
      authorization_url: "https://dash.cloudflare.example/oauth-2",
    }) };
    if (args[0] === "auth" && args[1] === "poll") return { stdout: JSON.stringify({ ok: true, status: "authorized" }) };
    if (args[0] === "dimensions") return { stdout: JSON.stringify({
      ok: true,
      dimensions: [{ id: "r2", display_name: "Default", provider: "r2" }],
    }) };
    if (args[0] === "auth" && args[1] === "status") return { stdout: JSON.stringify({ ok: true, state: "connected" }) };
    if (args[0] === "projects") return { stdout: JSON.stringify({
      ok: true,
      dimension_id: "r2",
      projects: [{ id: "room-a", display_name: "Room A" }],
    }) };
    if (args[0] === "snapshots") return { stdout: JSON.stringify({
      ok: true,
      dimension_id: "r2",
      latest: "jat-a",
      snapshots: [{ snapshot_id: "jat-a", display_name: "JAT A" }],
    }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const provider = new extension.__test__.HierarchyRoomsProvider();
  extension.__test__.setRoomsProvider(provider);

  assert.equal(await extension.__test__.connectCloudflare(
    { kind: "connection", state: "missing", dimension: { id: "r2", provider: "r2" } },
    { pollIntervalMs: 0 },
  ), "connected");
  const connection = provider.roots[0].children[0].children[0];

  assert.equal(connection.label, "✓ Connected");
  assert.equal(connection.children[0].label, "Room A · Default");
  assert.equal(connection.children[0].children[0].label, "JAT A");
  assert.equal(openExternalCalls[0].value, "https://dash.cloudflare.example/oauth-2");
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "projects"), true);
});

test("expired Cloudflare authority offers Reconnect Cloudflare instead of an empty Room", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-r2-expired-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return { stdout: JSON.stringify({
      ok: true,
      dimensions: [{ id: "r2", display_name: "Default", provider: "r2" }],
    }) };
    if (args[0] === "auth" && args[1] === "status") return { stdout: JSON.stringify({ ok: true, state: "expired" }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const catalog = await extension.__test__.loadCatalog(root, "Loading storage");
  const connection = buildProviderTree(catalog)[0].children[0].children[0];
  const treeItem = new extension.__test__.HierarchyRoomsProvider().getTreeItem(connection);

  assert.equal(connection.label, "⚠ Session expired");
  assert.equal(connection.description, "Reconnect Cloudflare");
  assert.equal(treeItem.command.command, "joshRoom.reconnectCloudflare");
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "projects"), false);
});

test("unavailable Cloudflare authority status fails closed without probing R2", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-r2-status-error-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return { stdout: JSON.stringify({
      ok: true,
      dimensions: [{ id: "r2", display_name: "Default", provider: "r2" }],
    }) };
    if (args[0] === "auth" && args[1] === "status") return {
      stdout: JSON.stringify({ ok: false, error: "auth status unavailable" }),
      code: 1,
    };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  const catalog = await extension.__test__.loadCatalog(root, "Loading storage");
  const connection = buildProviderTree(catalog)[0].children[0].children[0];

  assert.equal(connection.label, "⚠ Not connected");
  assert.equal(connection.description, "Connect Cloudflare");
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "projects"), false);
});

test("MinIO Add Storage asks for concrete settings without invoking Cloudflare OAuth", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-add-storage-minio-test-"));
  const { vscode, statusItem, inputBoxCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions" && args[1] === "add") return { stdout: JSON.stringify({ ok: true }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.quickPickResponses.push({ label: "MinIO", provider: "minio" });
  vscode.inputBoxResponses.push("Homelab", "https://minio.example", "rooms", "homelab-profile");

  assert.equal(await extension.__test__.addStorage(), "added");
  assert.deepEqual(inputBoxCalls.map((options) => options.prompt), [
    "Friendly Dimension name",
    "Endpoint URL",
    "Bucket or object-store namespace",
    "Existing host keyring credential profile (name only)",
  ]);
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "auth"), false);
  const addCall = spawnHarness.calls.find((entry) => entry.args[0] === "dimensions" && entry.args[1] === "add");
  assert.ok(addCall);
  assert.equal(addCall.args[addCall.args.indexOf("--provider") + 1], "minio");
});

test("native storage commands are understandable, distinct, and omit Use Dimension", () => {
  const packageJson = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
  const commands = packageJson.contributes.commands;
  const byId = new Map(commands.map((command) => [command.command, command]));
  assert.equal(byId.has("joshRoom.addDimension"), false);
  assert.equal(byId.has("joshRoom.openDimension"), false);
  assert.equal(byId.get("joshRoom.addStorage").title, "Josh: Connect Storage");
  assert.equal(byId.get("joshRoom.addStorage").icon, "$(cloud)");
  assert.equal(byId.get("joshRoom.save").icon, "$(save-as)");
  assert.equal(byId.get("joshRoom.refresh").icon, "$(refresh)");
  assert.equal(new Set([byId.get("joshRoom.addStorage").icon, byId.get("joshRoom.save").icon, byId.get("joshRoom.refresh").icon]).size, 3);
  const extension = fs.readFileSync(path.join(__dirname, "extension.js"), "utf8");
  assert.doesNotMatch(extension, /Use Dimension|Open Dimension|Add Dimension|Edit non-secret/);
  assert.match(extension, /vscode\.env\.openExternal\(vscode\.Uri\.parse\(authorizationUrl\)\)/);
  assert.doesNotMatch(extension, /webbrowser\.open/);
});
