"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { browseRecovery } = require("./recovery-actions");
const pin = { project_id: "room", dimension_id: "archive", snapshot_id: "old" };
const page = (extra = {}) => ({ room_id: "room", dimension_id: "archive", snapshot_id: "old", entries: [], next_offset: null, ...extra });

test("paging and cancellation retain exact original pin", async () => {
  const calls = [];
  let picks = 0;
  const vscode = { window: { showQuickPick: async (choices) => ++picks === 1 ? choices.find(c => c.action === "next") : undefined } };
  const context = { ...pin };
  const result = await browseRecovery(vscode, context, async args => {
    calls.push(args);
    context.snapshot_id = "new";
    return page({ next_offset: calls.length === 1 ? 100 : null });
  });
  assert.equal(result, "cancelled");
  assert.equal(calls.length, 2);
  assert.ok(calls.every(args => args[args.indexOf("--snapshot") + 1] === "old"));
  assert.equal(calls[1][calls[1].indexOf("--offset") + 1], "100");
  assert.ok(calls.every(args => args[1] === "tree"));
});

test("wrong historical identity fails before any picker", async () => {
  await assert.rejects(browseRecovery({}, pin, async () => page({ snapshot_id: "new" })), /identity changed/);
});

test("cancel recovery destination makes no write call", async () => {
  const picks = [{ label: "a", entry: { type: "file" }, path: "a" }, "Recover selection…", undefined];
  const calls = [];
  const vscode = { workspace: { isTrusted: true }, window: { showQuickPick: async () => picks.shift(), showSaveDialog: async () => undefined } };
  await browseRecovery(vscode, pin, async args => { calls.push(args); return page(); });
  assert.ok(calls.every(args => args[1] === "tree"));
});

test("untrusted workspace cannot recover", async () => {
  const picks = [{ entry: { type: "file" }, path: "a" }, "Recover selection…"];
  const vscode = { workspace: { isTrusted: false }, window: { showQuickPick: async () => picks.shift() } };
  await assert.rejects(browseRecovery(vscode, pin, async () => page()), /Trust/);
});
