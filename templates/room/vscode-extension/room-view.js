"use strict";

const childProcess = require("node:child_process");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");

const STARTUP_TIMEOUT_MS = 30_000;
const STARTUP_LINE_LIMIT = 64 * 1024;
const STOP_GRACE_MS = 1_000;

function abortError() {
  return Object.assign(new Error("Room View launch cancelled"), { name: "AbortError", code: "ABORT_ERR" });
}

function launcherError(code, message) {
  return Object.assign(new Error(message), { code });
}

function validateRoomViewInfo(value) {
  if (!value || value.ok !== true || value.command !== "view-serve" || value.host !== "127.0.0.1"
      || value.read_only !== true
      || !Number.isInteger(value.port) || value.port < 1 || value.port > 65535
      || typeof value.url !== "string") {
    throw new Error("Room View returned an invalid startup result");
  }
  let parsed;
  try {
    parsed = new URL(value.url);
  } catch (_error) {
    throw new Error("Room View returned an invalid startup URL");
  }
  if (parsed.protocol !== "http:" || parsed.hostname !== "127.0.0.1" || Number(parsed.port) !== value.port
      || parsed.pathname !== "/" || !parsed.searchParams.get("token")
      || parsed.username || parsed.password || parsed.hash) {
    throw new Error("Room View startup URL is not a valid loopback address");
  }
  return parsed.toString();
}

async function launchRoomView({
  runtime,
  cwd,
  vscode,
  cancellationToken,
  platform = process.platform,
  startupTimeoutMs = STARTUP_TIMEOUT_MS,
  spawn = childProcess.spawn,
} = {}) {
  if (platform !== "linux") throw new Error("Room View is supported only on local Linux VS Code.");
  if (vscode?.env?.remoteName) throw new Error("Room View cannot run from a remote VS Code window.");
  if (vscode?.workspace?.isTrusted !== true) throw new Error("Room View requires a trusted VS Code workspace.");
  if (!runtime || typeof runtime.command !== "string" || typeof runtime.args !== "function"
      || !runtime.env || typeof cwd !== "string" || !cwd || !vscode?.env?.openExternal || !vscode?.Uri?.parse) {
    throw new Error("Room View launcher is missing required runtime or VS Code context");
  }
  if (cancellationToken?.isCancellationRequested) throw abortError();

  const tempDirectory = await fs.mkdtemp(path.join(os.tmpdir(), "josh-room-view-"));
  await fs.chmod(tempDirectory, 0o700);
  const receiptFile = path.join(tempDirectory, "receipt.json");
  const commandArgs = ["view", "serve", "--provider", "room", "--workspace", cwd,
    "--port", "0", "--require-token"];
  let child;
  let closed = false;
  let disposed = false;
  let cleanupPromise;
  let startupTimer;
  let startupResolve;
  let startupReject;
  let stdout = "";
  let settled = false;

  const cleanupDirectory = async () => fs.rm(tempDirectory, { recursive: true, force: true });
  const waitForClose = (timeoutMs) => new Promise((resolve) => {
    if (closed) return resolve(true);
    const timeout = setTimeout(() => resolve(false), timeoutMs);
    child.once("close", () => { clearTimeout(timeout); resolve(true); });
  });
  const signalGroup = (signal) => {
    if (!child?.pid) return;
    try { process.kill(-child.pid, signal); } catch (_error) {
      try { child.kill(signal); } catch (_ignored) { /* process may already be gone */ }
    }
  };
  const dispose = () => {
    if (cleanupPromise) return cleanupPromise;
    disposed = true;
    cleanupPromise = (async () => {
      clearTimeout(startupTimer);
      if (child) {
        signalGroup("SIGTERM");
        if (!closed && !await waitForClose(STOP_GRACE_MS)) {
          signalGroup("SIGKILL");
          try { child.kill("SIGKILL"); } catch (_error) { /* process may already be gone */ }
          await waitForClose(STOP_GRACE_MS);
        }
        // A child process can outlive RCC. Kill the owned process group after the
        // leader closes so the launcher never leaves the server behind.
        signalGroup("SIGKILL");
      }
      await cleanupDirectory();
    })();
    return cleanupPromise;
  };
  const rejectStartup = (error) => {
    if (settled) return;
    settled = true;
    clearTimeout(startupTimer);
    startupReject(error);
  };
  const parseStartup = (line) => {
    if (settled) return;
    try {
      const url = validateRoomViewInfo(JSON.parse(line));
      settled = true;
      clearTimeout(startupTimer);
      startupResolve(url);
    } catch (_error) {
      rejectStartup(launcherError("ROOM_VIEW_STARTUP_INVALID", "Room View returned invalid or unsafe startup data"));
    }
  };

  let startupPromise;
  try {
    if (cancellationToken?.isCancellationRequested) throw abortError();
    const args = runtime.args(commandArgs, receiptFile);
    child = spawn(runtime.command, args, {
      cwd,
      env: runtime.env,
      detached: true,
      stdio: ["ignore", "pipe", "ignore"],
    });
    startupPromise = new Promise((resolve, reject) => {
      startupResolve = resolve;
      startupReject = reject;
      startupTimer = setTimeout(() => rejectStartup(launcherError("ROOM_VIEW_STARTUP_TIMEOUT", "Room View did not start before the startup deadline")), startupTimeoutMs);
      child.stdout.on("data", (chunk) => {
        if (settled) return;
        stdout += chunk.toString();
        if (Buffer.byteLength(stdout) > STARTUP_LINE_LIMIT) {
          rejectStartup(launcherError("ROOM_VIEW_STARTUP_INVALID", "Room View returned an oversized startup result"));
          void dispose();
          return;
        }
        const newline = stdout.indexOf("\n");
        if (newline >= 0) parseStartup(stdout.slice(0, newline));
      });
      child.once("error", () => rejectStartup(launcherError("ROOM_VIEW_SPAWN_FAILED", "Room View process could not be started")));
      child.once("close", () => {
        closed = true;
        if (!settled) rejectStartup(launcherError("ROOM_VIEW_EXITED_EARLY", "Room View process exited before startup completed"));
        void dispose();
      });
    });
  } catch (error) {
    await dispose();
    if (error?.name === "AbortError") throw error;
    throw launcherError("ROOM_VIEW_SPAWN_FAILED", "Room View process could not be started");
  }

  const cancellation = () => {
    rejectStartup(abortError());
    void dispose();
  };
  const subscription = cancellationToken?.onCancellationRequested?.(cancellation);
  if (cancellationToken?.isCancellationRequested) cancellation();

  let url;
  try {
    url = await startupPromise;
    if (disposed || cancellationToken?.isCancellationRequested) throw abortError();
    const opened = await vscode.env.openExternal(vscode.Uri.parse(url));
    if (opened === false) throw launcherError("ROOM_VIEW_OPEN_FAILED", "Room View could not be opened");
    if (disposed || cancellationToken?.isCancellationRequested) throw abortError();
  } catch (error) {
    subscription?.dispose();
    await dispose();
    if (error?.name === "AbortError") throw error;
    if (typeof error?.code === "string" && error.code.startsWith("ROOM_VIEW_")) throw error;
    throw launcherError("ROOM_VIEW_STARTUP_FAILED", "Room View failed during startup");
  }

  subscription?.dispose();
  return {
    url,
    get active() { return !closed && !disposed; },
    dispose,
  };
}

module.exports = { launchRoomView, validateRoomViewInfo };
