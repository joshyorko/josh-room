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

module.exports = { REGISTRY_URL, probeRegistry, waitForRegistry };
