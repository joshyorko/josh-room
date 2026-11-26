const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { followProgressFile, parseProgressLine, progressDetail } = require("./progress");

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

test("progress only displays measured transfers or counted child restores", () => {
  assert.equal(progressDetail({ stage: "build" }), undefined);
  assert.equal(progressDetail({ stage: "upload", current: 1536, total: 3072 }), "1.50 KiB / 3.00 KiB");
  assert.equal(progressDetail({ stage: "upload", current: 9, total: 8 }), undefined);
  assert.equal(progressDetail({ stage: "catalog-restore", current: 2, total: 5 }), "2 of 5 Rooms");
});
