"use strict";

const childProcess = require("node:child_process");
const fsConstants = require("node:fs").constants;
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");

const STARTUP_TIMEOUT_MS = 30_000;
const STARTUP_LINE_LIMIT = 64 * 1024;
const STARTUP_TOTAL_LIMIT = 256 * 1024;
const STDERR_LIMIT = 16 * 1024;
const STDERR_LINE_LIMIT = 2 * 1024;
const RECEIPT_LIMIT = 64 * 1024;
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
  kill = process.kill,
  onDiagnostic,
  sanitizeDiagnostic,
} = {}) {
  if (platform !== "linux") throw new Error("Room View is supported only on local Linux VS Code.");
  if (vscode?.env?.remoteName) throw new Error("Room View cannot run from a remote VS Code window.");
  if (vscode?.UIKind && vscode.env?.uiKind === vscode.UIKind.Web) throw new Error("Room View is unavailable in VS Code for the web.");
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
  let groupCleaned = false;
  let cleanupPromise;
  let startupTimer;
  let startupResolve;
  let startupReject;
  let stdout = "";
  let stdoutBytes = 0;
  let stderrBuffer = "";
  let stderrBytes = 0;
  let settled = false;

  const cleanupDirectory = async () => fs.rm(tempDirectory, { recursive: true, force: true });
  const redactDiagnostic = (text) => text.replace(/([?&]token=)[^&#\s]*/gi, "$1[redacted]");
  const validateReceipt = async () => {
    let handle;
    try {
      handle = await fs.open(receiptFile, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_NONBLOCK);
    } catch (error) {
      if (error.code === "ENOENT") return;
      throw launcherError("ROOM_VIEW_RECEIPT_INVALID", "Managed Room View returned an unsafe execution receipt");
    }
    try {
      const stat = await handle.stat();
      if (!stat.isFile() || stat.size > RECEIPT_LIMIT) {
        throw launcherError("ROOM_VIEW_RECEIPT_INVALID", "Managed Room View returned an invalid execution receipt");
      }
      let receipt;
      try {
        const data = Buffer.alloc(RECEIPT_LIMIT + 1);
        let bytesRead = 0;
        while (bytesRead < data.length) {
          const result = await handle.read(data, bytesRead, data.length - bytesRead, bytesRead);
          if (!result.bytesRead) break;
          bytesRead += result.bytesRead;
        }
        if (bytesRead > RECEIPT_LIMIT) {
          throw launcherError("ROOM_VIEW_RECEIPT_INVALID", "Managed Room View returned an oversized execution receipt");
        }
        receipt = JSON.parse(data.subarray(0, bytesRead).toString("utf8"));
      } catch (_error) {
        if (_error?.code === "ROOM_VIEW_RECEIPT_INVALID") throw _error;
        throw launcherError("ROOM_VIEW_RECEIPT_INVALID", "Managed Room View returned an invalid execution receipt");
      }
      if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)
          || (runtime.controllerArtifact
            && (receipt.artifactDigest ?? receipt.artifact_digest) !== runtime.controllerArtifact)) {
        throw launcherError("ROOM_VIEW_RECEIPT_MISMATCH", "Managed Room View execution receipt did not match the selected controller");
      }
    } finally {
      await handle.close();
    }
  };
  const waitForClose = (timeoutMs) => new Promise((resolve) => {
    if (closed) return resolve(true);
    const timeout = setTimeout(() => resolve(false), timeoutMs);
    child.once("close", () => { clearTimeout(timeout); resolve(true); });
  });
  const signalGroup = (signal) => {
    if (!child?.pid || (signal === "SIGKILL" && groupCleaned)) return;
    try { kill(-child.pid, signal); } catch (error) {
      if (error.code !== "ESRCH") {
        try { child.kill(signal); } catch (_ignored) { /* process may already be gone */ }
      }
    }
    if (signal === "SIGKILL") groupCleaned = true;
  };
  const dispose = () => {
    if (cleanupPromise) return cleanupPromise;
    disposed = true;
    cleanupPromise = (async () => {
      clearTimeout(startupTimer);
      if (child?.pid && !closed) {
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
    const text = line.trim();
    if (!text) return;
    let value;
    try {
      value = JSON.parse(text);
    } catch (_error) {
      return; // RCC may emit bounded status preambles before controller JSON.
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    if (value.ok === false) {
      rejectStartup(launcherError("ROOM_VIEW_CONTROLLER_FAILED", "Room View controller refused startup"));
      return;
    }
    if (value.command !== "view-serve") return;
    try {
      const url = validateRoomViewInfo(value);
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
      stdio: ["ignore", "pipe", "pipe"],
    });
    startupPromise = new Promise((resolve, reject) => {
      startupResolve = resolve;
      startupReject = reject;
      startupTimer = setTimeout(() => rejectStartup(launcherError("ROOM_VIEW_STARTUP_TIMEOUT", "Room View did not start before the startup deadline")), startupTimeoutMs);
      child.stdout.on("data", (chunk) => {
        if (settled) return;
        const text = chunk.toString();
        stdoutBytes += Buffer.byteLength(text);
        if (stdoutBytes > STARTUP_TOTAL_LIMIT) {
          rejectStartup(launcherError("ROOM_VIEW_STARTUP_INVALID", "Room View returned an oversized startup result"));
          void dispose().catch(() => {});
          return;
        }
        stdout += text;
        let newline;
        while ((newline = stdout.indexOf("\n")) >= 0 && !settled) {
          const line = stdout.slice(0, newline);
          stdout = stdout.slice(newline + 1);
          if (Buffer.byteLength(line) > STARTUP_LINE_LIMIT) {
            rejectStartup(launcherError("ROOM_VIEW_STARTUP_INVALID", "Room View returned an oversized startup line"));
            void dispose().catch(() => {});
            break;
          }
          parseStartup(line);
        }
        if (!settled && Buffer.byteLength(stdout) > STARTUP_LINE_LIMIT) {
          rejectStartup(launcherError("ROOM_VIEW_STARTUP_INVALID", "Room View returned an oversized startup line"));
          void dispose().catch(() => {});
        }
      });
      child.stderr?.on("data", (chunk) => {
        if (typeof onDiagnostic !== "function" || typeof sanitizeDiagnostic !== "function") return;
        const text = chunk.toString();
        stderrBytes += Buffer.byteLength(text);
        if (stderrBytes > STDERR_LIMIT) return;
        stderrBuffer += text;
        let newline;
        while ((newline = stderrBuffer.indexOf("\n")) >= 0) {
          const line = stderrBuffer.slice(0, newline);
          stderrBuffer = stderrBuffer.slice(newline + 1);
          if (Buffer.byteLength(line) <= STDERR_LINE_LIMIT) {
            try {
              const safe = sanitizeDiagnostic(redactDiagnostic(line));
              if (typeof safe === "string" && safe) onDiagnostic(redactDiagnostic(safe));
            } catch (_error) { /* diagnostic rendering must not affect startup */ }
          }
        }
        if (Buffer.byteLength(stderrBuffer) > STDERR_LINE_LIMIT) stderrBuffer = "";
      });
      child.once("error", () => {
        closed = true;
        rejectStartup(launcherError("ROOM_VIEW_SPAWN_FAILED", "Room View process could not be started"));
        void dispose().catch(() => {});
      });
      child.once("close", async () => {
        closed = true;
        if (!settled && stdout.trim()) parseStartup(stdout);
        let receiptError;
        try {
          await validateReceipt();
        } catch (error) {
          receiptError = error;
        }
        if (!settled) {
          if (receiptError) {
            rejectStartup(receiptError);
          } else {
            rejectStartup(launcherError("ROOM_VIEW_EXITED_EARLY", "Room View process exited before startup completed"));
          }
        } else if (receiptError && typeof onDiagnostic === "function") {
          try {
            const safe = sanitizeDiagnostic?.(receiptError.message) || receiptError.message;
            if (safe) onDiagnostic(redactDiagnostic(safe));
          } catch (_error) { /* diagnostics must not affect cleanup */ }
        }
        if (child.pid && !groupCleaned) {
          signalGroup("SIGKILL");
        }
        if (typeof onDiagnostic === "function" && typeof sanitizeDiagnostic === "function" && stderrBuffer) {
          try {
            const safe = sanitizeDiagnostic(redactDiagnostic(stderrBuffer));
            if (typeof safe === "string" && safe) onDiagnostic(redactDiagnostic(safe));
          } catch (_error) { /* diagnostic rendering must not affect cleanup */ }
        }
        void dispose().catch(() => {});
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
