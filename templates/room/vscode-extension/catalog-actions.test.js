"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { saveCatalog, enterChild, relativeSource } = require("./catalog-actions");

test("catalog confirmation binds child, parent and local-only selections before Save", async () => {
  const dialogs = [[{ fsPath: "/synthetic/root" }], [{ fsPath: "/synthetic/root/a" }], [{ fsPath: "/synthetic/root/notes.txt" }]];
  const picks = [{ id: "content" }, { dimension: { id: "dimension", provider: "minio", encryption_domain_id: "00000000-0000-4000-8000-000000000001" } }];
  let approved = false;
  const vscode = { window: {
    showOpenDialog: async () => dialogs.shift(), showQuickPick: async () => picks.shift(),
    showWarningMessage: async (_, options, choice) => { assert.match(options.detail, /unprotected/); approved = true; return choice; },
    showInformationMessage: async () => {},
  } };
  const result = await saveCatalog(vscode, {
    dimensions: async () => [{ id: "dimension", encryption_domain_id: "00000000-0000-4000-8000-000000000001" }],
    plan: async (selection) => { assert.equal(approved, false); assert.deepEqual(selection.assignments, [{ path: ".", outcome: "local-only" }, { path: "a", outcome: "child" }, { path: "notes.txt", outcome: "parent" }]); return { approval_sha256: "approved" }; },
    save: async (selection, approval) => { assert.equal(approved, true); assert.equal(approval, "approved"); return { ok: true, namespace: selection.namespace, parent_snapshot_id: "exact-pin" }; },
  });
  assert.equal(result.ok, true);
});

test("cancelling catalog review never invokes Save", async () => {
  let dialog = 0;
  const vscode = { window: {
    showOpenDialog: async () => [{ fsPath: ++dialog === 1 ? "/synthetic/root" : "/synthetic/root/a" }],
    showQuickPick: async (rows) => rows[0], showWarningMessage: async () => undefined,
  } };
  const result = await saveCatalog(vscode, { dimensions: async () => [{ id: "dimension", encryption_domain_id: "00000000-0000-4000-8000-000000000001" }],
    plan: async () => ({ approval_sha256: "digest" }), save: async () => { throw new Error("unapproved Save"); } });
  assert.equal(result, "cancelled");
});

test("whole child opens only after verified immutable entry", async () => {
  const inputs = ["1".repeat(32), "projects/a"];
  const vscode = { window: { showInputBox: async () => inputs.shift(), showInformationMessage: async () => {} } };
  const marker = { dimension_id: "dimension", project_id: "parent", snapshot_id: "parent-pin" };
  let opened;
  await enterChild(vscode, { root: "/synthetic/root", marker,
    enter: async (args) => { assert.equal(args.marker.snapshot_id, "parent-pin"); return { ok: true, marker_verified: true }; },
    open: async (target) => { opened = target; } });
  assert.equal(opened, "/synthetic/root/projects/a");
  assert.throws(() => relativeSource("/synthetic/root", "/synthetic/outside"), /inside/);
});
