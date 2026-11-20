"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { pickInventory } = require("./inventory-picker");

test("inventory native picker retains deep explicit picks across pages and global filters", async () => {
  const requests = [];
  const actions = [{ action: "next" }, { path: "deep/nonrecommended" }, { action: "search" }, { action: "review" }, { action: "finish" }];
  const vscode = { ProgressLocation: { Notification: 1 }, window: {
    withProgress: async (_, body) => body({ report() {} }, { isCancellationRequested: false }),
    showQuickPick: async (rows) => { assert.ok(rows.length <= 56); return actions.shift(); },
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
  assert.equal(requests.filter((r) => r.action === "lookup" && (r.path === "deep/nonrecommended" || r.id === "deep/nonrecommended")).length, 2);
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

test("inventory invalid manual lookup preserves previous picks and the session", async () => {
  const actions = [{ path: "safe", id: "7" }, { action: "manual" }, { action: "review" }, { action: "finish" }];
  let warnings = 0;
  const vscode = { ProgressLocation: { Notification: 1 }, window: {
    withProgress: async (_, body) => body({ report() {} }, { isCancellationRequested: false }),
    showQuickPick: async () => actions.shift(),
    showInputBox: async () => "typo",
    showWarningMessage: async () => { warnings++; },
    showInformationMessage: async (message) => assert.match(message, /1 explicit/),
  } };
  assert.equal(await pickInventory(vscode, async (request) => {
    if (request.action === "lookup" && request.path === "typo") {
      const error = new Error("synthetic invalid lookup"); error.recoverable = true; throw error;
    }
    return { status: "complete", directories_indexed: 1, candidates: [{ path: "safe", id: "7" }], next: null };
  }), "preview-only");
  assert.equal(warnings, 1);
});

test("inventory manual and browse selections share canonical identity and Finish failure retains review", async () => {
  const actions = [{ path: "safe", id: "7" }, { action: "manual" }, { action: "review" }, { action: "finish" }, { action: "finish" }];
  let lookup = 0;
  let warnings = 0;
  const vscode = { ProgressLocation: { Notification: 1 }, window: {
    withProgress: async (_, body) => body({ report() {} }, { isCancellationRequested: false }),
    showQuickPick: async () => actions.shift(),
    showInputBox: async () => "safe",
    showWarningMessage: async () => { warnings++; },
    showInformationMessage: async (message) => assert.match(message, /1 explicit/),
  } };
  assert.equal(await pickInventory(vscode, async (request) => {
    if (request.action === "lookup" && ++lookup === 3) {
      const error = new Error("synthetic unavailable"); error.recoverable = true; throw error;
    }
    return { id: "7", status: "complete", candidates: [{ path: "safe", id: "7" }], next: null };
  }), "preview-only");
  assert.equal(warnings, 1);
  assert.equal(lookup, 4);
});

test("inventory oversized pasted path and search never enter the protocol", async () => {
  const actions = [{ action: "manual" }, { action: "search" }, { action: "review" }, { action: "finish" }];
  let warnings = 0;
  const vscode = { ProgressLocation: { Notification: 1 }, window: {
    withProgress: async (_, body) => body({ report() {} }, { isCancellationRequested: false }),
    showQuickPick: async () => actions.shift(),
    showInputBox: async () => "x".repeat(130000),
    showWarningMessage: async () => { warnings++; },
    showInformationMessage: async (message) => assert.match(message, /0 explicit/),
  } };
  assert.equal(await pickInventory(vscode, async (request) => {
    assert.ok(JSON.stringify(request).length < 1000);
    return { status: "complete", candidates: [], next: null };
  }), "preview-only");
  assert.equal(warnings, 2);
});
