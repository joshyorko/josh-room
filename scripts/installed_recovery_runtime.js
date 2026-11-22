"use strict";
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

function runCandidatePhase(phase, command, args, { env, cwd, record,
  invoke = spawnSync, now = Date.now } = {}) {
  const deadlines = { "acquire-archive": 180000, "interpreter-probe": 60000 };
  if (!Object.hasOwn(deadlines, phase) || typeof record !== "function") throw new Error("Invalid RCC qualification phase");
  const started = now();
  const event = { phase, state: "RUNNING", deadline_ms: deadlines[phase] };
  record({ ...event });
  const result = invoke(command, args, { env, cwd, encoding: "utf8", timeout: deadlines[phase], maxBuffer: 4 * 1024 * 1024 });
  const stderr = String(result.stderr || "");
  const stdout = String(result.stdout || "");
  Object.assign(event, { state: result.error || result.status !== 0 ? "FAIL" : "PASS",
    elapsed_ms: Math.max(0, now() - started), exit_code: Number.isInteger(result.status) ? result.status : null,
    timed_out: result.error?.code === "ETIMEDOUT", stdout_bytes: Buffer.byteLength(stdout), stderr_bytes: Buffer.byteLength(stderr),
    progress_steps: [...stderr.matchAll(/Progress:\s*(\d{1,2})\/(\d{1,2})/g)].map(match => [Number(match[1]), Number(match[2])]).slice(-20),
    progress_categories: ["acquir", "extract", "materializ", "relocat", "verif", "lock"]
      .filter(value => stderr.toLowerCase().includes(value)) });
  record(event);
  if (event.state !== "PASS") throw new Error(`RCC qualification ${phase} ${event.timed_out ? "timed out" : "failed"}`);
  return stdout.trim();
}

function applyInstalledRuntime(extensionPath, runtime, { cache = require.cache,
  realpath = fs.realpathSync.native, platform = process.platform } = {}) {
  const canonical = filename => {
    const actual = realpath(filename).replaceAll("\\", "/");
    return platform === "win32" ? actual.toLowerCase() : actual;
  };
  const expected = canonical(path.join(extensionPath, "extension.js"));
  const matched = [];
  for (const loaded of Object.values(cache)) {
    if (!loaded?.filename || loaded.filename.split(/[\\/]/).pop().toLowerCase() !== "extension.js") continue;
    let actual;
    try { actual = canonical(loaded.filename); } catch { continue; }
    if (actual !== expected || typeof loaded.exports?.__test__?.setRuntimeForTests !== "function") continue;
    loaded.exports.__test__.setRuntimeForTests(runtime);
    matched.push(loaded.exports.__test__);
  }
  if (!matched.length) throw new Error("Activated installed runtime hook was not found");
  return matched;
}
function candidateRuntime(candidate, env) {
  if (!/^[0-9a-f]{40}$/.test(candidate.subject_sha || "")
    || !/^sha256:[0-9a-f]{64}$/.test(candidate.artifact || "")
    || !path.isAbsolute(candidate.command || "")) throw new Error("Invalid explicit RCC qualification input");
  return {
    command: candidate.command,
    args: (args, receiptFile) => ["--no-build", "env", "exec", "--artifact", candidate.artifact,
      "--permissive-local", ...(receiptFile ? ["--receipt-file", receiptFile] : []), "--inherit-streams", "--json",
      "--", "python", path.join(env.JOSH_ROOM_ACCEPTANCE_ROOT, "windows_recovery_controller.py"), ...args, "--json"],
    env, controllerArtifact: candidate.artifact, mode: "managed",
    jatRoot: env.JOSH_ROOM_JAT_ROOT,
  };
}
module.exports = { applyInstalledRuntime, candidateRuntime, runCandidatePhase };
