const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  createProgressTracker,
  followProgressFile,
  formatBytes,
  formatProgressDisplay,
  operationKind,
  parseProgressLine,
  renderProgressBar,
  renderStatusBar,
} = require("./progress");

test("parseProgressLine accepts the stable display contract", () => {
  assert.deepEqual(
    parseProgressLine('{"format_version":1,"stage":"upload","message":"Uploading","current":4,"total":10}'),
    { format_version: 1, stage: "upload", message: "Uploading", current: 4, total: 10 },
  );
  assert.equal(parseProgressLine('{"format_version":2,"stage":"upload","message":"Uploading"}'), undefined);
  assert.equal(parseProgressLine("decorative output"), undefined);
});

test("followProgressFile streams only fresh structured events", async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-progress-test-"));
  const progressPath = path.join(directory, "progress.jsonl");
  fs.writeFileSync(progressPath, '{"format_version":1,"stage":"old","message":"Old"}\n');
  const events = [];
  const follower = followProgressFile(progressPath, (event) => events.push(event), { intervalMs: 5 });
  fs.writeFileSync(progressPath, '{"format_version":1,"stage":"build","message":"Building portable haul"}\n');
  await new Promise((resolve, reject) => {
    const deadline = Date.now() + 500;
    const timer = setInterval(() => {
      if (events.length === 1) {
        clearInterval(timer);
        resolve();
      } else if (Date.now() >= deadline) {
        clearInterval(timer);
        reject(new Error(`timed out waiting for progress: ${JSON.stringify(events)}`));
      }
    }, 5);
  });
  follower.dispose();
  fs.rmSync(directory, { recursive: true, force: true });
  assert.equal(events[0].message, "Building portable haul");
});

test("operationKind selects an honest progress profile from CLI arguments", () => {
  assert.equal(operationKind(["snapshot", "create", "Heather"]), "save");
  assert.equal(operationKind(["hydrate", "heather"]), "restore");
  assert.equal(operationKind(["snapshots", "remove", "heather", "one"]), "remove");
  assert.equal(operationKind(["jat", "build"]), "jat-build");
});

test("progress tracker combines weighted stages with real transfer percentage", () => {
  const tracker = createProgressTracker("save");
  assert.deepEqual(tracker.update({ stage: "auth", message: "Cloudflare session is ready" }), {
    bar: "██░░░░░░░░░░░░░░░░░░",
    indeterminate: false,
    message: "Cloudflare session is ready",
    percent: 8,
    transfer: undefined,
  });
  const upload = tracker.update({
    stage: "upload",
    message: "Uploading encrypted Room",
    current: 3 * 1024 * 1024 * 1024,
    total: 4 * 1024 * 1024 * 1024,
  });
  assert.equal(upload.percent, 84);
  assert.equal(upload.transfer, "3.00 / 4.00 GiB");
  assert.equal(upload.bar, "█████████████████░░░");
  assert.equal(tracker.update({ stage: "complete", message: "Room saved safely" }).percent, 100);
});

test("JAT phases remain visibly indeterminate instead of inventing precision", () => {
  const state = createProgressTracker("save").update({ stage: "jat", message: "Running JAT Build through RCC" });
  assert.equal(state.indeterminate, true);
  assert.equal(state.percent, undefined);
  assert.equal(
    formatProgressDisplay("Saving Heather", "save", state).statusText,
    "$(sync~spin) ▓▒░░░░░░▒▓ Saving Heather",
  );
});

test("progress display formatting is compact and deterministic", () => {
  assert.equal(formatBytes(1536), "1.50 KiB");
  assert.equal(renderProgressBar(68), "██████████████░░░░░░");
  assert.deepEqual(formatProgressDisplay("Saving Heather Mk1 Room", "save", {
    bar: "██████████████░░░░░░",
    indeterminate: false,
    message: "Uploading encrypted Room",
    percent: 68,
    transfer: "1.46 / 2.15 GiB",
  }), {
    logLine: "██████████████░░░░░░ 68% · Uploading encrypted Room · 1.46 / 2.15 GiB",
    notification: "Uploading encrypted Room · 1.46 / 2.15 GiB · 68%",
    statusText: "$(sync~spin) ███████◆░░ 68% Saving Heather Mk1 Room",
    tooltip: "Saving Heather Mk1 Room\n██████████████░░░░░░ 68%\nUploading encrypted Room · 1.46 / 2.15 GiB",
  });
  assert.equal(renderStatusBar(68, 0), "███████◆░░");
  assert.equal(renderStatusBar(68, 1), "███████░◆░");
});
