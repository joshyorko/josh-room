"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { saveCatalog, enterChild, relativeSource, updateRoom } = require("./catalog-actions");

test("catalog confirmation binds child, parent and local-only selections before Save", async () => {
  const dialogs = [[{ fsPath: "/synthetic/root" }], [{ fsPath: "/synthetic/root/a" }], [{ fsPath: "/synthetic/root/notes.txt" }]];
  const picks = [{ id: "policy" }, { id: "content" }, { dimension: { id: "dimension", provider: "minio", encryption_domain_id: "00000000-0000-4000-8000-000000000001" } }];
  let approved = false;
  const vscode = { window: {
    showOpenDialog: async () => dialogs.shift(), showQuickPick: async () => picks.shift(),
    showWarningMessage: async (_, options, choice) => { assert.match(options.detail, /unprotected/); assert.match(options.detail, /a.*full-repository/); approved = true; return choice; },
    showInformationMessage: async () => {},
  } };
  const result = await saveCatalog(vscode, {
    dimensions: async () => [{ id: "dimension", encryption_domain_id: "00000000-0000-4000-8000-000000000001" }],
    plan: async (selection) => { assert.equal(approved, false); assert.deepEqual(selection.assignments, [{ path: ".", outcome: "local-only" }, { path: "a", outcome: "child" }, { path: "notes.txt", outcome: "parent" }]); return { request: { child_evidence: { a: { capture_mode: "full-repository" } } }, approval_sha256: "approved" }; },
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
    plan: async () => ({ request: { child_evidence: { a: { capture_mode: "source-only" } } }, approval_sha256: "digest" }), save: async () => { throw new Error("unapproved Save"); } });
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

test("child Save persists exact approved retry before writing and clears only on verification", async () => {
  const marker = { composition_namespace: "1".repeat(32), project_id: "child", snapshot_id: "jat-old" };
  let pending;
  const vscode = { window: { showWarningMessage: async (_text, options, action) => {
    assert.match(options.detail, /full-repository/); assert.match(options.detail, /Parent pins remain unchanged/); return action;
  }, showInformationMessage: async () => {} } };
  const callbacks = { marker, kind: "child", remember: async (ticket) => { pending = ticket; },
    plan: async (ticket) => { assert.equal(ticket.baseline, "jat-old"); return { request: { plan: { capture_mode: "full-repository" } }, changed: true, approval_sha256: "approved", snapshot_id: "jat-new" }; } };
  await assert.rejects(updateRoom(vscode, { ...callbacks, save: async (ticket) => {
    assert.deepEqual(ticket, pending); throw new Error("lost reply");
  } }), /lost reply/);
  assert.equal(pending.approval, "approved");
  const operation = pending.operationId;
  await updateRoom(vscode, { ...callbacks, pending, save: async (ticket) => {
    assert.equal(ticket.operationId, operation); return { ok: true, marker_verified: true, snapshot_id: "jat-new" };
  } });
  assert.equal(pending, undefined);
});

test("cancelled or changed pending approval cannot invoke update", async () => {
  const marker = { composition_namespace: "1".repeat(32), project_id: "child", snapshot_id: "jat-old" };
  const callbacks = { marker, kind: "child", remember: async () => { throw new Error("unexpected receipt"); },
    plan: async () => ({ request: { plan: { capture_mode: "source-only" } }, changed: true, approval_sha256: "new", snapshot_id: "jat-new" }),
    save: async () => { throw new Error("unexpected write"); } };
  const vscode = { window: { showWarningMessage: async () => undefined } };
  assert.equal(await updateRoom(vscode, callbacks), "cancelled");
  await assert.rejects(updateRoom(vscode, { ...callbacks, pending: { namespace: marker.composition_namespace,
    room: "child", baseline: "jat-old", operationId: "operation", approval: "old" } }), /Pending approval changed/);
});

test("parent refresh confirms exact path and pin without rewriting the working folder", async () => {
  const marker = { composition_namespace: "1".repeat(32), project_id: "parent", snapshot_id: "jat-parent" };
  const inputs = ["projects/a", "jat-child-new"];
  const vscode = { window: { showInputBox: async () => inputs.shift(), showWarningMessage: async (_text, options, action) => {
    assert.match(options.detail, /projects\/a/); assert.match(options.detail, /jat-child-new/);
    assert.match(options.detail, /working folders remain unchanged/); return action;
  }, showInformationMessage: async () => {} } };
  await updateRoom(vscode, { marker, kind: "parent", remember: async () => {},
    plan: async (ticket) => { assert.equal(ticket.pin, "jat-child-new"); return { changed: true, approval_sha256: "digest", snapshot_id: "jat-parent-new" }; },
    save: async (ticket) => { assert.equal(ticket.child, "projects/a"); return { ok: true, snapshot_id: "jat-parent-new" }; } });
});
