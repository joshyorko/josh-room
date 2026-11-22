"use strict";
// Cheap native dependency qualification before downloading provider/editor assets.
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { prepareWindowsHandoff } = require("../vscode-extension/windows-handoff");

async function main() {
  if (process.platform !== "win32") throw new Error("Native Windows required");
  const evidence = process.env.JOSH_ROOM_WINDOWS_RECOVERY_EVIDENCE;
  if (!evidence) throw new Error("Native handoff evidence directory required");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-empty-handoff-probe-"));
  const receipt = { state: "FAIL", gate: "native-prewrite-handoff-acl", source_sha: process.env.GITHUB_SHA,
    pr_head_sha: process.env.JOSH_ROOM_ACCEPTANCE_HEAD_SHA };
  try {
    await prepareWindowsHandoff(root);
    if (fs.statSync(path.join(root, "material.identity")).size !== 0) throw new Error("Native ACL preflight wrote unexpected content");
    const result = spawnSync(process.env.JOSH_ROOM_ACCEPTANCE_PYTHON, ["-c",
      "import sys; from pathlib import Path; from josh_room.private_paths import verify_private_path; p=Path(sys.argv[1]); verify_private_path(p,directory=True); verify_private_path(p/'material.identity',directory=False)", root],
    { env: { ...process.env, PYTHONPATH: path.resolve(__dirname, "../src") }, encoding: "utf8", timeout: 10000 });
    if (result.status !== 0) throw new Error("Native handoff preflight failed shared ACL verification");
    receipt.state = "PASS";
    console.log("Native empty handoff owner/SYSTEM ACL: PASS");
  } catch (error) {
    receipt.failed_boundary = /^(Native|Windows ACL)/.test(error.message) ? error.message : "Native handoff dependency failed";
    throw error;
  } finally {
    try {
      fs.mkdirSync(evidence, { recursive: true });
      fs.writeFileSync(path.join(evidence, "native-handoff-preflight.json"), JSON.stringify(receipt, null, 2));
    } finally { fs.rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 }); }
  }
}
main().catch(error => { console.error(/^(Native|Windows ACL)/.test(error.message) ? error.message : "Native handoff preflight failed"); process.exitCode = 1; });
