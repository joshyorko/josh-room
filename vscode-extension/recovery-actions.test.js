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

test("validated current child binding rejects replacement guards and copied markers", { skip: process.platform !== "linux" }, (t) => {
  const fs = require("node:fs");
  const os = require("node:os");
  const path = require("node:path");
  const crypto = require("node:crypto");
  const { currentChildPin } = require("./recovery-actions");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "josh-room-recovery-"));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const marker = {
    format_version: 4, dimension_id: "archive", encryption_domain_id: "domain",
    project_id: "room", snapshot_id: "jat-" + "a".repeat(32), display_name: "Child Room",
    composition_namespace: "1".repeat(32), workspace_signature: "b".repeat(64),
    signature_algorithm: "josh-room-stat-v1", capture_policy_sha256: "c".repeat(64),
    workspace_path_sha256: crypto.createHash("sha256").update(fs.realpathSync(root)).digest("hex"),
  };
  const file = path.join(root, ".josh-room.json");
  const guard = path.join(root, ".josh-room-split.json");
  fs.writeFileSync(file, JSON.stringify(marker));
  const binding = { namespace: marker.composition_namespace, room_id: marker.project_id, snapshot_id: marker.snapshot_id };
  fs.writeFileSync(guard, JSON.stringify(binding));
  assert.deepEqual(currentChildPin(root), { project_id: "room", dimension_id: "archive", namespace: binding.namespace,
    encryption_domain_id: "domain", snapshot_id: binding.snapshot_id });
  fs.writeFileSync(guard, JSON.stringify({ ...binding, snapshot_id: "another" }));
  assert.throws(() => currentChildPin(root), /guard binding/);
  fs.unlinkSync(guard);
  fs.symlinkSync(file, guard);
  assert.throws(() => currentChildPin(root));
  fs.writeFileSync(file, JSON.stringify({ ...marker, workspace_path_sha256: "0".repeat(64) }));
  assert.throws(() => currentChildPin(root), /marker binding/);
});

test("child browsing pins the authenticated namespace and domain on both sides of a diff", async () => {
  const child = { ...pin, namespace: "1".repeat(32), encryption_domain_id: "domain" };
  const result = page({ namespace: child.namespace, encryption_domain_id: child.encryption_domain_id });
  await assert.rejects(browseRecovery({}, child, async () => page()), /identity changed/);
  const picks = [{ entry: { type: "file" }, path: "a" }, "Compare with recovery point…"];
  const vscode = { workspace: { isTrusted: true }, window: { showQuickPick: async () => picks.shift(),
    showInputBox: async () => "other" } };
  const calls = [];
  await assert.rejects(browseRecovery(vscode, child, async args => {
    calls.push(args);
    return args[1] === "diff" ? { ...result, compare_to: { ...result, namespace: "2".repeat(32), snapshot_id: "other" } } : result;
  }), /identity changed/);
  assert.deepEqual(calls.map(args => args[1]), ["tree", "diff"]);
  assert.ok(calls.every(args => args[args.indexOf("--namespace") + 1] === child.namespace
    && args[args.indexOf("--expected-domain") + 1] === child.encryption_domain_id));
});
