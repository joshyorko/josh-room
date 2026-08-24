const fs = require("fs");
const http = require("http");

const REGISTRY_URL = "http://127.0.0.1:5000";

function probeRegistry() {
  return new Promise((resolve, reject) => {
    const request = http.get(`${REGISTRY_URL}/v2/_catalog`, { timeout: 1000 }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        if (response.statusCode !== 200) {
          reject(new Error(`registry returned HTTP ${response.statusCode}`));
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(new Error(`registry returned invalid JSON: ${error.message}`));
        }
      });
    });
    request.on("timeout", () => request.destroy(new Error("registry probe timed out")));
    request.on("error", reject);
  });
}

async function waitForRegistry(
  probe = probeRegistry,
  { timeoutMs = 120000, intervalMs = 500 } = {},
) {
  const deadline = Date.now() + timeoutMs;
  let lastError = new Error("registry did not become ready");
  do {
    try {
      return await probe();
    } catch (error) {
      lastError = error;
    }
    if (Date.now() >= deadline) break;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  } while (true);
  throw new Error(`Hauler registry did not become ready: ${lastError.message}`);
}

function cleanLogLine(line) {
  return String(line).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").trim();
}

function stageForLog(line) {
  const normalized = cleanLogLine(line).toLowerCase();
  if (normalized.includes("listening on") && normalized.includes(":5000")) return "Registry is ready";
  if (normalized.includes("starting registry on port")) return "Starting read-only registry";
  if (normalized.includes("copied artifacts to")) return "Loading registry images";
  if (normalized.includes("hauler/") && normalized.includes(":latest")) return "Inspecting haul contents";
  if (normalized.includes("restore space from library") || normalized.includes("holotree")) {
    return "Preparing RCC environment";
  }
  return undefined;
}

function followLogFile(logPath, onLine, { intervalMs = 100 } = {}) {
  let previous = "";
  try {
    previous = fs.readFileSync(logPath, "utf8");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  let pending = "";
  const poll = () => {
    let current;
    try {
      current = fs.readFileSync(logPath, "utf8");
    } catch (error) {
      if (error.code === "ENOENT") return;
      onLine(`Unable to read ${logPath}: ${error.message}`);
      return;
    }
    if (current === previous) return;
    const added = current.startsWith(previous) ? current.slice(previous.length) : current;
    previous = current;
    const lines = (pending + added).split(/\r?\n/);
    pending = lines.pop() || "";
    for (const line of lines) {
      const cleaned = cleanLogLine(line);
      if (cleaned) onLine(cleaned);
    }
  };
  const timer = setInterval(poll, intervalMs);
  timer.unref?.();
  return {
    dispose: () => {
      poll();
      clearInterval(timer);
    },
  };
}

module.exports = { REGISTRY_URL, cleanLogLine, followLogFile, probeRegistry, stageForLog, waitForRegistry };
