const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { EventEmitter } = require("node:events");
const Module = require("node:module");

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

function createVscodeMock(workspaceFolder) {
  const quickPickCalls = [];
  const openDialogCalls = [];
  const inputBoxCalls = [];
  const infoCalls = [];
  const warningCalls = [];
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
      Uri: { file: (fsPath) => ({ fsPath }) },
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
      env: { clipboard: { writeText: async () => {} } },
      workspace: {
        workspaceFolders: workspaceFolder ? [{ uri: { fsPath: workspaceFolder } }] : [],
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
  const { vscode, statusItem } = createVscodeMock(root);
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
