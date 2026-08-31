const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const { EventEmitter } = require("node:events");
const Module = require("node:module");
const { buildProviderTree, flattenDimensionRooms } = require("./registry");
const managedRuntime = require("./runtime");

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
    child.stdin = new EventEmitter();
    child.stdin.end = (value) => { child.stdinPayload = value; };
    child.stdout = new EventEmitter();
    child.stderr = new EventEmitter();
    child.kill = (signal = "SIGTERM") => {
      child.killed = signal;
      if (!child.closed) {
        child.closed = true;
        child.emit("close", null, signal);
      }
    };
    child.closeWith = ({ stdout = "", stderr = "", code = 0 } = {}) => {
      if (child.closed) return;
      if (stdout) child.stdout.emit("data", Buffer.from(stdout));
      if (stderr) child.stderr.emit("data", Buffer.from(stderr));
      child.closed = true;
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
  const progressCalls = [];
  const logLines = [];
  const treeViewCalls = [];
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
    appendLine(line) { logLines.push(line); },
    show() { outputChannel.shown = true; },
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
        withProgress: async (options, task) => {
          const listeners = [];
          const token = {
            isCancellationRequested: false,
            onCancellationRequested(callback) {
              listeners.push(callback);
              return { dispose: () => listeners.splice(listeners.indexOf(callback), 1) };
            },
            cancel() {
              if (token.isCancellationRequested) return;
              token.isCancellationRequested = true;
              for (const callback of [...listeners]) callback();
            },
          };
          progressCalls.push({ options, token });
          return task({ report() {} }, token);
        },
        createStatusBarItem: () => statusItem,
        createOutputChannel: () => outputChannel,
        createTreeView: (id, options) => {
          treeViewCalls.push({ id, options });
          return { dispose() {} };
        },
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
    progressCalls,
    logLines,
    treeViewCalls,
  };
}

function loadExtension(vscodeMock, spawnMock, registryMock) {
  const originalLoad = Module._load;
  const targets = [
    require.resolve("./extension"),
    require.resolve("./dirty"),
    require.resolve("./registry"),
    require.resolve("./progress"),
    require.resolve("./runtime"),
  ];
  for (const target of targets) delete require.cache[target];
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "vscode") return vscodeMock;
    if (request === "child_process") return { spawn: spawnMock };
    if (request === "./registry" && registryMock) {
      return { ...originalLoad(request, parent, isMain), ...registryMock };
    }
    return originalLoad(request, parent, isMain);
  };
  try {
    const extension = require("./extension");
    extension.__test__.setRuntimeForTests({
      command: "/test/managed-rcc",
      args: (args) => [...args, "--json"],
      env: {},
      jatRoot: path.join(os.tmpdir(), "josh-room-test-jat"),
    });
    return extension;
  } finally {
    Module._load = originalLoad;
  }
}

function loadExtensionWithRealProcesses(vscodeMock) {
  const originalLoad = Module._load;
  const targets = [require.resolve("./extension"), require.resolve("./runtime")];
  for (const target of targets) delete require.cache[target];
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "vscode") return vscodeMock;
    return originalLoad(request, parent, isMain);
  };
  try {
    return require("./extension");
  } finally {
    Module._load = originalLoad;
  }
}

test("extension backend commands use the managed RCC controller boundary", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-managed-command-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({ stdout: JSON.stringify({ ok: true }) }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  extension.__test__.setRuntimeForTests({
    command: "/private/runtime/rcc",
    args: (args) => ["run", "-r", "/private/controller/robot.yaml", "-t", "Josh Room", "--", ...args, "--json"],
    env: { ROBOCORP_HOME: "/private/runtime/robocorp", RCC_HOLOTREE_MODE: "private" },
  });

  await extension.__test__.runJoshRoom(["status"], root);

  assert.equal(spawnHarness.calls[0].command, "/private/runtime/rcc");
  assert.deepEqual(spawnHarness.calls[0].args.slice(0, 6), [
    "run", "-r", "/private/controller/robot.yaml", "-t", "Josh Room", "--",
  ]);
  assert.equal(spawnHarness.calls[0].options.env.ROBOCORP_HOME, "/private/runtime/robocorp");
  assert.equal(spawnHarness.calls[0].options.env.RCC_HOLOTREE_MODE, "private");
  assert.notEqual(spawnHarness.calls[0].command, "josh-room");
});

test("Windows terminal launch passes environment through terminal options", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-windows-terminal-test-"));
  const { vscode } = createVscodeMock(root);
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  const launch = extension.__test__.buildTerminalLaunch(
    {
      command: "C:\\Program Files\\Josh Room\\rcc.exe",
      args: (args) => ["--no-build", "env", "exec", "--", ...args],
    },
    ["jat", "serve", "--haul", "C:\\Users\\Josh\\Room Files\\images.tar.zst"],
    {
      JOSH_ROOM_EXTENSION_MODE: "1",
      JOSH_ROOM_PROGRESS_FILE: "C:\\Users\\Josh\\AppData\\Local\\progress.jsonl",
      PATH: "C:\\managed;C:\\Windows\\System32",
    },
    "win32",
  );

  assert.doesNotMatch(launch.command, /JOSH_ROOM_EXTENSION_MODE=/);
  assert.doesNotMatch(launch.command, /JOSH_ROOM_PROGRESS_FILE=/);
  assert.match(launch.command, /^"C:\\Program Files\\Josh Room\\rcc\.exe"/);
  assert.equal(launch.environment.JOSH_ROOM_EXTENSION_MODE, "1");
  assert.equal(launch.environment.PATH, "C:\\managed;C:\\Windows\\System32");
});

test("restore destination names reject Windows separators, drives, UNC paths, and dot segments", () => {
  const { vscode } = createVscodeMock(os.tmpdir());
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  const safe = extension.__test__.isSafeRestoreName;
  assert.equal(safe("room-01"), true);
  for (const value of ["..", ".", "..\\outside", ".. /outside", "C:\\outside", "C:/outside", "\\\\server\\share", "/outside", "room/name", "room\\name", "room\0name"]) {
    assert.equal(safe(value), false, value);
  }
});

test("Windows child cancellation terminates the child directly", () => {
  const { vscode } = createVscodeMock(os.tmpdir());
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  const originalKill = process.kill;
  let killed;
  process.kill = () => { throw new Error("negative process-group kill is forbidden on Windows"); };
  try {
    extension.__test__.terminateChild({ pid: 123, kill: (signal) => { killed = signal; } }, "win32");
  } finally {
    process.kill = originalKill;
  }
  assert.equal(killed, "SIGTERM");
});

test("JAT runtime acquisition is lazy and limited to JAT-backed operations", () => {
  const { vscode } = createVscodeMock(os.tmpdir());
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  const needsJat = extension.__test__.operationNeedsJat;
  assert.equal(needsJat(["dimensions", "list"]), false);
  assert.equal(needsJat(["auth", "status"]), false);
  assert.equal(needsJat(["status"]), false);
  assert.equal(needsJat(["snapshot", "create"]), true);
  assert.equal(needsJat(["hydrate"]), true);
  assert.equal(needsJat(["serve"]), true);
  assert.equal(needsJat(["jat", "build"]), true);
  assert.equal(needsJat(["doctor"]), true);
});

test("initial runtime readiness acquires controller and defers JAT", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-lazy-jat-startup-"));
  const { vscode } = createVscodeMock(root);
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  const liveRuntime = require("./runtime");
  const originals = {
    readManifest: liveRuntime.readManifest,
    ensureManagedRcc: liveRuntime.ensureManagedRcc,
    ensureControllerRuntime: liveRuntime.ensureControllerRuntime,
    ensureJatRuntime: liveRuntime.ensureJatRuntime,
  };
  let controllerCalls = 0;
  let jatCalls = 0;
  liveRuntime.readManifest = () => ({
    schema_version: 1,
    extension_version: "test",
    rcc: { version: "v18.19.3", platforms: {} },
    controller: { robot: "runtime/controller/robot.yaml" },
    jat: { git_sha: "a".repeat(40) },
  });
  liveRuntime.ensureManagedRcc = async () => ({ executable: "/managed/rcc", version: "v18.19.3" });
  liveRuntime.ensureControllerRuntime = async () => {
    controllerCalls += 1;
    return { artifact: "sha256:" + "b".repeat(64) };
  };
  liveRuntime.ensureJatRuntime = async () => {
    jatCalls += 1;
    return { artifact: "sha256:" + "c".repeat(64) };
  };
  try {
    const state = await extension.__test__.initializeManagedRuntime(
      { globalStorageUri: { fsPath: root }, extensionPath: root },
      { event() {} },
    );
    assert.equal(controllerCalls, 1);
    assert.equal(jatCalls, 0);
    assert.equal(state.jat, undefined);
  } finally {
    Object.assign(liveRuntime, originals);
  }
});

test("extension consumes the private controller result receipt when RCC suppresses stdout", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-result-receipt-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ options }) => {
    fs.writeFileSync(options.env.JOSH_ROOM_RESULT_FILE, '{"ok":true,"operation":"status"}');
    return { stdout: "" };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  const result = await extension.__test__.runJoshRoom(["status"], root);

  assert.deepEqual(result, { ok: true, operation: "status" });
});

test("MinIO credentials use VS Code SecretStorage and stay out of controller argv", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-secretstorage-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const values = new Map();
  const secrets = {
    get: async (key) => values.get(key),
    store: async (key, value) => values.set(key, value),
  };
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "list") {
      return { stdout: JSON.stringify({ ok: true, connections: [] }) };
    }
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "create") {
      return { stdout: JSON.stringify({ ok: true, connection: { id: "secret-minio", provider: "minio", credential_profile: "secret-profile" } }) };
    }
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "list") {
      return { stdout: JSON.stringify({ ok: true, buckets: ["secret-room"] }) };
    }
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "check") {
      return { stdout: JSON.stringify({ ok: true, accessible: true }) };
    }
    if (args[0] === "dimensions" && args[1] === "add") return { stdout: JSON.stringify({ ok: true }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  extension.__test__.setExtensionContextForTests({ secrets });
  vscode.quickPickResponses.push(
    { label: "MinIO", provider: "minio" },
    { label: "secret-room", bucket: "secret-room" },
  );
  vscode.inputBoxResponses.push("https://minio.example.invalid:9000", "secret-access", "secret-key");

  assert.equal(await extension.__test__.addStorage(), "added");
  const stored = JSON.parse(values.get("josh-room.credentials.v1"));
  assert.deepEqual(stored.profiles["secret-profile"], {
    "access-key-id": "secret-access",
    "secret-access-key": "secret-key",
  });
  for (const call of spawnHarness.calls) {
    assert.doesNotMatch(call.args.join(" "), /secret-access|secret-key/);
  }
  assert.match(String(spawnHarness.calls.find((call) => call.args[0] === "provider" && call.args[1] === "connection" && call.args[2] === "create").child.stdinPayload), /secret-access/);
});

test("real managed extension runner reaches the packaged controller", {
  skip: !process.env.JOSH_ROOM_MANAGED_RUNTIME_ROOT,
}, async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-real-runner-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const extension = loadExtensionWithRealProcesses(vscode);
  extension.__test__.setStatusItem(statusItem);
  extension.__test__.setExtensionContextForTests({
    extensionPath: __dirname,
    globalStorageUri: { fsPath: process.env.JOSH_ROOM_MANAGED_RUNTIME_ROOT },
    secrets: { get: async () => undefined },
  });
  extension.__test__.setRuntimeForTests(undefined);

  let result;
  try {
    result = await extension.__test__.runJoshRoom(["auth", "status"], root);
  } catch (error) {
    console.error(JSON.stringify({ message: error.message, command: error.command, args: error.args, resultPath: error.resultPath }));
    throw error;
  }

  assert.equal(result.ok, true);
  assert.equal(result.state, "missing");
});

test("real extension process uses an already resolved managed runtime", {
  skip: !process.env.JOSH_ROOM_MANAGED_RUNTIME_ROOT,
}, async () => {
  const storage = process.env.JOSH_ROOM_MANAGED_RUNTIME_ROOT;
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-real-process-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const extension = loadExtensionWithRealProcesses(vscode);
  const rcc = path.join(storage, "runtime/rcc/v18.19.2/linux-x64/rcc");
  const jatRoot = path.join(storage, "runtime/jat/096c5f3c5d735a67f41c4fabbf63e4af1aacadf1");
  const environment = managedRuntime.runtimeEnvironment({ globalStorageUri: { fsPath: storage } }, {
    rccExecutable: rcc,
    controllerRoot: path.join(__dirname, "runtime/controller"),
    jatRoot,
    jatArtifact: "sha256:0b5891d1ddc82ffa6dd8d0616b5b77693ec733709492307b2ef947ff0f8f34fb",
    jatSourceSha: "096c5f3c5d735a67f41c4fabbf63e4af1aacadf1",
  }, root);
  extension.__test__.setStatusItem(statusItem);
  extension.__test__.setRuntimeForTests({
    command: rcc,
    args: (args) => ["run", "--silent", "-r", path.join(__dirname, "runtime/controller/robot.yaml"), "-t", "Josh Room", "--", ...args, "--json"],
    env: environment,
    jatRoot,
  });

  const result = await extension.__test__.runJoshRoom(["auth", "status"], root);

  assert.equal(result.ok, true);
  assert.equal(result.state, "missing");
});

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
    if (args[0] === "auth" && args[1] === "status") {
      return { stdout: JSON.stringify({ ok: true, state: "connected", encryption_state: "connected" }) };
    }
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

test("Save Room to a fresh MinIO Dimension authorizes encryption once and preserves the target", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-save-minio-auth-test-"));
  const source = path.join(root, "source-room");
  fs.mkdirSync(source);
  const { vscode, statusItem, openExternalCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions" && args.includes("--with-hierarchy")) {
      return { stdout: JSON.stringify({ ok: true, dimensions: [
        { id: "backup", display_name: "Backup", provider: "minio", rooms: [] },
      ] }) };
    }
    if (args[0] === "dimensions") {
      return { stdout: JSON.stringify({ ok: true, dimensions: [
        { id: "backup", display_name: "Backup", provider: "minio" },
      ] }) };
    }
    if (args[0] === "auth" && args[1] === "status") {
      return { stdout: JSON.stringify({ ok: true, state: "missing", encryption_state: "missing", r2_state: "missing" }) };
    }
    if (args[0] === "auth" && args[1] === "start") {
      return { stdout: JSON.stringify({ ok: true, session_id: "encryption-session", authorization_url: "https://auth.example.invalid/encryption" }) };
    }
    if (args[0] === "auth" && args[1] === "wait") {
      return { stdout: JSON.stringify({ ok: true, status: "authorized" }) };
    }
    if (args[0] === "snapshot" && args[1] === "create") {
      return { stdout: JSON.stringify({ ok: true, project_id: "new-room", ciphertext_size: 1024 }) };
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

  assert.equal(await extension.__test__.saveRoom(), "saved");
  assert.deepEqual(openExternalCalls.map((uri) => uri.value), ["https://auth.example.invalid/encryption"]);
  const authStart = spawnHarness.calls.find((call) => call.args[0] === "auth" && call.args[1] === "start");
  const authWait = spawnHarness.calls.find((call) => call.args[0] === "auth" && call.args[1] === "wait");
  assert.ok(authStart.args.includes("--purpose") && authStart.args.includes("encryption"));
  assert.ok(authWait.args.includes("--purpose") && authWait.args.includes("encryption"));
  const snapshots = spawnHarness.calls.filter((call) => call.args[0] === "snapshot" && call.args[1] === "create");
  assert.equal(snapshots.length, 1);
  assert.match(snapshots[0].args.join(" "), /--dimension backup/);
  assert.equal(spawnHarness.calls.some((call) => call.args[0] === "auth" && call.args[1] === "start" && call.args.includes("r2")), false);
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
          snapshots: [
            { snapshot_id: "new-snapshot", created_at: "2026-08-26T12:00:00Z", workspace_fingerprint: "a".repeat(64) },
            { snapshot_id: "old-snapshot", created_at: "2026-08-25T12:00:00Z", workspace_fingerprint: "a".repeat(64) },
          ],
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
    project: { id: "trusted-room", display_name: "Trusted Room", dimension_id: "trusted-dimension", snapshots: [
      { snapshot_id: "new-snapshot", workspace_fingerprint: "a".repeat(64) },
      { snapshot_id: "old-snapshot", workspace_fingerprint: "a".repeat(64) },
    ] },
    snapshot: { snapshot_id: "old-snapshot" },
    snapshotId: "old-snapshot",
    dimension_id: "trusted-dimension",
  });
  await extension.__test__.linkRoom();
  vscode.quickPickResponses.push({
    project: { id: "trusted-room", display_name: "Trusted Room", dimension_id: "trusted-dimension", snapshots: [
      { snapshot_id: "new-snapshot", workspace_fingerprint: "a".repeat(64) },
      { snapshot_id: "old-snapshot", workspace_fingerprint: "a".repeat(64) },
    ] },
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

test("Link Existing Folder verifies a legacy JAT in isolation before binding", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-legacy-link-match-test-"));
  const content = path.join(root, "keep.txt");
  fs.writeFileSync(content, "keep this folder unchanged\n");
  const before = fs.readFileSync(content, "utf8");
  const { vscode, statusItem } = createVscodeMock(root);
  let verificationRoot;
  const linkCalls = [];
  const fingerprint = "a".repeat(64);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return { stdout: JSON.stringify({ ok: true, dimensions: [{
      id: "archive", display_name: "Archive", provider: "minio", projects: [{
        id: "legacy-room", display_name: "Legacy Room", snapshots: [{
          snapshot_id: "legacy-jat", display_name: "Legacy JAT", workspace_fingerprint: "0".repeat(64),
        }],
      }],
    }] }) };
    if (args[0] === "auth" && args[1] === "status") return { stdout: JSON.stringify({ ok: true, state: "missing" }) };
    if (args[0] === "hydrate") {
      verificationRoot = args[args.indexOf("--destination") + 1];
      return { stdout: JSON.stringify({ ok: true }) };
    }
    if (args[0] === "status") return { stdout: JSON.stringify({
      ok: true, path_matches: true, state: "clean", current_workspace_fingerprint: fingerprint,
    }) };
    if (args[0] === "link") {
      linkCalls.push(args);
      return { stdout: JSON.stringify({ ok: true, workspace_fingerprint: fingerprint }) };
    }
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  assert.equal(await extension.__test__.linkRoom({
    kind: "jat",
    project: { id: "legacy-room", display_name: "Legacy Room", dimension_id: "archive" },
    snapshot: { snapshot_id: "legacy-jat" },
    snapshotId: "legacy-jat",
    dimension_id: "archive",
  }), "linked");
  assert.equal(linkCalls.length, 1);
  assert.equal(linkCalls[0][linkCalls[0].indexOf("--workspace-fingerprint") + 1], fingerprint);
  assert.equal(fs.readFileSync(content, "utf8"), before);
  assert.equal(fs.existsSync(verificationRoot), false);
});

test("legacy Link Existing Folder rejects a mismatch without touching the folder", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-legacy-link-mismatch-test-"));
  const content = path.join(root, "keep.txt");
  fs.writeFileSync(content, "keep this folder unchanged\n");
  const before = fs.readFileSync(content, "utf8");
  const { vscode, statusItem, warningCalls } = createVscodeMock(root);
  let verificationRoot;
  let linkCalls = 0;
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions") return { stdout: JSON.stringify({ ok: true, dimensions: [{
      id: "archive", display_name: "Archive", provider: "minio", projects: [{
        id: "legacy-room", display_name: "Legacy Room", snapshots: [{
          snapshot_id: "legacy-jat", display_name: "Legacy JAT", workspace_fingerprint: "0".repeat(64),
        }],
      }],
    }] }) };
    if (args[0] === "auth" && args[1] === "status") return { stdout: JSON.stringify({ ok: true, state: "missing" }) };
    if (args[0] === "hydrate") {
      verificationRoot = args[args.indexOf("--destination") + 1];
      return { stdout: JSON.stringify({ ok: true }) };
    }
    if (args[0] === "status") {
      const workspace = args[args.indexOf("--workspace") + 1];
      return { stdout: JSON.stringify({
        ok: true, path_matches: true, state: "clean",
        current_workspace_fingerprint: workspace === root ? "a".repeat(64) : "b".repeat(64),
      }) };
    }
    if (args[0] === "link") {
      linkCalls += 1;
      return { stdout: JSON.stringify({ ok: true }) };
    }
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  assert.equal(await extension.__test__.linkRoom({
    kind: "jat",
    project: { id: "legacy-room", display_name: "Legacy Room", dimension_id: "archive" },
    snapshot: { snapshot_id: "legacy-jat" },
    snapshotId: "legacy-jat",
    dimension_id: "archive",
  }), "mismatch");
  assert.equal(linkCalls, 0);
  assert.equal(fs.readFileSync(content, "utf8"), before);
  assert.equal(fs.existsSync(verificationRoot), false);
  assert.match(warningCalls[0][0], /does not match.*Enter.*save this folder as a new Room/);
  assert.deepEqual(warningCalls[0].slice(1), ["Enter", "Save as New"]);
});

test("clicking a historical JAT and pressing Enter hydrates that exact old snapshot", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-historical-jat-test-"));
  writeMarker(root, { snapshot_id: "new-snapshot" });
  const previousRoot = process.env.JOSH_ROOM_WORKSPACE_ROOT;
  const previousInstance = process.env.JOSH_ROOM_INSTANCE;
  process.env.JOSH_ROOM_WORKSPACE_ROOT = root;
  process.env.JOSH_ROOM_INSTANCE = path.join(root, "state");
  try {
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
  } finally {
    if (previousRoot === undefined) delete process.env.JOSH_ROOM_WORKSPACE_ROOT;
    else process.env.JOSH_ROOM_WORKSPACE_ROOT = previousRoot;
    if (previousInstance === undefined) delete process.env.JOSH_ROOM_INSTANCE;
    else process.env.JOSH_ROOM_INSTANCE = previousInstance;
  }
});

test("Enter reopens a canonical Room from an unrelated CWD without provider or restore work", async () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-unrelated-cwd-test-"));
  const workspaceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-canonical-root-test-"));
  const destination = path.join(workspaceRoot, "trusted-room");
  fs.mkdirSync(destination);
  fs.writeFileSync(path.join(destination, "keep.txt"), "keep this materialization\n");
  writeMarker(destination, {
    dimension_id: "trusted-dimension",
    project_id: "trusted-room",
    snapshot_id: "new-snapshot",
    workspace_path_sha256: sha256Hex(destination),
  });
  const previousRoot = process.env.JOSH_ROOM_WORKSPACE_ROOT;
  const previousInstance = process.env.JOSH_ROOM_INSTANCE;
  process.env.JOSH_ROOM_WORKSPACE_ROOT = workspaceRoot;
  process.env.JOSH_ROOM_INSTANCE = path.join(workspaceRoot, "state");
  try {
    const { vscode, statusItem, executeCalls } = createVscodeMock(cwd);
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
      id: "new-snapshot",
      snapshot_id: "new-snapshot",
      snapshot: { snapshot_id: "new-snapshot" },
      project: { id: "trusted-room", display_name: "Trusted Room" },
      dimension: { id: "trusted-dimension", display_name: "Trusted Dimension" },
    });

    assert.deepEqual(spawnHarness.calls, []);
    assert.deepEqual(executeCalls.find(([name]) => name === "vscode.openFolder"), [
      "vscode.openFolder", { fsPath: destination }, false,
    ]);
  } finally {
    if (previousRoot === undefined) delete process.env.JOSH_ROOM_WORKSPACE_ROOT;
    else process.env.JOSH_ROOM_WORKSPACE_ROOT = previousRoot;
    if (previousInstance === undefined) delete process.env.JOSH_ROOM_INSTANCE;
    else process.env.JOSH_ROOM_INSTANCE = previousInstance;
  }
});

test("clean historical JAT Enter switches the same canonical Room directory atomically", async () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-switch-cwd-test-"));
  const workspaceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-switch-root-test-"));
  const destination = path.join(workspaceRoot, "trusted-room");
  fs.mkdirSync(destination);
  fs.writeFileSync(path.join(destination, "before.txt"), "before\n");
  writeMarker(destination, {
    dimension_id: "trusted-dimension",
    project_id: "trusted-room",
    snapshot_id: "new-snapshot",
    workspace_path_sha256: sha256Hex(destination),
  });
  const previousRoot = process.env.JOSH_ROOM_WORKSPACE_ROOT;
  const previousInstance = process.env.JOSH_ROOM_INSTANCE;
  process.env.JOSH_ROOM_WORKSPACE_ROOT = workspaceRoot;
  process.env.JOSH_ROOM_INSTANCE = path.join(workspaceRoot, "state");
  try {
    const { vscode, statusItem, warningResponses, warningCalls, executeCalls } = createVscodeMock(cwd);
    warningResponses.push("Switch Recovery Point");
    const spawnHarness = createSpawnHarness(({ args }) => {
      if (args[0] === "status") return { stdout: JSON.stringify({
        ok: true,
        state: "clean",
        path_matches: true,
        fingerprint_matches: true,
      }) };
      if (args[0] === "hydrate") {
        const restored = args[args.indexOf("--destination") + 1];
        fs.mkdirSync(restored, { recursive: true });
        fs.writeFileSync(path.join(restored, "after.txt"), "after\n");
        writeMarker(restored, {
          dimension_id: "trusted-dimension",
          project_id: "trusted-room",
          snapshot_id: "old-snapshot",
          workspace_path_sha256: sha256Hex(restored),
        });
        return { stdout: JSON.stringify({ ok: true, destination: restored, snapshot_id: "old-snapshot" }) };
      }
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
    assert.notEqual(hydrate.args[hydrate.args.indexOf("--destination") + 1], destination);
    assert.equal(fs.existsSync(path.join(destination, "after.txt")), true);
    assert.equal(fs.existsSync(path.join(destination, "before.txt")), false);
    assert.equal(JSON.parse(fs.readFileSync(path.join(destination, ".josh-room.json"))).snapshot_id, "old-snapshot");
    assert.equal(fs.existsSync(`${destination}.josh-room-backup`), false);
    assert.match(warningCalls[0][0], /Switch this Room to.*old-snapshot/);
    assert.deepEqual(warningCalls[0].slice(-2), [{ modal: true }, "Switch Recovery Point"]);
    assert.deepEqual(executeCalls.find(([name]) => name === "vscode.openFolder"), [
      "vscode.openFolder", { fsPath: destination }, false,
    ]);
  } finally {
    if (previousRoot === undefined) delete process.env.JOSH_ROOM_WORKSPACE_ROOT;
    else process.env.JOSH_ROOM_WORKSPACE_ROOT = previousRoot;
    if (previousInstance === undefined) delete process.env.JOSH_ROOM_INSTANCE;
    else process.env.JOSH_ROOM_INSTANCE = previousInstance;
  }
});

test("clean historical JAT Enter rewrites the promoted marker hash and reuses the final path without rehydrating", async () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-switch-rebind-cwd-test-"));
  const workspaceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-switch-rebind-root-test-"));
  const destination = path.join(workspaceRoot, "trusted-room");
  const stateRoot = path.join(workspaceRoot, "state");
  fs.mkdirSync(destination);
  fs.writeFileSync(path.join(destination, "before.txt"), "before\n");
  writeMarker(destination, {
    dimension_id: "trusted-dimension",
    project_id: "trusted-room",
    snapshot_id: "new-snapshot",
    workspace_path_sha256: sha256Hex(destination),
  });
  const previousRoot = process.env.JOSH_ROOM_WORKSPACE_ROOT;
  const previousInstance = process.env.JOSH_ROOM_INSTANCE;
  process.env.JOSH_ROOM_WORKSPACE_ROOT = workspaceRoot;
  process.env.JOSH_ROOM_INSTANCE = stateRoot;
  try {
    const first = createVscodeMock(cwd);
    const spawnHarness = createSpawnHarness(({ args }) => {
      if (args[0] === "status") {
        return { stdout: JSON.stringify({
          ok: true,
          state: "clean",
          path_matches: true,
          fingerprint_matches: true,
        }) };
      }
      if (args[0] === "hydrate") {
        const restored = args[args.indexOf("--destination") + 1];
        fs.mkdirSync(restored, { recursive: true });
        fs.writeFileSync(path.join(restored, "after.txt"), "after\n");
        writeMarker(restored, {
          dimension_id: "trusted-dimension",
          project_id: "trusted-room",
          snapshot_id: "old-snapshot",
          workspace_path_sha256: sha256Hex(restored),
        });
        return { stdout: JSON.stringify({ ok: true, destination: restored, snapshot_id: "old-snapshot" }) };
      }
      return { stdout: JSON.stringify({ ok: true }) };
    });
    const extension = loadExtension(first.vscode, spawnHarness.spawn);
    extension.__test__.setStatusItem(first.statusItem);
    first.warningResponses.push("Switch Recovery Point");

    const selected = {
      kind: "jat",
      id: "old-snapshot",
      snapshot_id: "old-snapshot",
      snapshot: { snapshot_id: "old-snapshot" },
      project: {
        id: "trusted-room",
        display_name: "Trusted Room",
        dimension_id: "trusted-dimension",
        dimension: { id: "trusted-dimension", display_name: "Trusted Dimension" },
      },
      dimension: { id: "trusted-dimension", display_name: "Trusted Dimension" },
    };
    await extension.__test__.enterRoom(selected);

    const marker = JSON.parse(fs.readFileSync(path.join(destination, ".josh-room.json"), "utf8"));
    const locator = JSON.parse(fs.readFileSync(path.join(stateRoot, "materializations.json"), "utf8"));
    const statusCall = spawnHarness.calls.find((entry) => entry.args[0] === "status");

    assert.equal(marker.workspace_path_sha256, sha256Hex(destination));
    assert.equal(marker.snapshot_id, "old-snapshot");
    assert.equal(locator.materializations[JSON.stringify(["trusted-dimension", "trusted-room"])], destination);
    assert.equal(statusCall.args[statusCall.args.indexOf("--workspace") + 1], destination);

    const reopened = createVscodeMock(destination);
    const reopenedExtension = loadExtension(reopened.vscode, spawnHarness.spawn);
    reopenedExtension.__test__.setStatusItem(reopened.statusItem);
    const before = spawnHarness.calls.length;
    const reopenedSelected = { ...selected, snapshotId: "old-snapshot" };
    assert.equal(await reopenedExtension.__test__.enterRoom(reopenedSelected), "current");
    assert.equal(spawnHarness.calls.length, before);
    assert.equal(reopened.executeCalls.some(([name]) => name === "vscode.openFolder"), false);
  } finally {
    if (previousRoot === undefined) delete process.env.JOSH_ROOM_WORKSPACE_ROOT;
    else process.env.JOSH_ROOM_WORKSPACE_ROOT = previousRoot;
    if (previousInstance === undefined) delete process.env.JOSH_ROOM_INSTANCE;
    else process.env.JOSH_ROOM_INSTANCE = previousInstance;
  }
});

test("dirty historical JAT Enter offers safe actions without replacing local content", async () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-dirty-cwd-test-"));
  const workspaceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-dirty-root-test-"));
  const destination = path.join(workspaceRoot, "trusted-room");
  fs.mkdirSync(destination);
  const content = path.join(destination, "unsaved.txt");
  fs.writeFileSync(content, "do not clobber\n");
  writeMarker(destination, {
    dimension_id: "trusted-dimension",
    project_id: "trusted-room",
    snapshot_id: "new-snapshot",
    workspace_path_sha256: sha256Hex(destination),
  });
  const previousRoot = process.env.JOSH_ROOM_WORKSPACE_ROOT;
  const previousInstance = process.env.JOSH_ROOM_INSTANCE;
  process.env.JOSH_ROOM_WORKSPACE_ROOT = workspaceRoot;
  process.env.JOSH_ROOM_INSTANCE = path.join(workspaceRoot, "state");
  try {
    const { vscode, statusItem, warningResponses, warningCalls } = createVscodeMock(cwd);
    warningResponses.push("Cancel");
    const spawnHarness = createSpawnHarness(({ args }) => {
      if (args[0] === "status") return { stdout: JSON.stringify({
        ok: false,
        state: "changed",
        path_matches: true,
        fingerprint_matches: false,
      }) };
      if (args[0] === "hydrate") throw new Error("dirty Room must not hydrate");
      return { stdout: JSON.stringify({ ok: true }) };
    });
    const extension = loadExtension(vscode, spawnHarness.spawn);
    extension.__test__.setStatusItem(statusItem);

    assert.equal(await extension.__test__.enterRoom({
      kind: "jat",
      id: "old-snapshot",
      snapshot_id: "old-snapshot",
      snapshot: { snapshot_id: "old-snapshot" },
      project: { id: "trusted-room", display_name: "Trusted Room" },
      dimension: { id: "trusted-dimension", display_name: "Trusted Dimension" },
    }), "cancelled");
    assert.deepEqual(spawnHarness.calls.map((entry) => entry.args[0]), ["status"]);
    assert.equal(fs.readFileSync(content, "utf8"), "do not clobber\n");
    assert.match(warningCalls[0][0], /Save Current First.*Discard Changes \& Switch.*Cancel/);
    assert.deepEqual(warningCalls[0].slice(-4), [
      { modal: true }, "Save Current First", "Discard Changes & Switch", "Cancel",
    ]);
  } finally {
    if (previousRoot === undefined) delete process.env.JOSH_ROOM_WORKSPACE_ROOT;
    else process.env.JOSH_ROOM_WORKSPACE_ROOT = previousRoot;
    if (previousInstance === undefined) delete process.env.JOSH_ROOM_INSTANCE;
    else process.env.JOSH_ROOM_INSTANCE = previousInstance;
  }
});

test("stale local Room locator entries are repaired from corroborated markers", async () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-stale-index-cwd-test-"));
  const workspaceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-stale-index-root-test-"));
  const destination = path.join(workspaceRoot, "trusted-room");
  const stateRoot = path.join(workspaceRoot, "state");
  fs.mkdirSync(destination);
  fs.writeFileSync(path.join(destination, "keep.txt"), "keep\n");
  writeMarker(destination, {
    dimension_id: "trusted-dimension",
    project_id: "trusted-room",
    snapshot_id: "new-snapshot",
    workspace_path_sha256: sha256Hex(destination),
  });
  fs.mkdirSync(stateRoot);
  fs.writeFileSync(path.join(stateRoot, "materializations.json"), JSON.stringify({
    format_version: 1,
    materializations: { [JSON.stringify(["trusted-dimension", "trusted-room"])]: path.join(workspaceRoot, "removed-room") },
  }));
  const previousRoot = process.env.JOSH_ROOM_WORKSPACE_ROOT;
  const previousInstance = process.env.JOSH_ROOM_INSTANCE;
  process.env.JOSH_ROOM_WORKSPACE_ROOT = workspaceRoot;
  process.env.JOSH_ROOM_INSTANCE = stateRoot;
  try {
    const { vscode, statusItem, executeCalls } = createVscodeMock(cwd);
    const spawnHarness = createSpawnHarness(() => ({ stdout: JSON.stringify({ ok: true }) }));
    const extension = loadExtension(vscode, spawnHarness.spawn);
    extension.__test__.setStatusItem(statusItem);

    await extension.__test__.enterRoom({
      kind: "jat",
      id: "new-snapshot",
      snapshot_id: "new-snapshot",
      snapshot: { snapshot_id: "new-snapshot" },
      project: { id: "trusted-room", display_name: "Trusted Room" },
      dimension: { id: "trusted-dimension", display_name: "Trusted Dimension" },
    });

    assert.deepEqual(spawnHarness.calls, []);
    assert.deepEqual(executeCalls.find(([name]) => name === "vscode.openFolder"), [
      "vscode.openFolder", { fsPath: destination }, false,
    ]);
    const repaired = JSON.parse(fs.readFileSync(path.join(stateRoot, "materializations.json")));
    assert.equal(repaired.materializations[JSON.stringify(["trusted-dimension", "trusted-room"])], destination);
  } finally {
    if (previousRoot === undefined) delete process.env.JOSH_ROOM_WORKSPACE_ROOT;
    else process.env.JOSH_ROOM_WORKSPACE_ROOT = previousRoot;
    if (previousInstance === undefined) delete process.env.JOSH_ROOM_INSTANCE;
    else process.env.JOSH_ROOM_INSTANCE = previousInstance;
  }
});

test("identical Room IDs in different Dimensions cannot collide on first materialization", async () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-dimension-collision-cwd-test-"));
  const workspaceRoot = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-enter-dimension-collision-root-test-"));
  const existing = path.join(workspaceRoot, "same-room");
  fs.mkdirSync(existing);
  writeMarker(existing, {
    dimension_id: "archive",
    project_id: "same-room",
    snapshot_id: "archive-jat",
    workspace_path_sha256: sha256Hex(existing),
  });
  const previousRoot = process.env.JOSH_ROOM_WORKSPACE_ROOT;
  const previousInstance = process.env.JOSH_ROOM_INSTANCE;
  process.env.JOSH_ROOM_WORKSPACE_ROOT = workspaceRoot;
  process.env.JOSH_ROOM_INSTANCE = path.join(workspaceRoot, "state");
  try {
    const { vscode, statusItem, executeCalls } = createVscodeMock(cwd);
    const spawnHarness = createSpawnHarness(({ args }) => {
      if (args[0] === "hydrate") {
        const destination = args[args.indexOf("--destination") + 1];
        fs.mkdirSync(destination, { recursive: true });
        writeMarker(destination, {
          dimension_id: "backup",
          project_id: "same-room",
          snapshot_id: "backup-jat",
          workspace_path_sha256: sha256Hex(destination),
        });
      }
      return { stdout: JSON.stringify({ ok: true }) };
    });
    const extension = loadExtension(vscode, spawnHarness.spawn);
    extension.__test__.setStatusItem(statusItem);

    await extension.__test__.enterRoom({
      kind: "jat",
      id: "backup-jat",
      snapshot_id: "backup-jat",
      snapshot: { snapshot_id: "backup-jat" },
      project: { id: "same-room", display_name: "Same Room" },
      dimension: { id: "backup", display_name: "Backup", provider: "minio" },
    });

    const hydrate = spawnHarness.calls.find((entry) => entry.args[0] === "hydrate");
    assert.ok(hydrate);
    const destination = hydrate.args[hydrate.args.indexOf("--destination") + 1];
    assert.equal(destination, path.join(workspaceRoot, "backup--same-room"));
    assert.equal(fs.existsSync(existing), true);
    assert.equal(JSON.parse(fs.readFileSync(path.join(existing, ".josh-room.json"))).dimension_id, "archive");
    assert.deepEqual(executeCalls.find(([name]) => name === "vscode.openFolder"), [
      "vscode.openFolder", { fsPath: destination }, false,
    ]);
  } finally {
    if (previousRoot === undefined) delete process.env.JOSH_ROOM_WORKSPACE_ROOT;
    else process.env.JOSH_ROOM_WORKSPACE_ROOT = previousRoot;
    if (previousInstance === undefined) delete process.env.JOSH_ROOM_INSTANCE;
    else process.env.JOSH_ROOM_INSTANCE = previousInstance;
  }
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

test("serving a historical JAT preserves its provider, Dimension, Room, and snapshot", async () => {
  for (const provider of ["r2", "minio"]) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), `josh-room-${provider}-jat-serve-test-`));
    const { vscode, statusItem } = createVscodeMock(root);
    const terminalCalls = [];
    const dimensionId = `${provider}-dimension`;
    const spawnHarness = createSpawnHarness(({ args }) => {
      if (args[0] === "dimensions" && !args.includes("--with-hierarchy")) return { stdout: JSON.stringify({
        ok: true,
        dimensions: [{
          id: dimensionId,
          display_name: provider === "r2" ? "Cloud Archive" : "MinIO Archive",
          provider,
          projects: [{
            id: "room-a",
            display_name: "Room A",
            snapshots: [
              { snapshot_id: "jat-new", created_at: "2026-08-27T00:01:00Z" },
              { snapshot_id: "jat-old", created_at: "2026-08-26T00:01:00Z" },
            ],
          }],
        }],
      }) };
      if (args[0] === "auth" && args[1] === "status") return { stdout: JSON.stringify({ ok: true, state: "connected" }) };
      if (args[0] === "snapshots") return { stdout: JSON.stringify({
        ok: true,
        latest: "jat-new",
        snapshots: [
          { snapshot_id: "jat-new", created_at: "2026-08-27T00:01:00Z" },
          { snapshot_id: "jat-old", created_at: "2026-08-26T00:01:00Z" },
        ],
      }) };
      throw new Error(`unexpected command: ${args.join(" ")}`);
    });
    const extension = loadExtension(vscode, spawnHarness.spawn);
    extension.__test__.setStatusItem(statusItem);

    const result = await extension.__test__.serveRoom({
      kind: "jat",
      id: "jat-old",
      snapshot_id: "jat-old",
      project: { id: "room-a", display_name: "Room A" },
      dimension: { id: dimensionId, display_name: "Archive", provider },
    }, {
      startRegistry: async (options) => {
        terminalCalls.push(options);
        return "started";
      },
    });

    assert.equal(result, "started");
    assert.equal(terminalCalls.length, 1);
    assert.deepEqual(terminalCalls[0].args, [
      "serve", "room-a", "--snapshot", "jat-old", "--backend", provider, "--dimension", dimensionId,
    ]);
    assert.equal(terminalCalls[0].args.includes("jat-new"), false);
    assert.equal(spawnHarness.calls.filter((entry) => entry.args[0] === "snapshots").length, 1);
  }
});

test("JAT nodes expose the existing Serve Images action in root and template manifests", () => {
  for (const manifest of ["vscode-extension/package.json", "templates/room/vscode-extension/package.json"]) {
    const packageJson = JSON.parse(fs.readFileSync(path.join(__dirname, "..", manifest), "utf8"));
    assert.ok(packageJson.contributes.menus["view/item/context"].some((item) => (
      item.command === "joshRoom.serve"
      && item.when.includes("viewItem == jat")
    )));
  }
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
    if (args[0] === "dimensions" && !args.includes("--with-hierarchy")) return { stdout: JSON.stringify({ ok: true, dimensions: [
      { id: "unavailable", display_name: "Unavailable", provider: "r2" },
      { id: "healthy", display_name: "Healthy", provider: "minio" },
    ] }) };
    if (args[0] === "dimensions" && args.includes("--with-hierarchy")
      && args[args.indexOf("--dimension") + 1] === "unavailable") {
      return { stderr: "unavailable", code: 1 };
    }
    if (args[0] === "dimensions" && args.includes("--with-hierarchy")) return { stdout: JSON.stringify({ ok: true,
      dimensions: [{ id: "healthy", display_name: "Healthy", provider: "minio",
        rooms: [{ id: "healthy-room", display_name: "Healthy Room", jats: [] }] }],
    }) };
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
  const connection = tree[0].children[0];
  const dimension = connection.children[0];
  const treeProvider = new extension.__test__.HierarchyRoomsProvider();
  const connectionItem = treeProvider.getTreeItem(connection);

  assert.equal(tree[0].label, "Cloudflare R2");
  assert.equal(dimension.label, "Default");
  assert.equal(connection.label, "⚠ Not connected");
  assert.equal(connection.description, "Connect Cloudflare");
  assert.equal(connectionItem.command.command, "joshRoom.connectStorage");
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "projects"), false);
});

test("disconnected MinIO storage stays disconnected and does not load hierarchy until reconnect", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-minio-disconnected-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions" && args.includes("--with-hierarchy")) {
      throw new Error("disconnected MinIO must not load hierarchy");
    }
    if (args[0] === "dimensions") return { stdout: JSON.stringify({
      ok: true,
      dimensions: [{
        id: "rooms-a",
        display_name: "Rooms A",
        provider: "minio",
        connection_id: "home-minio",
        connection_state: "connected",
        bucket: "rooms-a",
      }],
      connections: [{
        id: "home-minio",
        display_name: "Home MinIO",
        provider: "minio",
        endpoint: "https://minio.example.invalid:9000",
        auth_state: "disconnected",
      }],
    }) };
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "list") return { stdout: JSON.stringify({
      ok: true,
      connections: [{
        id: "home-minio",
        display_name: "Home MinIO",
        provider: "minio",
        endpoint: "https://minio.example.invalid:9000",
        auth_state: "disconnected",
      }],
    }) };
    if (args[0] === "auth" && args[1] === "status") return { stdout: JSON.stringify({ ok: true, state: "missing" }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  const catalog = await extension.__test__.loadCatalog(root, "Loading storage");
  const tree = buildProviderTree(catalog);
  const minioProvider = tree.find((provider) => provider.provider === "minio");
  const connection = minioProvider.children[0];
  const dimension = connection.children[0];
  const treeItem = new extension.__test__.HierarchyRoomsProvider().getTreeItem(connection);

  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "dimensions" && entry.args.includes("--with-hierarchy")), false);
  assert.equal(connection.label, "⚠ Disconnected");
  assert.equal(connection.description, "Reconnect");
  assert.equal(connection.state, "disconnected");
  assert.equal(treeItem.command.command, "joshRoom.reconnectStorage");
  assert.equal(dimension.children.length, 0);
});

test("Add Storage uses Cloudflare OAuth and then the shared explicit bucket workflow", async () => {
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
    if (args[0] === "auth" && args[1] === "wait") return { stdout: JSON.stringify({ ok: true, status: "authorized" }) };
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "list") return { stdout: JSON.stringify({ ok: true, provider: "r2", endpoint: "https://r2.example", credential_profile: "oauth-runtime", buckets: ["existing-r2"] }) };
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "create") return { stdout: JSON.stringify({ ok: true, provider: "r2", bucket: "josh-room-r2", created: true }) };
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "check") return { stdout: JSON.stringify({ ok: true, provider: "r2", bucket: "josh-room-r2", accessible: true }) };
    if (args[0] === "dimensions" && args[1] === "add") return { stdout: JSON.stringify({ ok: true }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.quickPickResponses.push(
    { label: "Cloudflare R2", provider: "r2" },
    { label: "$(add) Create new bucket… (recommended: josh-room)", create: true, bucket: "josh-room" },
  );
  vscode.inputBoxResponses.push("josh-room-r2");

  assert.equal(await extension.__test__.addStorage(), "added");
  assert.equal(inputBoxCalls.length, 1);
  assert.deepEqual(openExternalCalls.map((uri) => uri.value), ["https://dash.cloudflare.example/oauth"]);
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "dimensions" && entry.args[1] === "add"), true);
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "auth" && entry.args[1] === "start"), true);
});

test("Cloudflare connect delegates polling to one long-lived wait command", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-r2-single-wait-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  let waitCalls = 0;
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "auth" && args[1] === "start") return { stdout: JSON.stringify({
      ok: true,
      session_id: "session-single-wait",
      authorization_url: "https://dash.cloudflare.example/oauth-single-wait",
    }) };
    if (args[0] === "auth" && args[1] === "wait") {
      waitCalls += 1;
      return { stdout: JSON.stringify({ ok: true, status: waitCalls === 1 ? "pending" : "authorized" }) };
    }
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  await assert.rejects(
    extension.__test__.connectCloudflare(
      { kind: "connection", state: "missing", dimension: { id: "r2", provider: "r2" } },
      { pollIntervalMs: 0 },
    ),
    /Cloudflare authorization pending/,
  );
  assert.equal(waitCalls, 1);
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
    if (args[0] === "auth" && args[1] === "wait") return { stdout: JSON.stringify({ ok: true, status: "authorized" }) };
    if (args[0] === "dimensions" && args.includes("--with-hierarchy")) return { stdout: JSON.stringify({
      ok: true,
      dimensions: [{ id: "r2", display_name: "Default", provider: "r2", rooms: [{
        id: "room-a", display_name: "Room A", jats: [{ snapshot_id: "jat-a", display_name: "JAT A" }],
      }] }],
    }) };
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
  const connection = provider.roots[0].children[0];

  assert.equal(connection.label, "✓ Connected");
  assert.equal(connection.children[0].label, "Default");
  assert.equal(connection.children[0].children[0].label, "Room A · Default");
  assert.equal(openExternalCalls[0].value, "https://dash.cloudflare.example/oauth-2");
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "projects"), false);
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
  const connection = buildProviderTree(catalog)[0].children[0];
  const treeItem = new extension.__test__.HierarchyRoomsProvider().getTreeItem(connection);

  assert.equal(connection.label, "⚠ Session expired");
  assert.equal(connection.description, "Reconnect Cloudflare");
  assert.equal(treeItem.command.command, "joshRoom.reconnectStorage");
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
  const connection = buildProviderTree(catalog)[0].children[0];

  assert.equal(connection.label, "⚠ Not connected");
  assert.equal(connection.description, "Connect Cloudflare");
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "projects"), false);
});

test("R2 disconnect logs out the local OAuth session instead of treating the endpoint as a connection id", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-r2-logout-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "auth" && args[1] === "logout") return { stdout: JSON.stringify({ ok: true, status: "logged_out", logged_out: true }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  let refreshes = 0;
  extension.__test__.setRoomsProvider({ refresh: async () => { refreshes += 1; } });

  assert.equal(await extension.__test__.disconnectStorage({
    id: "https://r2.example.invalid",
    provider: "r2",
    connection: { id: "https://r2.example.invalid", provider: "r2" },
    dimension: { id: "r2", provider: "r2" },
  }), "disconnected");
  assert.deepEqual(spawnHarness.calls.map((entry) => entry.args.slice(0, 3)), [["auth", "logout", "--json"]]);
  assert.equal(refreshes, 1);
});

test("warm controller calls use operation progress without repeating preparation messaging", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-controller-preparation-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({ stdout: JSON.stringify({ ok: true, dimensions: [] }) }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const events = [];

  await extension.__test__.runJoshRoom(["dimensions", "list"], root, undefined, { event: (event) => events.push(event) });
  assert.equal(events.some((event) => /Preparing Josh Room controller environment/.test(event.message)), false);
  assert.equal(events.some((event) => /Starting Josh Room controller/.test(event.message)), true);
});

test("RCC stdout and stderr stream live with sanitized popup/output lines", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-rcc-stream-test-"));
  const { vscode, statusItem, logLines } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({ autoClose: false }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  extension.__test__.setOutputChannelForTests({ appendLine: (line) => logLines.push(line), info() {}, warn() {}, error() {}, show() {} });
  const events = [];
  const operation = extension.__test__.runJoshRoom(["dimensions", "list"], root, undefined, { event: (event) => events.push(event) });
  await new Promise((resolve) => setImmediate(resolve));
  const child = spawnHarness.calls[0].child;
  child.stdout.emit("data", Buffer.from(JSON.stringify({ ok: true, dimensions: [{ id: "private-catalog-data" }] })));
  child.stderr.emit("data", Buffer.from("RCC materializing controller\nBearer super-secret-token\n"));
  child.closeWith();
  await operation;

  assert.equal(logLines.some((line) => /RCC materializing controller/.test(line)), true);
  assert.equal(logLines.some((line) => /super-secret-token/.test(line)), false);
  assert.equal(logLines.some((line) => /^\d{4}-\d{2}-\d{2}T/.test(line)), true);
  assert.equal(events.some((event) => /RCC materializing controller/.test(event.message)), true);
  assert.equal(logLines.some((line) => /private-catalog-data/.test(line)), false);
});

test("fresh activation gates all storage calls behind runtime readiness", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-activation-gate-test-"));
  const { vscode, treeViewCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    throw new Error(`storage command started before runtime readiness: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  let release;
  extension.__test__.setRuntimeReadinessForTests(new Promise((resolve) => { release = resolve; }));
  extension.__test__.setRuntimeForTests({ command: "/private/rcc", args: (args) => [...args, "--json"], env: {}, jatRoot: root });
  const context = { extensionPath: root, globalStorageUri: { fsPath: root }, subscriptions: [], secrets: { get: async () => undefined } };

  extension.activate(context);
  await new Promise((resolve) => setImmediate(resolve));
  const roomsView = treeViewCalls.find((view) => view.id === "joshRoom.rooms").options.treeDataProvider;
  const pending = roomsView.getChildren();
  assert.equal(pending[0].kind, "runtime");
  assert.match(pending[0].label, /Preparing Josh Room runtime/);
  assert.deepEqual(spawnHarness.calls, []);
  release({});
  await new Promise((resolve) => setImmediate(resolve));
});

test("local fallback prompt requires explicit Build Locally and supports Show Logs then Cancel", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-fallback-prompt-test-"));
  const { vscode, warningResponses, outputChannel } = createVscodeMock(root);
  const extension = loadExtension(vscode, () => { throw new Error("not expected"); });
  extension.__test__.setOutputChannelForTests(outputChannel);
  warningResponses.push("Show Logs", "Cancel");

  const error = new Error("controller artifact is unpublished");
  error.fallbackReason = "controller-artifact-unpublished";
  const result = await extension.__test__.chooseLocalFallback(error);
  assert.equal(result, "cancelled");
  assert.equal(outputChannel.shown, true);
});

test("choosing Build Locally prewarms the controller before local runtime readiness returns", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-local-prewarm-extension-test-"));
  const controllerRoot = path.join(root, "controller");
  fs.mkdirSync(controllerRoot, { recursive: true });
  fs.writeFileSync(path.join(controllerRoot, "robot.yaml"), "tasks: {}\n");
  const { vscode, warningResponses } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => args[0] === "ht"
    ? { stdout: JSON.stringify({ ok: true }) }
    : { stdout: JSON.stringify({ ok: true }) });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  warningResponses.push("Build Locally");
  const events = [];
  const manifest = {
    extension_version: "0.1.10",
    jat: { git_sha: "a".repeat(40), environment_artifact: { digest: "sha256:" + "b".repeat(64) } },
    controller: {},
  };
  const error = new Error("controller artifact unpublished");
  error.fallbackReason = "controller-artifact-unpublished";
  const state = await extension.__test__.localRuntimeState(
      { globalStorageUri: { fsPath: root } }, manifest,
      { version: "v18.19.2" }, { jatRoot: path.join(root, "jat"), sourceSha: manifest.jat.git_sha },
      error, { event: (event) => events.push(event) }, controllerRoot,
    );
  assert.equal(spawnHarness.calls.some((call) => call.args[0] === "ht"), true);
  assert.equal(state.localReady, false);
  assert.equal(fs.existsSync(path.join(root, "runtime", "local-fallback.json")), true);
  assert.equal(events.some((event) => /Controller environment ready/.test(event.message)), true);
  assert.equal(events.some((event) => /JAT.*materializ/i.test(event.message)), false);
});

test("local JAT fallback publishes once and warm reuse performs only no-build checks", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-local-jat-once-test-"));
  const controllerRoot = path.join(root, "controller");
  const jatRoot = path.join(root, "jat");
  fs.mkdirSync(controllerRoot, { recursive: true });
  fs.mkdirSync(jatRoot, { recursive: true });
  fs.writeFileSync(path.join(controllerRoot, "robot.yaml"), "tasks: {}\n");
  fs.writeFileSync(path.join(jatRoot, "robot.yaml"), "tasks: {}\n");
  const { vscode, warningResponses } = createVscodeMock(root);
  const localArtifact = "sha256:" + "d".repeat(64);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "env" && args[1] === "publish") return { stdout: JSON.stringify({ artifactDigest: localArtifact }) };
    if (args[0] === "--no-build" && args.includes("hauler")) return { stdout: JSON.stringify({ artifactDigest: localArtifact, exitCode: 0 }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  warningResponses.push("Build Locally");
  const manifest = {
    extension_version: "0.1.10",
    jat: { git_sha: "a".repeat(40), environment_artifact: { digest: "sha256:" + "b".repeat(64) } },
    controller: {},
  };
  const error = new Error("portable JAT is incompatible");
  error.fallbackReason = "environment-compatibility";
  const first = await extension.__test__.localRuntimeState(
    { globalStorageUri: { fsPath: root } }, manifest,
    { version: "v18.19.2", executable: "/private/managed/rcc" },
    { jatRoot, sourceSha: manifest.jat.git_sha }, error, { event() {} }, controllerRoot,
  );
  assert.equal(first.jat.artifact, localArtifact);
  assert.equal(spawnHarness.calls.filter((call) => call.args[0] === "env" && call.args[1] === "publish").length, 1);

  const beforeWarm = spawnHarness.calls.length;
  const second = await extension.__test__.localRuntimeState(
    { globalStorageUri: { fsPath: root } }, manifest,
    { version: "v18.19.2", executable: "/private/managed/rcc" },
    first.jat, error, { event() {} }, controllerRoot,
  );
  assert.equal(second.localReady, true);
  const warmCalls = spawnHarness.calls.slice(beforeWarm);
  assert.equal(warmCalls.some((call) => call.args[0] === "env" && call.args[1] === "publish"), false);
  assert.equal(warmCalls.some((call) => call.args[0] === "run"), false);
  assert.equal(warmCalls.every((call) => call.args[0] === "--no-build"), true);
});

test("failed local controller prewarm writes no marker and never reports runtime ready", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-local-prewarm-failure-test-"));
  const controllerRoot = path.join(root, "controller");
  fs.mkdirSync(controllerRoot, { recursive: true });
  fs.writeFileSync(path.join(controllerRoot, "robot.yaml"), "tasks: {}\n");
  const { vscode, warningResponses } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => args[0] === "ht"
    ? { stderr: "controller prewarm failed", code: 1 }
    : { stdout: JSON.stringify({ ok: true }) });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  warningResponses.push("Build Locally");
  const events = [];
  const error = new Error("controller artifact unpublished");
  error.fallbackReason = "controller-artifact-unpublished";
  await assert.rejects(extension.__test__.localRuntimeState(
      { globalStorageUri: { fsPath: root } },
      { extension_version: "0.1.10", jat: { git_sha: "a".repeat(40), environment_artifact: { digest: "sha256:" + "b".repeat(64) } }, controller: {} },
      { version: "v18.19.2" }, { jatRoot: path.join(root, "jat") }, error,
      { event: (event) => events.push(event) }, controllerRoot,
    ), /controller prewarm failed/);
  assert.equal(fs.existsSync(path.join(root, "runtime", "local-fallback.json")), false);
  assert.equal(events.some((event) => /Controller environment ready/.test(event.message)), false);
});

test("local fallback runtime command uses managed RCC and the packaged recipe", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-fallback-command-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({ stdout: JSON.stringify({ ok: true }) }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  extension.__test__.setRuntimeForTests({
    mode: "local-build-fallback",
    command: "/private/managed/rcc",
    args: (args) => ["run", "--silent", "-r", "/private/controller/robot.yaml", "-t", "Josh Room", "--", ...args, "--json"],
    env: { RCC_HOLOTREE_MODE: "private", PATH: "/private/managed:/usr/bin", JOSH_ROOM_EXTENSION_MODE: "1", JOSH_ROOM_JAT_ARTIFACT: "sha256:" + "a".repeat(64) },
    jatArtifact: "sha256:" + "a".repeat(64),
    jatRoot: root,
  });

  await extension.__test__.runJoshRoom(["status"], root);
  assert.equal(spawnHarness.calls[0].command, "/private/managed/rcc");
  assert.deepEqual(spawnHarness.calls[0].args.slice(0, 7), [
    "run", "--silent", "-r", "/private/controller/robot.yaml", "-t", "Josh Room", "--",
  ]);
  assert.equal(spawnHarness.calls[0].options.env.RCC_HOLOTREE_MODE, "private");
  assert.equal(spawnHarness.calls[0].options.env.JOSH_ROOM_EXTENSION_MODE, "1");
});

test("portable runtime retry clears only the scoped fallback marker", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-fallback-clear-test-"));
  const { vscode } = createVscodeMock(root);
  const extension = loadExtension(vscode, () => { throw new Error("not expected"); });
  extension.__test__.setExtensionContextForTests({ globalStorageUri: { fsPath: root }, secrets: { get: async () => undefined } });
  const marker = path.join(root, "runtime", "local-fallback.json");
  fs.mkdirSync(path.dirname(marker), { recursive: true });
  fs.writeFileSync(marker, "scoped marker");

  assert.equal(await extension.__test__.clearLocalFallback(), "portable-runtime-retry");
  assert.equal(fs.existsSync(marker), false);
});

test("MinIO Add Storage asks for concrete settings without invoking Cloudflare OAuth", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-add-storage-minio-test-"));
  const { vscode, statusItem, inputBoxCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "list") return { stdout: JSON.stringify({ ok: true, connections: [] }) };
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "create") return { stdout: JSON.stringify({ ok: true, connection: { id: "home", provider: "minio" } }) };
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "list") return { stdout: JSON.stringify({ ok: true, buckets: ["rooms"] }) };
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "check") return { stdout: JSON.stringify({ ok: true, accessible: true }) };
    if (args[0] === "dimensions" && args[1] === "add") return { stdout: JSON.stringify({ ok: true }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.quickPickResponses.push({ label: "MinIO", provider: "minio" });
  vscode.quickPickResponses.push({ label: "rooms", bucket: "rooms" });
  vscode.inputBoxResponses.push("https://minio.example", "access-synthetic", "secret-synthetic");

  assert.equal(await extension.__test__.addStorage(), "added");
  assert.deepEqual(inputBoxCalls.map((options) => options.prompt), [
    "Endpoint URL",
    "Access key",
    "Secret key",
  ]);
  assert.equal(inputBoxCalls[1].password, true);
  assert.equal(inputBoxCalls[2].password, true);
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "auth"), false);
  assert.match(String(spawnHarness.calls.find((entry) => entry.args[0] === "provider" && entry.args[2] === "create").child.stdinPayload), /access-synthetic/);
  assert.doesNotMatch(spawnHarness.calls.find((entry) => entry.args[0] === "provider" && entry.args[2] === "create").args.join(" "), /access-synthetic|secret-synthetic/);
  const addCall = spawnHarness.calls.find((entry) => entry.args[0] === "dimensions" && entry.args[1] === "add");
  assert.ok(addCall);
  assert.equal(addCall.args[addCall.args.indexOf("--connection") + 1], "home");
});

test("MinIO Add Storage offers an existing connection and a new connection when one reusable connection exists", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-add-storage-minio-reuse-test-"));
  const { vscode, statusItem, quickPickCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "list") {
      return { stdout: JSON.stringify({ ok: true, connections: [{
        id: "home-minio",
        display_name: "Home MinIO",
        provider: "minio",
        endpoint: "https://minio.example.invalid:9000",
      }] }) };
    }
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "list") {
      return { stdout: JSON.stringify({ ok: true, buckets: ["rooms"] }) };
    }
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "check") {
      return { stdout: JSON.stringify({ ok: true, accessible: true }) };
    }
    if (args[0] === "dimensions" && args[1] === "add") return { stdout: JSON.stringify({ ok: true }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.quickPickResponses.push(
    { label: "MinIO", provider: "minio" },
    {
      label: "Home MinIO",
      connection: {
        id: "home-minio",
        display_name: "Home MinIO",
        provider: "minio",
        endpoint: "https://minio.example.invalid:9000",
      },
    },
    { label: "rooms", bucket: "rooms" },
  );

  assert.equal(await extension.__test__.addStorage(), "added");
  assert.equal(quickPickCalls.length, 3);
  assert.deepEqual(quickPickCalls[1].items.map((item) => item.label), [
    "Home MinIO",
    "$(add) New MinIO Connection…",
  ]);
});

test("MinIO Add Storage never auto-selects a singleton existing bucket and recommends a new Josh Room bucket", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-add-storage-minio-singleton-test-"));
  const { vscode, statusItem, quickPickCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "list") return { stdout: JSON.stringify({ ok: true, connections: [] }) };
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "create") return { stdout: JSON.stringify({ ok: true, connection: { id: "home", provider: "minio", credential_profile: "home-profile" } }) };
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "list") return { stdout: JSON.stringify({ ok: true, buckets: ["fizzy-production"] }) };
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "create") return { stdout: JSON.stringify({ ok: true, bucket: "josh-room", created: true }) };
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "check") return { stdout: JSON.stringify({ ok: true, accessible: true }) };
    if (args[0] === "dimensions" && args[1] === "add") return { stdout: JSON.stringify({ ok: true }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.quickPickResponses.push(
    { label: "MinIO", provider: "minio" },
    { label: "$(add) Create new bucket… (recommended: josh-room)", create: true, bucket: "josh-room", recommended: true },
  );
  vscode.inputBoxResponses.push("http://minio.example.invalid:9000", "synthetic-access", "synthetic-secret", "josh-room");

  assert.equal(await extension.__test__.addStorage(), "added");
  assert.equal(quickPickCalls[1].items[0].create, true);
  assert.equal(quickPickCalls[1].items[0].bucket, "josh-room");
  const createCall = spawnHarness.calls.find((entry) => entry.args[0] === "provider" && entry.args[1] === "bucket" && entry.args[2] === "create");
  assert.equal(createCall.args[createCall.args.indexOf("--bucket") + 1], "josh-room");
  const addCall = spawnHarness.calls.find((entry) => entry.args[0] === "dimensions" && entry.args[1] === "add");
  assert.equal(addCall.args.includes("fizzy-production"), false);
  assert.equal(addCall.args.includes("josh-room"), true);
});

test("root extension storage commands parse through the actual Python CLI contract", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-extension-cli-contract-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "list") {
      return { stdout: JSON.stringify({ ok: true, connections: [] }) };
    }
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "create") {
      return { stdout: JSON.stringify({ ok: true, connection: { id: "synthetic-minio", provider: "minio" } }) };
    }
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "list") {
      return { stdout: JSON.stringify({ ok: true, buckets: ["synthetic-room"] }) };
    }
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "check") {
      return { stdout: JSON.stringify({ ok: true, accessible: true }) };
    }
    if (args[0] === "dimensions" && args[1] === "add") return { stdout: JSON.stringify({ ok: true }) };
    throw new Error(`unexpected command: ${args.join(" ")}`);
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.quickPickResponses.push(
    { label: "MinIO", provider: "minio" },
    { label: "synthetic-room", bucket: "synthetic-room" },
  );
  vscode.inputBoxResponses.push("https://minio.example.invalid:9000", "synthetic-access", "synthetic-secret");

  assert.equal(await extension.__test__.addStorage(), "added");
  const vectors = spawnHarness.calls.map((entry) => entry.args);
  const python = path.join(__dirname, "..", ".venv", "bin", "python");
  const parser = spawnSync(fs.existsSync(python) ? python : "python3", ["-c", [
    "import json, sys",
    "from josh_room.cli import build_parser",
    "for argv in json.load(sys.stdin): build_parser().parse_args(argv)",
  ].join("\n")], {
    cwd: path.join(__dirname, ".."),
    env: { ...process.env, PYTHONPATH: path.join(__dirname, "..", "src") },
    input: JSON.stringify(vectors),
    encoding: "utf8",
  });
  assert.equal(parser.status, 0, `Python CLI rejected root extension vectors: ${parser.stderr}`);
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

test("synthetic Default Cloudflare storage does not expose editable settings", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-synthetic-settings-test-"));
  const { vscode, statusItem, inputBoxCalls, infoCalls } = createVscodeMock(root);
  const extension = loadExtension(vscode, createSpawnHarness(() => ({
    stdout: JSON.stringify({ ok: true }),
  })).spawn);
  extension.__test__.setStatusItem(statusItem);

  const catalog = buildProviderTree({
    dimensions: [{
      id: "r2",
      display_name: "Default",
      provider: "r2",
      synthetic: true,
      connection_state: "missing",
      projects: [],
    }],
  });
  const dimension = catalog[0].children[0].children[0];
  const treeItem = new extension.__test__.HierarchyRoomsProvider().getTreeItem(dimension);

  assert.equal(treeItem.contextValue, "dimension-synthetic");
  assert.equal(await extension.__test__.editStorageSettings(dimension), "cancelled");
  assert.equal(inputBoxCalls.length, 0);
  assert.match(infoCalls[0][0], /managed by OAuth/);
});

test("editStorageSettings uses the actual reusable connection instead of the Dimension id", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-edit-storage-settings-test-"));
  const { vscode, statusItem, inputBoxCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "update") {
      return { stdout: JSON.stringify({ ok: true, connection: { id: "home-minio", provider: "minio" } }) };
    }
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const tree = buildProviderTree({
    connections: [{
      id: "home-minio",
      display_name: "Home MinIO",
      provider: "minio",
      endpoint: "https://minio.example.invalid:9000",
    }],
    dimensions: [{
      id: "rooms-a",
      display_name: "Rooms A",
      provider: "minio",
      connection_id: "home-minio",
      endpoint: "https://wrong.example.invalid:9000",
      bucket: "rooms-a",
      projects: [],
    }],
  });
  const dimension = tree[0].children[0].children[0];
  vscode.inputBoxResponses.push("https://minio.example.invalid:9000", "access-synthetic", "secret-synthetic");

  assert.equal(await extension.__test__.editStorageSettings(dimension), "updated");
  assert.equal(inputBoxCalls[0].value, "https://minio.example.invalid:9000");
  const updateCall = spawnHarness.calls.find((entry) => entry.args[0] === "provider" && entry.args[1] === "connection" && entry.args[2] === "update");
  assert.ok(updateCall);
  assert.equal(updateCall.args[updateCall.args.indexOf("--connection") + 1], "home-minio");
});

test("generic MinIO bucket-list failures surface instead of falling back to manual entry", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-minio-bucket-list-error-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "provider" && args[1] === "connection" && args[2] === "list") {
      return { stdout: JSON.stringify({ ok: true, connections: [{
        id: "home-minio",
        display_name: "Home MinIO",
        provider: "minio",
        endpoint: "https://minio.example.invalid:9000",
      }] }) };
    }
    if (args[0] === "provider" && args[1] === "bucket" && args[2] === "list") {
      throw new Error("backend unavailable");
    }
    if (args[0] === "dimensions" && args[1] === "add") return { stdout: JSON.stringify({ ok: true }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.quickPickResponses.push({ label: "MinIO", provider: "minio" });
  vscode.quickPickResponses.push({
    label: "Home MinIO",
    connection: {
      id: "home-minio",
      display_name: "Home MinIO",
      provider: "minio",
      endpoint: "https://minio.example.invalid:9000",
    },
  });

  await assert.rejects(extension.__test__.addStorage(), /backend unavailable/);
  assert.equal(spawnHarness.calls.some((entry) => entry.args[0] === "provider" && entry.args[1] === "bucket" && entry.args[2] === "check"), false);
});

test("cancelling Cloudflare OAuth stops its child and a later Connect starts cleanly", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-r2-cancel-test-"));
  const { vscode, statusItem, progressCalls } = createVscodeMock(root);
  let authStarts = 0;
  let authPolls = 0;
  const authCancels = [];
  const spawnHarness = createSpawnHarness(({ args, child }) => {
    if (args[0] === "auth" && args[1] === "start") {
      authStarts += 1;
      return { stdout: JSON.stringify({
        ok: true,
        session_id: `session-${authStarts}`,
        authorization_url: `https://dash.cloudflare.example/oauth-${authStarts}`,
      }) };
    }
    if (args[0] === "auth" && args[1] === "wait") {
      authPolls += 1;
      if (authPolls === 1) {
        setImmediate(() => progressCalls.at(-1).token.cancel());
        return { autoClose: false };
      }
      return { stdout: JSON.stringify({ ok: true, status: "authorized" }) };
    }
    if (args[0] === "auth" && args[1] === "cancel") {
      authCancels.push(args[2]);
      return { stdout: JSON.stringify({ ok: true, status: "canceled" }) };
    }
    if (args[0] === "dimensions" && args.includes("--with-hierarchy")) return { stdout: JSON.stringify({
      ok: true,
      dimensions: [{ id: "r2", display_name: "Default", provider: "r2", rooms: [{
        id: "room-a", display_name: "Room A", jats: [{ snapshot_id: "jat-a", display_name: "JAT A" }],
      }] }],
    }) };
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
  const dimension = { id: "r2", provider: "r2" };

  const cancelled = await extension.__test__.connectCloudflare(
    { kind: "connection", state: "missing", dimension },
    { pollIntervalMs: 0 },
  );
  assert.equal(cancelled, "cancelled");
  assert.equal(authPolls, 1);
  assert.equal(spawnHarness.calls[1].child.killed, "SIGTERM");
  assert.deepEqual(authCancels, ["session-1"]);
  assert.equal(progressCalls.some((entry) => entry.options.cancellable === true), true);

  const relaunched = loadExtension(vscode, spawnHarness.spawn);
  relaunched.__test__.setStatusItem(statusItem);
  const relaunchedProvider = new relaunched.__test__.HierarchyRoomsProvider();
  relaunched.__test__.setRoomsProvider(relaunchedProvider);
  assert.equal(await relaunched.__test__.connectCloudflare(
    { kind: "connection", state: "missing", dimension },
    { pollIntervalMs: 0 },
  ), "connected");
  assert.equal(authStarts, 2);
  assert.equal(authPolls, 2);
  assert.equal(relaunchedProvider.roots[0].children[0].children[0].children[0].label, "Room A · Default");
  assert.equal(spawnHarness.calls.filter((entry) => entry.args[0] === "dimensions").length > 0, true);
});

test("native loading reads each Dimension hierarchy once without Room snapshot N+1", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-hierarchy-read-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const hierarchyCalls = [];
  const forbiddenCalls = [];
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions" && args.includes("--with-hierarchy")) {
      const id = args[args.indexOf("--dimension") + 1];
      hierarchyCalls.push(id);
      return { stdout: JSON.stringify({ ok: true, dimensions: [{
        id,
        display_name: id,
        provider: "minio",
        connection_id: "home-minio",
        bucket: id,
        rooms: [{
          id: `${id}-room`,
          display_name: `${id} Room`,
          latest: `${id}-jat-2`,
          jats: [
            { snapshot_id: `${id}-jat-1`, display_name: `${id} JAT 1` },
            { snapshot_id: `${id}-jat-2`, display_name: `${id} JAT 2` },
          ],
        }],
      }] }) };
    }
    if (args[0] === "dimensions") return { stdout: JSON.stringify({ ok: true, dimensions: [
      { id: "development", display_name: "Development", provider: "minio", connection_id: "home-minio", bucket: "development" },
      { id: "experiments", display_name: "Experiments", provider: "minio", connection_id: "home-minio", bucket: "experiments" },
    ] }) };
    if (args[0] === "provider" && args[1] === "connection") {
      return { stdout: JSON.stringify({ ok: true, connections: [{
        id: "home-minio", display_name: "Homelab", provider: "minio", endpoint: "https://minio.example.invalid",
      }] }) };
    }
    if (args[0] === "auth" && args[1] === "status") return { stdout: JSON.stringify({ ok: true, state: "missing" }) };
    if (args[0] === "projects" || args[0] === "snapshots") {
      forbiddenCalls.push(args);
      return { stderr: "N+1 catalog call forbidden", code: 1 };
    }
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  const catalog = await extension.__test__.loadCatalog(root, "Loading hierarchy");

  assert.deepEqual(hierarchyCalls.sort(), ["development", "experiments"]);
  assert.deepEqual(forbiddenCalls, []);
  assert.deepEqual(catalog.projects.map((project) => project.id).sort(), [
    "development-room", "experiments-room",
  ]);
  assert.deepEqual(catalog.projects.flatMap((project) => (project.jats || project.snapshots || []).map((snapshot) => snapshot.snapshot_id)).sort(), [
    "development-jat-1", "development-jat-2", "experiments-jat-1", "experiments-jat-2",
  ]);
});

test("native loading keeps a failed Dimension visibly failed instead of empty", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-dimension-error-test-"));
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "dimensions" && args.includes("--with-hierarchy")
      && args[args.indexOf("--dimension") + 1] === "broken") {
      return { stdout: JSON.stringify({
        ok: false,
        error: "MinIO bucket is unavailable",
        error_code: "bucket-access-denied",
        dimension_id: "broken",
      }), code: 1 };
    }
    if (args[0] === "dimensions" && !args.includes("--with-hierarchy")) return { stdout: JSON.stringify({ ok: true, dimensions: [
      { id: "broken", display_name: "Broken bucket", provider: "minio", connection_id: "home-minio", bucket: "broken" },
      { id: "healthy", display_name: "Healthy bucket", provider: "minio", connection_id: "home-minio", bucket: "healthy" },
    ] }) };
    if (args[0] === "provider" && args[1] === "connection") return { stdout: JSON.stringify({ ok: true, connections: [
      { id: "home-minio", display_name: "Homelab", provider: "minio", endpoint: "https://minio.example.invalid" },
    ] }) };
    if (args[0] === "auth" && args[1] === "status") return { stdout: JSON.stringify({ ok: true, state: "missing" }) };
    if (args[0] === "dimensions" && args.includes("--with-hierarchy")) return { stdout: JSON.stringify({ ok: true, dimensions: [{
      id: "healthy", display_name: "Healthy bucket", provider: "minio", connection_id: "home-minio", bucket: "healthy",
      rooms: [{ id: "healthy-room", display_name: "Healthy Room", jats: [] }],
    }] }) };
    return { stdout: JSON.stringify({ ok: true }) };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  const catalog = await extension.__test__.loadCatalog(root, "Loading error states");
  const tree = buildProviderTree(catalog);
  const dimensions = tree[0].children[0].children;
  const broken = dimensions.find((item) => item.id === "broken");
  const healthy = dimensions.find((item) => item.id === "healthy");
  assert.equal(broken.state, "error");
  assert.match(broken.children[0].label, /Bucket access denied|Could not load/);
  assert.equal(healthy.children[0].label, "Healthy Room · Healthy bucket");
});

// ---------------------------------------------------------------------------
// JAT capability-capsule workflows (Pack / Inspect / Extract / Restore /
// Serve / Export / Copy)
// ---------------------------------------------------------------------------

const JAT_COMMAND_IDS = [
  "joshRoom.jatBuild",
  "joshRoom.jatInspect",
  "joshRoom.jatExtract",
  "joshRoom.jatRestore",
  "joshRoom.jatServe",
  "joshRoom.jatExport",
  "joshRoom.jatCopy",
];

function withWindowExtras(vscode) {
  const saveDialogCalls = [];
  const saveDialogResponses = [];
  const errorCalls = [];
  const errorResponses = [];
  vscode.window.showSaveDialog = async (options) => {
    saveDialogCalls.push(options);
    return saveDialogResponses.shift();
  };
  vscode.window.showErrorMessage = async (...args) => {
    errorCalls.push(args);
    return errorResponses.shift();
  };
  return { saveDialogCalls, saveDialogResponses, errorCalls, errorResponses };
}

function withClipboardRecorder(vscode) {
  const writes = [];
  const originalWrite = vscode.env.clipboard.writeText;
  vscode.env.clipboard.writeText = async (value) => {
    writes.push(value);
    return originalWrite(value);
  };
  return writes;
}

function demoInventory() {
  return [
    {
      reference: "registry.example.test:5000/demo/app:1.0",
      type: "image",
      platform: "linux/amd64",
      size: 1048576,
      digest: "sha256:" + "d".repeat(64),
    },
    { reference: "files/app-settings", type: "files" },
  ];
}

test("JAT Tools rows expose the seven capability-capsule commands in order", () => {
  const { vscode } = createVscodeMock(os.tmpdir());
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  const provider = new extension.__test__.JatToolsProvider();
  const rows = provider.getChildren();

  assert.equal(rows.length, 7);
  assert.deepEqual(rows.map((row) => row.label), [
    "Pack Folder into JAT",
    "Inspect JAT",
    "Extract from JAT",
    "Restore Workspace",
    "Serve JAT…",
    "Export Images…",
    "Copy / Seed…",
  ]);
  assert.deepEqual(rows.map((row) => row.description), [
    "Build",
    "Inventory",
    "One reference",
    "Restore",
    "Auto · Files · Registry · Both",
    "containerd",
    "registry · dir",
  ]);
  assert.deepEqual(rows.map((row) => row.command), JAT_COMMAND_IDS);
  for (const row of rows) {
    const treeItem = provider.getTreeItem(row);
    assert.equal(treeItem.label, row.label);
    assert.equal(treeItem.description, row.description);
    assert.equal(treeItem.iconPath.id, row.icon);
    assert.deepEqual(treeItem.command, { command: row.command, title: row.label });
    assert.equal(treeItem.collapsibleState, vscode.TreeItemCollapsibleState.None);
  }
  for (const handler of ["jatBuild", "jatInspect", "jatExtract", "jatServe", "jatExport", "jatCopy"]) {
    assert.equal(typeof extension.__test__[handler], "function", handler);
  }
});

test("activation registers every JAT capability command", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-registration-test-"));
  const { vscode, statusItem, commandCallbacks } = createVscodeMock(root);
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  extension.__test__.setStatusItem(statusItem);
  extension.__test__.setRuntimeReadinessForTests(new Promise(() => {}));
  extension.activate({
    extensionPath: root,
    globalStorageUri: { fsPath: root },
    subscriptions: [],
    secrets: { get: async () => undefined },
  });

  for (const id of JAT_COMMAND_IDS) {
    assert.equal(typeof commandCallbacks.get(id), "function", id);
  }
});

test("registered JAT commands wrap controller failures into the error message UX", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-register-error-test-"));
  const { vscode, statusItem, commandCallbacks } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({
    stdout: JSON.stringify({ ok: false, error: "boom" }),
  }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const extras = withWindowExtras(vscode);
  extension.__test__.setRuntimeReadinessForTests(new Promise(() => {}));
  extension.activate({
    extensionPath: root,
    globalStorageUri: { fsPath: root },
    subscriptions: [],
    secrets: { get: async () => undefined },
  });
  vscode.openDialogResponses.push([{ fsPath: path.join(root, "capsule.haul.tar.zst") }]);

  assert.equal(await commandCallbacks.get("joshRoom.jatInspect")(), "failed");
  assert.deepEqual(spawnHarness.calls[0].args.slice(0, 4), ["jat", "inspect", "--haul", path.join(root, "capsule.haul.tar.zst")]);
  assert.deepEqual(extras.errorCalls, [["boom"]]);
});

test("Pack Folder into JAT forwards every advanced capture input to the build controller", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-build-advanced-test-"));
  const source = path.join(root, "demo-room");
  fs.mkdirSync(source);
  const output = path.join(root, "demo-room.haul.tar.zst");
  const imagesList = path.join(source, "images.txt");
  const manifest = path.join(source, "hauler-manifest.yaml");
  const { vscode, statusItem, quickPickCalls, openDialogCalls, inputBoxCalls, warningCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({
    stdout: JSON.stringify({ ok: true, payload_path: output, payload_size: 4096, sha256: "c".repeat(64) }),
  }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const extras = withWindowExtras(vscode);
  extras.saveDialogResponses.push({ fsPath: output });
  vscode.openDialogResponses.push(
    [{ fsPath: source }],
    [{ fsPath: imagesList }],
    [{ fsPath: manifest }],
  );
  vscode.quickPickResponses.push(
    { label: "Workspace only", allImages: false },
    [{ id: "imagesFile" }, { id: "manifests" }, { id: "chunk" }, { id: "slim" }],
  );
  vscode.inputBoxResponses.push(" 500M ");
  vscode.warningResponses.push("Keep full extras");

  assert.equal(await extension.__test__.jatBuild(), "built");

  assert.deepEqual(quickPickCalls[0].items.map((item) => item.label), [
    "Workspace only",
    "Workspace + all tagged local OCI images",
  ]);
  assert.equal(quickPickCalls[1].options.canPickMany, true);
  assert.equal(quickPickCalls[1].options.title, "Advanced capture inputs");
  assert.match(warningCalls[0][0], /Slim build excludes cosign signatures/);
  assert.deepEqual(warningCalls[0].slice(1), [{ modal: true }, "Use slim build", "Keep full extras"]);
  const buildCall = spawnHarness.calls.at(-1);
  assert.deepEqual(buildCall.args, [
    "jat", "build", "--source", source, "--output", output,
    "--images-file", imagesList,
    "--hauler-manifest", manifest,
    "--chunk-size", "500M",
    "--json",
  ]);
  assert.equal(buildCall.args.includes("--exclude-extras"), false);
  assert.equal(buildCall.args.includes("--all-images"), false);
  assert.equal(inputBoxCalls[0].validateInput("500M"), undefined);
  assert.match(String(inputBoxCalls[0].validateInput("1Mi")), /positive byte count/);
  assert.deepEqual(extras.errorCalls, []);
});

test("Pack Folder into JAT slim confirmation excludes referrer extras", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-build-slim-test-"));
  const source = path.join(root, "demo-room");
  fs.mkdirSync(source);
  const output = path.join(root, "demo-room.haul.tar.zst");
  const imagesList = path.join(source, "images.txt");
  const { vscode, statusItem, infoCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({
    stdout: JSON.stringify({ ok: true, payload_path: output, payload_size: 4096, sha256: "c".repeat(64) }),
  }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const extras = withWindowExtras(vscode);
  extras.saveDialogResponses.push({ fsPath: output });
  vscode.openDialogResponses.push([{ fsPath: source }], [{ fsPath: imagesList }]);
  vscode.quickPickResponses.push(
    { label: "Workspace only", allImages: false },
    [{ id: "imagesFile" }, { id: "slim" }],
  );
  vscode.warningResponses.push("Use slim build");

  assert.equal(await extension.__test__.jatBuild(), "built");

  const buildCall = spawnHarness.calls.at(-1);
  assert.deepEqual(buildCall.args.slice(0, 8), [
    "jat", "build", "--source", source, "--output", output,
    "--images-file", imagesList,
  ]);
  assert.equal(buildCall.args.includes("--exclude-extras"), true);
  assert.match(infoCalls[0][0], new RegExp(output.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
});

test("Pack Folder into JAT Esc on advanced inputs keeps the simple one-click pack", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-build-simple-test-"));
  const source = path.join(root, "demo-room");
  fs.mkdirSync(source);
  const output = path.join(root, "demo-room.haul.tar.zst");
  const { vscode, statusItem, quickPickCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({
    stdout: JSON.stringify({ ok: true, payload_path: output, payload_size: 4096, sha256: "c".repeat(64) }),
  }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const extras = withWindowExtras(vscode);
  extras.saveDialogResponses.push({ fsPath: output });
  vscode.openDialogResponses.push([{ fsPath: source }]);
  vscode.quickPickResponses.push({ label: "Workspace only", allImages: false }, undefined);

  assert.equal(await extension.__test__.jatBuild(), "built");

  assert.equal(quickPickCalls.length, 2);
  assert.deepEqual(spawnHarness.calls.at(-1).args, [
    "jat", "build", "--source", source, "--output", output, "--json",
  ]);
});

test("Inspect JAT renders the inventory QuickPick and copies the selected reference", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-inspect-test-"));
  const haul = path.join(root, "capsule.haul.tar.zst");
  const inventory = demoInventory();
  const { vscode, statusItem, quickPickCalls, infoCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({
    stdout: JSON.stringify({
      ok: true,
      inventory,
      anchors: { images: true, files: true, charts: false },
    }),
  }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const clipboardWrites = withClipboardRecorder(vscode);
  vscode.openDialogResponses.push([{ fsPath: haul }]);
  vscode.quickPickResponses.push({ entry: inventory[0] });
  vscode.infoResponses.push("Copy Reference");

  assert.equal(await extension.__test__.jatInspect(), "inspected");

  const inspectCall = spawnHarness.calls[0];
  assert.deepEqual(inspectCall.args.slice(0, 4), ["jat", "inspect", "--haul", haul]);
  assert.equal(inspectCall.args.at(-1), "--json");
  const pick = quickPickCalls.at(-1);
  assert.deepEqual(pick.items.map((item) => item.label), [
    "$(package) registry.example.test:5000/demo/app:1.0",
    "$(package) files/app-settings",
  ]);
  assert.deepEqual(pick.items.map((item) => item.description), [
    "image · linux/amd64 · 1.0 MB",
    "files",
  ]);
  assert.match(pick.options.title, /2 references/);
  assert.match(pick.options.placeHolder, /JAT anchors: images, files/);
  assert.match(infoCalls[0][0], /registry\.example\.test:5000\/demo\/app:1\.0/);
  assert.deepEqual(infoCalls[0].slice(1), ["Copy Reference", "Extract…"]);
  assert.deepEqual(clipboardWrites, ["registry.example.test:5000/demo/app:1.0"]);
  assert.equal(infoCalls.at(-1)[0], "Copied registry.example.test:5000/demo/app:1.0.");
});

test("Inspect JAT surfaces controller failures to the caller", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-inspect-error-test-"));
  const haul = path.join(root, "capsule.haul.tar.zst");
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({
    stdout: JSON.stringify({ ok: false, error: "boom" }),
  }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.openDialogResponses.push([{ fsPath: haul }]);

  await assert.rejects(extension.__test__.jatInspect(), (error) => {
    assert.equal(error.message, "boom");
    assert.deepEqual(error.result, { ok: false, error: "boom" });
    return true;
  });
  assert.deepEqual(spawnHarness.calls[0].args.slice(0, 4), ["jat", "inspect", "--haul", haul]);
});

test("Extract from JAT inspects once and extracts the chosen reference", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-extract-test-"));
  const haul = path.join(root, "capsule.haul.tar.zst");
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-extract-parent-"));
  const inventory = demoInventory();
  const destination = path.join(parent, "demo-extract");
  const { vscode, statusItem, infoCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(({ args }) => {
    if (args[0] === "jat" && args[1] === "inspect") {
      return { stdout: JSON.stringify({ ok: true, inventory }) };
    }
    return {
      stdout: JSON.stringify({
        ok: true,
        payload_path: path.join(destination, "app"),
        payload_size: 2048,
        sha256: "b".repeat(64),
      }),
    };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.openDialogResponses.push([{ fsPath: haul }], [{ fsPath: parent }]);
  vscode.quickPickResponses.push({ entry: inventory[0] });
  vscode.inputBoxResponses.push("demo-extract");

  assert.equal(await extension.__test__.jatExtract(), "extracted");
  assert.equal(fs.existsSync(destination), false);

  assert.equal(spawnHarness.calls.length, 2);
  assert.deepEqual(spawnHarness.calls[0].args.slice(0, 4), ["jat", "inspect", "--haul", haul]);
  assert.deepEqual(spawnHarness.calls[1].args, [
    "jat", "extract", "--haul", haul,
    "--reference", "registry.example.test:5000/demo/app:1.0",
    "--destination", destination,
    "--json",
  ]);
  assert.match(infoCalls[0][0], /Extracted registry\.example\.test:5000\/demo\/app:1\.0 to/);
  assert.match(infoCalls[0][0], new RegExp(path.join(destination, "app").replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
});

test("Extract from JAT with a preselected reference skips the inspect spawn", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-extract-preselected-test-"));
  const haul = path.join(root, "capsule.haul.tar.zst");
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-extract-preselected-parent-"));
  const reference = "registry.example.test:5000/demo/app:1.0";
  const destination = path.join(parent, "demo-extract");
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({ stdout: JSON.stringify({ ok: true }) }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.openDialogResponses.push([{ fsPath: parent }]);
  vscode.inputBoxResponses.push("demo-extract");

  assert.equal(await extension.__test__.jatExtract({ haul, reference }), "extracted");

  assert.equal(spawnHarness.calls.length, 1);
  assert.deepEqual(spawnHarness.calls[0].args, [
    "jat", "extract", "--haul", haul,
    "--reference", reference,
    "--destination", destination,
    "--json",
  ]);
});

test("Export Images writes the containerd archive and reports the payload receipt", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-export-test-"));
  const haul = path.join(root, "capsule.haul.tar.zst");
  const output = path.join(root, "capsule-images.tar");
  const { vscode, statusItem, infoCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({
    stdout: JSON.stringify({
      ok: true,
      payloads: [{ path: output, size: 1048576, sha256: "a".repeat(64) }],
    }),
  }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  const extras = withWindowExtras(vscode);
  extras.saveDialogResponses.push({ fsPath: output });
  vscode.openDialogResponses.push([{ fsPath: haul }]);

  assert.equal(await extension.__test__.jatExport(), "exported");

  assert.deepEqual(spawnHarness.calls[0].args, [
    "jat", "export", "--haul", haul, "--output", output, "--json",
  ]);
  assert.match(infoCalls[0][0], /Exported containerd archive/);
  assert.ok(infoCalls[0][0].includes(output));
  assert.ok(infoCalls[0][0].includes("a".repeat(16)));
});

test("Copy / Seed pushes a haul to a credential-free registry target", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-copy-registry-test-"));
  const haul = path.join(root, "capsule.haul.tar.zst");
  const target = "registry://registry.example.test:5000";
  const { vscode, statusItem, quickPickCalls, inputBoxCalls, infoCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({
    stdout: JSON.stringify({
      ok: true,
      transfer: { destination: target, transport: "registry" },
    }),
  }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.openDialogResponses.push([{ fsPath: haul }]);
  vscode.quickPickResponses.push({ label: "Registry", scheme: "registry://" });
  vscode.inputBoxResponses.push(target);

  assert.equal(await extension.__test__.jatCopy(), "copied");

  assert.deepEqual(spawnHarness.calls[0].args, [
    "jat", "copy", "--haul", haul, "--to", target, "--json",
  ]);
  assert.deepEqual(quickPickCalls.at(-1).items.map((item) => item.label), ["Registry", "Directory"]);
  assert.equal(inputBoxCalls[0].placeHolder, "registry://registry.example.test:5000");
  assert.equal(inputBoxCalls[0].validateInput(target), undefined);
  assert.match(String(inputBoxCalls[0].validateInput("registry://user:token@host")), /credentials/);
  assert.match(String(inputBoxCalls[0].validateInput("registry://host?x=1")), /credentials/);
  assert.match(infoCalls[0][0], /Seeded registry:\/\/registry\.example\.test:5000 \(registry\)/);
});

test("Copy / Seed projects a haul into a local directory target", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-copy-dir-test-"));
  const haul = path.join(root, "capsule.haul.tar.zst");
  const seedDirectory = path.join(root, "seed");
  fs.mkdirSync(seedDirectory);
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => ({ stdout: JSON.stringify({ ok: true }) }));
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.openDialogResponses.push([{ fsPath: haul }], [{ fsPath: seedDirectory }]);
  vscode.quickPickResponses.push({ label: "Directory", scheme: "dir://" });

  assert.equal(await extension.__test__.jatCopy(), "copied");

  assert.deepEqual(spawnHarness.calls[0].args, [
    "jat", "copy", "--haul", haul, "--to", `dir://${seedDirectory}`, "--json",
  ]);
});

test("Serve JAT offers the four projection modes and launches the mode-specific terminal", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-serve-test-"));
  const haul = path.join(root, "capsule.haul.tar.zst");
  const { vscode, statusItem, quickPickCalls, infoCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => { throw new Error("serve must not spawn a controller"); });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);

  assert.deepEqual(extension.__test__.JAT_SERVE_MODES.map((item) => item.label), [
    "Auto", "Files", "Registry", "Files + Registry",
  ]);
  assert.deepEqual(extension.__test__.JAT_SERVE_MODES.map((item) => item.mode), [
    "auto", "files", "registry", "both",
  ]);
  assert.deepEqual(extension.__test__.JAT_SERVE_MODES.map((item) => item.description), [
    "Let JAT choose the compatible projection",
    "Direct downloads / file artifacts",
    "OCI images and charts",
    "Expose both from the same capsule",
  ]);

  vscode.openDialogResponses.push([{ fsPath: haul }]);
  vscode.quickPickResponses.push(extension.__test__.JAT_SERVE_MODES[1]);

  assert.equal(await extension.__test__.jatServe(), "started");
  assert.equal(quickPickCalls.at(-1).items, extension.__test__.JAT_SERVE_MODES);
  assert.match(infoCalls[0][0], /serving files from this capsule/);
  assert.deepEqual(spawnHarness.calls, []);

  const launch = extension.__test__.buildTerminalLaunch(
    { command: "/test/managed-rcc", args: (args) => [...args, "--json"] },
    ["jat", "serve", "--haul", haul, "--mode", "both"],
    { JOSH_ROOM_EXTENSION_MODE: "1" },
    "linux",
  );
  assert.match(launch.command, /--mode/);
  assert.match(launch.command, /both/);
  assert.match(launch.command, /'jat' 'serve' '--haul'/);
});

test("Auto serve reports the registry when JAT starts the registry projection", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-auto-registry-test-"));
  const haul = path.join(root, "mixed.capsule.tar.zst");
  const { vscode, statusItem, infoCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => { throw new Error("serve must not spawn a controller"); });
  const extension = loadExtension(vscode, spawnHarness.spawn, {
    waitForRegistry: async () => ({ repositories: ["demo/app"] }),
    waitForFileserver: async () => { throw new Error("fileserver must not win when the registry is up"); },
  });
  extension.__test__.setStatusItem(statusItem);

  vscode.openDialogResponses.push([{ fsPath: haul }]);
  vscode.quickPickResponses.push(extension.__test__.JAT_SERVE_MODES[0]);

  assert.equal(await extension.__test__.jatServe(), "started");
  assert.match(infoCalls[0][0], /Hauler registry is ready/);
});

test("Auto serve follows the fileserver when JAT resolves a files-only capsule", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-auto-files-test-"));
  const haul = path.join(root, "files-only.capsule.tar.zst");
  const { vscode, statusItem, infoCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => { throw new Error("serve must not spawn a controller"); });
  const extension = loadExtension(vscode, spawnHarness.spawn, {
    // A files-only capsule never opens port 5000; the wait must not hang on it.
    waitForRegistry: () => new Promise(() => {}),
    waitForFileserver: async () => ({ fileserver: true }),
  });
  extension.__test__.setStatusItem(statusItem);

  vscode.openDialogResponses.push([{ fsPath: haul }]);
  vscode.quickPickResponses.push(extension.__test__.JAT_SERVE_MODES[0]);

  assert.equal(await extension.__test__.jatServe(), "started");
  assert.match(infoCalls[0][0], /resolved this capsule to a files projection/);
  assert.match(infoCalls[0][0], /127\.0\.0\.1:8080/);
});

test("Auto serve fails when no JAT serve endpoint becomes ready", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-auto-fail-test-"));
  const haul = path.join(root, "capsule.tar.zst");
  const { vscode, statusItem } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => { throw new Error("serve must not spawn a controller"); });
  const extension = loadExtension(vscode, spawnHarness.spawn, {
    waitForRegistry: async () => { throw new Error("registry down"); },
    waitForFileserver: async () => { throw new Error("fileserver down"); },
  });
  extension.__test__.setStatusItem(statusItem);
  const extras = withWindowExtras(vscode);

  vscode.openDialogResponses.push([{ fsPath: haul }]);
  vscode.quickPickResponses.push(extension.__test__.JAT_SERVE_MODES[0]);

  assert.equal(await extension.__test__.jatServe(), "failed");
  assert.match(extras.errorCalls[0][0], /JAT serve failed to start/);
  assert.match(extras.errorCalls[0][0], /fileserver down/);
});

test("waitForServeEndpoint resolves with the first endpoint JAT starts", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-auto-race-test-"));
  const { vscode } = createVscodeMock(root);
  let registryResolve;
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); }, {
    waitForRegistry: () => new Promise((resolve) => { registryResolve = resolve; }),
    waitForFileserver: async () => ({ fileserver: true }),
  });

  const pending = extension.__test__.waitForServeEndpoint();
  registryResolve({ repositories: ["late/app"] });
  assert.equal((await pending).kind, "files");
});

test("cancelling a JAT operation kills the spawned controller child", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-jat-cancel-test-"));
  const haul = path.join(root, "capsule.haul.tar.zst");
  const { vscode, statusItem, progressCalls } = createVscodeMock(root);
  const spawnHarness = createSpawnHarness(() => {
    setImmediate(() => progressCalls.at(-1).token.cancel());
    return { autoClose: false };
  });
  const extension = loadExtension(vscode, spawnHarness.spawn);
  extension.__test__.setStatusItem(statusItem);
  vscode.openDialogResponses.push([{ fsPath: haul }]);

  await assert.rejects(extension.__test__.jatInspect(), /cancelled/);

  assert.deepEqual(spawnHarness.calls[0].args.slice(0, 4), ["jat", "inspect", "--haul", haul]);
  assert.equal(spawnHarness.calls[0].child.killed, "SIGTERM");
});

test("JAT chunk size validation accepts hauler byte counts with optional units", () => {
  const { vscode } = createVscodeMock(os.tmpdir());
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  const accepts = extension.__test__.isHaulerChunkSize;
  for (const value of ["500M", "1G", "500MB", "1024", "512k", "2T", "2TB", " 500M "]) {
    assert.equal(accepts(value), true, value);
  }
  for (const value of ["1B", "1Mi", "-5", "abc", "", "0M", "1.5G", undefined]) {
    assert.equal(accepts(value), false, String(value));
  }
});

test("copy target validation rejects credentials, queries, fragments, and whitespace", () => {
  const { vscode } = createVscodeMock(os.tmpdir());
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  const accepts = extension.__test__.isSafeCopyTarget;
  assert.equal(accepts("registry://registry.example.test:5000", "registry://"), true);
  assert.equal(accepts("registry://registry.example.test:5000/team/app", "registry://"), true);
  assert.equal(accepts("dir:///tmp/seed-room", "dir://"), true);
  assert.equal(accepts("registry://user:token@registry.example.test:5000", "registry://"), false);
  assert.equal(accepts("registry://registry.example.test:5000?ns=team", "registry://"), false);
  assert.equal(accepts("registry://registry.example.test:5000#frag", "registry://"), false);
  assert.equal(accepts("dir://C:/x y", "dir://"), false);
  assert.equal(accepts("registry://", "registry://"), false);
  assert.equal(accepts("https://registry.example.test:5000", "registry://"), false);
});

test("resultPayloads prefers controller payloads and falls back to receipts", () => {
  const { vscode } = createVscodeMock(os.tmpdir());
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  const payloads = extension.__test__.resultPayloads;
  const preferred = [{ path: "chunk-000.tar.zst", size: 1, sha256: "a".repeat(64) }];
  assert.equal(payloads({ payloads: preferred, payload_path: "fallback" }, "fallback"), preferred);
  assert.deepEqual(payloads({ payload_path: "out.tar.zst", payload_size: 2048, sha256: "b".repeat(64) }, "fallback"), [
    { path: "out.tar.zst", size: 2048, sha256: "b".repeat(64) },
  ]);
  assert.deepEqual(payloads({}, "destination"), [{ path: "destination" }]);
  assert.deepEqual(payloads({}), []);
  assert.deepEqual(payloads(undefined, undefined), []);
});

test("inventory quick pick items skip reference-less entries and compose descriptions", () => {
  const { vscode } = createVscodeMock(os.tmpdir());
  const extension = loadExtension(vscode, () => { throw new Error("spawn must not run"); });
  const items = extension.__test__.inventoryQuickPickItems;
  const entry = {
    reference: "registry.example.test:5000/demo/app:1.0",
    type: "image",
    platform: "linux/amd64",
    size: 1048576,
  };
  const mapped = items([entry, {}, { reference: "" }, { reference: 42 }, null]);
  assert.equal(mapped.length, 1);
  assert.equal(mapped[0].label, "$(package) registry.example.test:5000/demo/app:1.0");
  assert.equal(mapped[0].description, "image · linux/amd64 · 1.0 MB");
  assert.equal(mapped[0].entry, entry);
  assert.deepEqual(items(undefined), []);
  assert.deepEqual(items("not-a-list"), []);
});
