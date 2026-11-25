"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { notifyDone, notifyError, confirmDestructive } = require("./messaging");

function fixture() {
  const calls = [];
  const vscode = { window: Object.fromEntries(["showInformationMessage", "showErrorMessage", "showWarningMessage"]
    .map(name => [name, (...args) => { calls.push([name, ...args]); return Promise.resolve(args.at(-1)); }])) };
  return { vscode, calls };
}

test("completion has at most one attention action", async () => {
  const { vscode, calls } = fixture();
  await notifyDone(vscode, "Saved Recovery Point", { action: "Copy Receipt" });
  assert.deepEqual(calls[0], ["showInformationMessage", "Saved Recovery Point", "Copy Receipt"]);
});

test("errors expose Retry only for explicitly idempotent actions plus logs", async () => {
  const { vscode, calls } = fixture();
  await notifyError(vscode, "Refresh Catalog", "Check the Storage connection.", { retry: true, idempotent: true });
  await notifyError(vscode, "Restore Room", "Inspect the operation before retrying.", { retry: true });
  assert.deepEqual(calls[0].slice(2), ["Retry", "Show Logs"]);
  assert.deepEqual(calls[1].slice(2), ["Show Logs"]);
});

test("destructive confirmation uses modal verb confirmation", async () => {
  const { vscode, calls } = fixture();
  await confirmDestructive(vscode, "Delete", "Recovery Point point-1");
  assert.deepEqual(calls[0], ["showWarningMessage", "Delete Recovery Point point-1?", { modal: true }, "Delete"]);
});
