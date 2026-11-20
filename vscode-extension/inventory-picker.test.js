"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { pickInventory } = require("./inventory-picker");

test("inventory native picker retains deep explicit picks across pages and global filters", async () => {
  const requests = [];
  const actions = [{ action: "next" }, { path: "deep/nonrecommended" }, { action: "search" }, { action: "review" }];
  const vscode = { ProgressLocation: { Notification: 1 }, window: {
    withProgress: async (_, body) => body({ report() {} }, { isCancellationRequested: false }),
    showQuickPick: async (rows) => { assert.ok(rows.length <= 55); return actions.shift(); },
    showInputBox: async () => "last-9999",
    showInformationMessage: async (message) => assert.match(message, /1 explicit/),
  } };
  const outcome = await pickInventory(vscode, async (request) => {
    requests.push(request);
    if (request.action === "page") return { status: "complete", directories_indexed: 10000,
      candidates: Array.from({ length: 50 }, (_, i) => ({ path: `row-${i}`, evidence_code: "manual-directory" })), next: "row-49" };
    return { status: "complete", directories_indexed: 10000 };
  });
  assert.equal(outcome, "preview-only");
  assert.ok(requests.some((r) => r.after === "row-49"));
  assert.ok(requests.some((r) => r.query === "last-9999" && r.after === ""));
  assert.equal(requests.filter((r) => r.action === "lookup" && r.path === "deep/nonrecommended").length, 2);
});

test("inventory cancellation pauses and resumes without accepting recommendations", async () => {
  let passes = 0;
  const requests = [];
  const vscode = { ProgressLocation: { Notification: 1 }, window: {
    withProgress: async (_, body) => body({ report() {} }, { isCancellationRequested: ++passes === 1 }),
    showQuickPick: async () => passes === 1 ? { label: "Resume discovery" } : undefined,
  } };
  assert.equal(await pickInventory(vscode, async (request) => {
    requests.push(request);
    return { status: passes === 1 ? "scanning" : "complete", candidates: [], directories_indexed: 1 };
  }), "cancelled");
  assert.ok(requests.some((r) => r.action === "cancel"));
  assert.ok(!requests.some((r) => r.action === "lookup"));
});
