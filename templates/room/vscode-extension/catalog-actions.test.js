"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { saveCatalog, enterChild, relativeSource, updateRoom } = require("./catalog-actions");

const binding = { provider: "minio", dimension_id: "dimension", connection_id: "synthetic-connection",
  encryption_domain_id: "00000000-0000-4000-8000-000000000001", physical_bucket: "minio:https://storage.example.invalid:fixture" };

test("catalog confirmation binds child, parent and local-only selections before Save", async () => {
  const dialogs = [[{ fsPath: "/synthetic/root" }], [{ fsPath: "/synthetic/root/notes.txt" }]];
  const picks = [{ id: "parent" }, { id: "continue" }, { dimension: { id: "dimension", provider: "minio", display_name: "Storage", endpoint: "https://STORAGE.example.invalid:443/", bucket: "fixture", connection_id: "synthetic-connection" } }];
  let approved = false;
  const vscode = { Uri: { file: fsPath => ({ fsPath }) }, window: {
    showInputBox: () => assert.fail("Catalog creation requested a manual UUID"),
    showOpenDialog: async () => dialogs.shift(), showQuickPick: async () => picks.shift(),
    showWarningMessage: async (_, options, choice) => { assert.match(options.detail, /Storage.*MinIO.*synthetic-connection/i); assert.ok(options.detail.includes(binding.physical_bucket)); assert.match(options.detail, /unprotected/); assert.match(options.detail, /a.*Files and Git history/); approved = true; return choice; },
    showInformationMessage: async () => {},
  } };
  const result = await saveCatalog(vscode, {
    selectChildren: async () => ["a"], dimensions: async () => [{ id: "dimension", provider: "minio", encryption_domain_id: "00000000-0000-4000-8000-000000000001" }],
    plan: async (selection) => { assert.equal(approved, false); assert.deepEqual(selection.assignments, [{ path: ".", outcome: "local-only" }, { path: "a", outcome: "child" }, { path: "notes.txt", outcome: "parent" }]); return { request: { destination_binding: binding, child_evidence: { a: { capture_mode: "full-repository" } } }, approval_sha256: "approved" }; },
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
  const result = await saveCatalog(vscode, { selectChildren: async () => ["a"], dimensions: async () => [{ id: "dimension", provider: "minio", encryption_domain_id: "00000000-0000-4000-8000-000000000001" }],
    plan: async () => ({ request: { destination_binding: binding, child_evidence: { a: { capture_mode: "source-only" } } }, approval_sha256: "digest" }), save: async () => { throw new Error("unapproved Save"); } });
  assert.equal(result, "cancelled");
});

const parentMarker = { composition_namespace: "1".repeat(32), dimension_id: "dimension", project_id: "parent", snapshot_id: "parent-pin" };
const children = { ok: true, namespace: parentMarker.composition_namespace, parent_room_id: "parent", parent_snapshot_id: "parent-pin",
  current_parent: true, children: [{ path: "projects/a", room_id: "a", snapshot_id: "pinned-a",
    recovery_points: [{ snapshot_id: "pinned-a", created_at: "old" }, { snapshot_id: "jat-child-new", created_at: "new" }] }] };

test("whole child opens only after verified immutable entry", async () => {
  const vscode = { window: { showQuickPick: async rows => rows[0], showInformationMessage: async () => {} } };
  let opened;
  const args = { root: "/synthetic/root", marker: parentMarker, listChildren: async () => children,
    enter: async selection => {
      assert.equal(selection.namespace, parentMarker.composition_namespace);
      assert.equal(selection.child, "projects/a");
      return { ok: true, marker_verified: true, namespace: selection.namespace, snapshot_id: "pinned-a", room_id: "a" };
    }, open: async target => { opened = target; } };
  await enterChild(vscode, args);
  assert.equal(opened, "/synthetic/root/projects/a");
  await assert.rejects(enterChild(vscode, { ...args, enter: async () => ({ ok: true, marker_verified: true, snapshot_id: "other" }),
    open: assert.fail }), /did not verify/);
  await assert.rejects(enterChild(vscode, { ...args, listChildren: async () => ({ ...children, parent_snapshot_id: "stale" }),
    enter: assert.fail }), /verified parent changed/);
  assert.equal(await enterChild({ window: { showQuickPick: async () => undefined } }, { ...args, enter: assert.fail }), "cancelled");
  assert.throws(() => relativeSource("/synthetic/root", "/synthetic/outside"), /inside/);
});

test("child Save persists exact approved retry before writing and clears only on verification", async () => {
  const marker = { composition_namespace: "1".repeat(32), project_id: "child", snapshot_id: "jat-old" };
  let pending;
  const vscode = { window: { showWarningMessage: async (_text, options, action) => {
    assert.match(options.detail, /Files and Git history/); assert.match(options.detail, /Parent pins remain unchanged/); return action;
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
  const marker = parentMarker;
  const vscode = { window: { showQuickPick: async rows => rows[0], showWarningMessage: async (_text, options, action) => {
    assert.match(options.detail, /projects\/a/); assert.match(options.detail, /jat-child-new/);
    assert.match(options.detail, /working folders remain unchanged/); return action;
  }, showInformationMessage: async () => {} } };
  await updateRoom(vscode, { marker, kind: "parent", listChildren: async () => children, remember: async () => {},
    plan: async (ticket) => { assert.equal(ticket.pin, "jat-child-new"); return { changed: true, approval_sha256: "digest", snapshot_id: "jat-parent-new" }; },
    save: async (ticket) => { assert.equal(ticket.child, "projects/a"); return { ok: true, snapshot_id: "jat-parent-new" }; } });
});

test("child-specific mode and component inputs reach both plan and approved capture", async () => {
  const source = "/synthetic/work";
  const children = ["projects/a", "projects/b"];
  const components = { rcc_environment: "omit", homebrew_recovery: "refresh", hauler_content: "omit" };
  const options = { component_selection: { format_version: 1, components }, homebrew_directory: "/synthetic/recipe" };
  const dialogs = [[{ fsPath: source }]];
  const picks = [{ id: "git" }, rows => [rows[1]], { id: "source-only" },
    { id: "components" }, rows => [rows[0]], { id: "continue" },
    { dimension: { id: "test", provider: "minio", encryption_domain_id: "00000000-0000-4000-8000-000000000001" } }];
  const vscode = { window: {
    showOpenDialog: async () => dialogs.shift(), showQuickPick: async rows => {
      const value = picks.shift(); return typeof value === "function" ? value(rows) : value;
    },
    showWarningMessage: async (_title, settings, action) => {
      assert.match(settings.detail, /projects\/a.*Files and Git history.*Homebrew recovery \(capture\)/);
      assert.match(settings.detail, /projects\/b.*Files only.*Workspace only/);
      return action;
    },
    showInformationMessage: async () => {},
  } };
  let approved;
  await saveCatalog(vscode, {
    selectChildren: async () => children, dimensions: async () => [], chooseComponents: async sources => {
      assert.deepEqual(sources, [source + "/" + children[0]]);
      return options;
    },
    plan: async selection => {
      approved = selection;
      assert.deepEqual(selection.assignments.slice(1), [
        { path: children[0], outcome: "child", component_options: options },
        { path: children[1], outcome: "child", capture_mode: "source-only" },
      ]);
      return { approval_sha256: "test", request: { destination_binding: { ...binding, dimension_id: "test" }, child_evidence: {
        [children[0]]: { capture_mode: "full-repository", component_plan: { selection: options.component_selection } },
        [children[1]]: { capture_mode: "source-only" },
      } } };
    },
    save: async (selection, approval) => {
      assert.deepEqual(selection, approved); assert.equal(approval, "test");
      return { ok: true, namespace: selection.namespace, parent_snapshot_id: "parent" };
    },
  });
});

test("retry retains approval without persisting explicit external component paths", async () => {
  const marker = { composition_namespace: "1".repeat(32), project_id: "child", snapshot_id: "jat-old" };
  const options = { component_selection: { format_version: 1, components: {
    rcc_environment: "omit", homebrew_recovery: "refresh", hauler_content: "omit",
  } }, homebrew_directory: "/synthetic/recipe" };
  const vscode = { window: { showWarningMessage: async (_, settings, action) => {
    assert.match(settings.detail, /Homebrew recovery \(capture\)/); return action;
  }, showInformationMessage: async () => {} } };
  let pending;
  const callbacks = {
    kind: "child", marker, remember: async value => { pending = value; }, chooseComponents: async () => options,
    plan: async ticket => {
      assert.deepEqual(ticket.component_options, options);
      return { approval_sha256: "receipt", changed: true, snapshot_id: "new", request: { plan: {
        capture_mode: "full-repository", component_plan: { selection: options.component_selection },
      } } };
    },
  };
  await assert.rejects(updateRoom(vscode, { ...callbacks, save: async () => { throw Error("interrupted"); } }), /interrupted/);
  assert.equal(pending.customComponents, true);
  assert.ok(!JSON.stringify(pending).includes(options.homebrew_directory));
  await updateRoom(vscode, { ...callbacks, pending, save: async ticket => {
    assert.deepEqual(ticket.component_options, options);
    return { ok: true, marker_verified: true, snapshot_id: "new", components: { homebrew_recovery: {} } };
  } });
  assert.equal(pending, undefined);
});

for (const [field, value] of Object.entries({
  provider: "s3", dimension_id: "other-dimension", connection_id: "other-connection",
  physical_bucket: "minio:https://storage.example.invalid:other-bucket",
  endpoint: "minio:https://other.example.invalid:fixture", encryption_domain_id: "00000000-0000-4000-8000-000000000002",
})) test(`Catalog refuses changed ${field} before approval`, async () => {
  const dialogs = [[{ fsPath: "/synthetic/root" }], [{ fsPath: "/synthetic/root/a" }]];
  const dimension = { id: binding.dimension_id, provider: binding.provider, connection_id: binding.connection_id,
    encryption_domain_id: binding.encryption_domain_id, endpoint: "https://storage.example.invalid", bucket: "fixture" };
  const vscode = { window: { showOpenDialog: async () => dialogs.shift(), showQuickPick: async rows => rows[0],
    showWarningMessage: () => assert.fail("unexpected approval") } };
  await assert.rejects(saveCatalog(vscode, { selectChildren: async () => ["a"], dimensions: async () => [dimension],
    plan: async () => ({ request: { destination_binding: { ...binding, [field === "endpoint" ? "physical_bucket" : field]: value },
      child_evidence: { a: { capture_mode: "full-repository" } } } }),
    save: () => assert.fail("unexpected save") }), /destination.*changed/i);
});

test("32 folders use default capture settings with a single Continue", async () => {
  const children = Array.from({ length: 32 }, (_, index) => `child-${index}`);
  const titles = [];
  const vscode = { window: { showOpenDialog: async () => [{ fsPath: "/synthetic/root" }],
    showQuickPick: async (rows, options) => { titles.push(options.title); return rows[0]; },
    showWarningMessage: async (_message, options, action) => {
      assert.match(options.detail, /32 folders/);
      assert.doesNotMatch(options.detail, /namespace|snapshot|digest|[a-f0-9]{32}/i);
      return action;
    }, showInformationMessage: async () => {} } };
  await saveCatalog(vscode, { selectChildren: async () => children,
    chooseComponents: () => assert.fail("unexpected component prompt"),
    dimensions: async () => [{ id: "dimension", provider: "minio" }],
    plan: async selection => {
      assert.deepEqual(selection.assignments.slice(1), children.map(path => ({ path, outcome: "child" })));
      return { approval_sha256: "digest", request: { destination_binding: binding,
        child_evidence: Object.fromEntries(children.map(path => [path, { capture_mode: "full-repository" }])) } };
    }, save: async () => ({ ok: true }) });
  assert.equal(titles.length, 2);
});

test("bulk component choices apply once to a group and can be cleared on one child", async () => {
  const children = ["a", "b", "c"];
  const options = { component_selection: { format_version: 1, components: {
    rcc_environment: "refresh", hauler_content: "omit", homebrew_recovery: "omit",
  } } };
  const actions = ["components", "all", "components", "first", "continue", "destination"];
  const scopes = [];
  const vscode = { window: { showOpenDialog: async () => [{ fsPath: "/synthetic" }], showQuickPick: async rows => {
    const next = actions.shift();
    if (next === "all") return rows.filter(row => row.picked);
    if (next === "first") return rows.slice(0, 1);
    return next === "destination" ? rows[0] : rows.find(row => row.id === next);
  }, showWarningMessage: async () => undefined } };
  const result = await saveCatalog(vscode, { dimensions: async () => [{ id: binding.dimension_id, provider: binding.provider }],
    selectChildren: async () => children, chooseComponents: async paths => {
      scopes.push(paths);
      return paths.length === 1 ? null : options;
    },
    plan: async ({ assignments }) => {
      const evidence = {};
      for (const row of assignments.filter(row => row.outcome === "child")) {
        assert.deepEqual(row.component_options, row.path === "a" ? undefined : options);
        evidence[row.path] = { capture_mode: "full-repository",
          component_plan: row.component_options && { selection: options.component_selection } };
      }
      return { request: { destination_binding: binding, child_evidence: evidence } };
    }, save: () => assert.fail("cancelled review") });
  assert.equal(result, "cancelled");
  assert.deepEqual(scopes, [["/synthetic/a", "/synthetic/b", "/synthetic/c"], ["/synthetic/a"]]);
});
