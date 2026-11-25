"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { saveCatalog, enterChild, hydrateChildren, relativeSource, updateRoom } = require("./catalog-actions");

const binding = { provider: "minio", dimension_id: "dimension", connection_id: "synthetic-connection",
  encryption_domain_id: "00000000-0000-4000-8000-000000000001", physical_bucket: "minio:https://storage.example.invalid:fixture" };

function installReviewPicker(vscode, interact) {
  vscode.window.showInputBox ??= async options => options.value;
  vscode.QuickPickItemKind = { Separator: -1 };
  vscode.window.createQuickPick = () => {
    let accepted, hidden, disposed = false;
    const picker = {
      show() {},
      onDidAccept(callback) { accepted = callback; },
      onDidHide(callback) { hidden = callback; },
      hide() { hidden(); },
      dispose() { disposed = true; },
      set activeItems(items) {
        this.activeItem = items[0];
        interact(this, item => {
          this.selectedItems = [item];
          accepted();
        });
        assert.equal(disposed, true);
      },
    };
    return picker;
  };
}

function action(picker, label) {
  return picker.items.find(item => item.action === label);
}

test("catalog confirmation assigns only checked child folders and explains root-file ownership", async () => {
  const dialogs = [[{ fsPath: "/synthetic/root" }]];
  const picks = [{ dimension: { id: "dimension", provider: "minio", display_name: "Storage", endpoint: "https://STORAGE.example.invalid:443/", bucket: "fixture", connection_id: "synthetic-connection" } }];
  let approved = false;
  let receipt;
  const vscode = { env: { clipboard: { writeText: async text => { receipt = JSON.parse(text); } } }, Uri: { file: fsPath => ({ fsPath }) }, window: {
    showOpenDialog: async () => dialogs.shift(), showQuickPick: async () => picks.shift(),
    showInformationMessage: async (_, action) => { assert.equal(action, "Copy recovery receipt"); return action; },
  } };
  vscode.window.showInputBox = async options => {
    assert.equal(options.value, "root");
    assert.ok(options.validateInput("  "));
    return "  Synthetic Catalog  ";
  };
  installReviewPicker(vscode, (picker, accept) => {
    assert.match(picker.title, /Synthetic Catalog → Storage · 1 child Room/);
    assert.deepEqual(picker.items.filter(item => item.kind === -1).map(item => item.label),
      ["Destination", "Capture", "Actions", "Child Rooms"]);
    const destination = picker.items.find(item => item.label === "Storage");
    assert.equal(destination.description, "MinIO");
    assert.equal(destination.detail, "https://storage.example.invalid:fixture");
    assert.equal(picker.items.find(item => item.label === "Connection").description, "synthetic-connection");
    assert.equal(picker.items.find(item => item.label === "Dimension").description, "dimension");
    assert.match(picker.activeItem.label, /Catalog parent/);
    assert.match(picker.activeItem.detail, /Unchecked folders stay local/);
    const child = picker.items.find(item => item.label === "$(folder) a");
    assert.equal(child.description, "Files + Git if present  ·  Workspace only");
    accept(child);
    approved = true;
    accept(action(picker, "Save catalog"));
  });
  const result = await saveCatalog(vscode, {
    selectChildren: async root => { assert.equal(root, "/synthetic/root"); return ["a"]; }, dimensions: async () => [{ id: "dimension", provider: "minio", encryption_domain_id: "00000000-0000-4000-8000-000000000001" }],
    plan: async (selection) => { assert.equal(approved, false); assert.equal(selection.name, "Synthetic Catalog"); assert.deepEqual(selection.assignments, [{ path: "a", outcome: "child" }]); return { request: { destination_binding: binding, child_evidence: { a: { capture_mode: "full-repository" } } }, approval_sha256: "approved" }; },
    save: async (selection, approval) => { assert.equal(approved, true); assert.equal(approval, "approved"); return { ok: true, namespace: selection.namespace, parent_room_id: selection.parentRoom, parent_snapshot_id: "exact-pin" }; },
  });
  assert.equal(result.ok, true);
  assert.deepEqual(receipt, { dimension_id: "dimension", namespace: result.namespace, parent_room_id: result.parent_room_id, parent_snapshot_id: "exact-pin" });
});

test("Advanced settings return to a fresh plan and a second confirmation", async () => {
  const source = "/synthetic/root";
  const options = { component_selection: { format_version: 1, components: {
    rcc_environment: "omit", homebrew_recovery: "omit", hauler_content: "refresh",
  } } };
  let review = 0;
  let plans = 0;
  let settingsMenus = 0;
  const vscode = { window: {
    showOpenDialog: async () => [{ fsPath: source }],
    showQuickPick: async (rows, settings) => {
      if (settings.title === "Where should this catalog be saved?") return { dimension: { id: "dimension", provider: "minio" } };
      if (settings.title === "Catalog child settings") return { id: settingsMenus++ === 0 ? "components" : "review" };
      if (settings.title === "Apply settings to folders") return [rows[0]];
      return assert.fail(`Unexpected picker: ${settings.title}`);
    },
    showInformationMessage: async () => undefined,
  } };
  installReviewPicker(vscode, (picker, accept) => {
    const child = picker.items.find(item => item.label === "$(folder) a");
    if (++review === 1) assert.match(child.description, /Workspace only/);
    else assert.equal(child.detail, "OCI / Hauler content (capture)");
    accept(action(picker, review === 1 ? "Advanced settings" : "Save catalog"));
  });
  const result = await saveCatalog(vscode, {
    selectChildren: async () => ["a"],
    dimensions: async () => [],
    chooseComponents: async sources => { assert.deepEqual(sources, [source + "/a"]); return options; },
    plan: async selection => {
      plans += 1;
      if (plans === 1) assert.deepEqual(selection.assignments, [{ path: "a", outcome: "child" }]);
      else assert.deepEqual(selection.assignments, [{ path: "a", outcome: "child", component_options: options }]);
      return { request: { destination_binding: binding, child_evidence: { a: { capture_mode: "full-repository",
        ...(plans > 1 ? { component_plan: { selection: options.component_selection } } : {}) } } }, approval_sha256: `approval-${plans}` };
    },
    save: async (_, approval) => ({ ok: true, approval }),
  });
  assert.equal(result.approval, "approval-2");
  assert.equal(plans, 2);
  assert.equal(review, 2);
});

test("cancelling catalog review never invokes Save", async () => {
  const vscode = { window: {
    showOpenDialog: async () => [{ fsPath: "/synthetic/root" }],
    showQuickPick: async (rows) => rows[0],
  } };
  installReviewPicker(vscode, picker => picker.hide());
  const result = await saveCatalog(vscode, { selectChildren: async () => ["a"], dimensions: async () => [{ id: "dimension", provider: "minio", encryption_domain_id: "00000000-0000-4000-8000-000000000001" }],
    plan: async () => ({ request: { destination_binding: binding, child_evidence: { a: { capture_mode: "source-only" } } }, approval_sha256: "digest" }), save: async () => { throw new Error("unapproved Save"); } });
  assert.equal(result, "cancelled");
});

test("Catalog previews explicit files-only and local-only choices before saving", async () => {
  const refusals = [
    ["shallow", "composition-child-git-files-only", "Review files only"],
    ["reserved", "composition-child-room-metadata", "Leave folder local"],
  ];
  let plans = 0;
  const vscode = { window: {
    showOpenDialog: async () => [{ fsPath: "/synthetic/root" }],
    showQuickPick: async rows => rows[0],
    showInformationMessage: async message => assert.match(message, /2 child Rooms/),
    showWarningMessage: async (folder, options, button) => {
      const [path, , expectedButton] = refusals[plans - 1];
      assert.equal(folder, path);
      assert.equal(options.modal, true);
      assert.equal(button, expectedButton);
      return button;
    },
  } };
  installReviewPicker(vscode, (picker, accept) => {
    assert.match(picker.title, /2 child Rooms/);
    assert.match(picker.items.find(item => item.label === "$(folder) shallow").description, /Files only/);
    assert.ok(!picker.items.some(item => item.label === "$(folder) reserved"));
    accept(action(picker, "Save catalog"));
  });
  const desired = [{ path: "shallow", outcome: "child", capture_mode: "source-only" }, { path: "healthy", outcome: "child" }];
  await saveCatalog(vscode, {
    selectChildren: async () => ["shallow", "reserved", "healthy"],
    dimensions: async () => [{ id: "dimension", provider: "minio" }],
    plan: async selection => {
      const refusal = refusals[plans++];
      if (refusal) {
        const [path, code] = refusal;
        throw Object.assign(new Error("Synthetic refusal"), { result: {
          error_code: code, child_path_sha256: crypto.createHash("sha256").update(path).digest("hex"),
        } });
      }
      assert.deepEqual(selection.assignments, desired);
      return { approval_sha256: "fresh", request: { destination_binding: binding, child_evidence: {
        shallow: { capture_mode: "source-only" }, healthy: { capture_mode: "full-repository" },
      } } };
    },
    save: async (selection, digest) => {
      assert.deepEqual(selection.assignments, desired);
      assert.equal(digest, "fresh");
      return { ok: true };
    },
  });
  assert.equal(plans, 3);
});

const parentMarker = { composition_namespace: "1".repeat(32), dimension_id: "dimension", project_id: "parent", snapshot_id: "parent-pin" };
const children = { ok: true, namespace: parentMarker.composition_namespace, parent_room_id: "parent", parent_snapshot_id: "parent-pin",
  current_parent: true, revision: 1, next_offset: null, children: [{ path: "projects/a", room_id: "a", snapshot_id: "pinned-a",
    other_point_count: 1 }] };
const childHistory = { ...children, child_path: "projects/a", child_room_id: "a", pinned_snapshot_id: "pinned-a",
  recovery_points: [{ snapshot_id: "jat-child-new", created_at: "new" }] };

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
  await updateRoom(vscode, { marker, kind: "parent", listChildren: async (_, page) => page.childPath ? childHistory : children, remember: async () => {},
    plan: async (ticket) => { assert.equal(ticket.pin, "jat-child-new"); return { changed: true, approval_sha256: "digest", snapshot_id: "jat-parent-new" }; },
    save: async (ticket) => { assert.equal(ticket.child, "projects/a"); return { ok: true, snapshot_id: "jat-parent-new" }; } });
});

test("child-specific mode and component inputs reach both plan and approved capture", async () => {
  const source = "/synthetic/work";
  const children = ["projects/a", "projects/b"];
  const components = { rcc_environment: "omit", homebrew_recovery: "refresh", hauler_content: "omit" };
  const options = { component_selection: { format_version: 1, components }, homebrew_directory: "/synthetic/recipe" };
  let settingsMenu = 0;
  let folderScope = 0;
  let review = 0;
  const vscode = { window: {
    showOpenDialog: async () => [{ fsPath: source }], showQuickPick: async (rows, picker) => {
      if (picker.title === "Where should this catalog be saved?") return { dimension: { id: "test", provider: "minio", encryption_domain_id: "00000000-0000-4000-8000-000000000001" } };
      if (picker.title === "Catalog child settings") return [{ id: "git" }, { id: "components" }, { id: "review" }][settingsMenu++];
      if (picker.title === "Apply settings to folders") return [[rows[1]], [rows[0]]][folderScope++];
      if (picker.title === "Git capture for 1 folders") return { id: "source-only" };
      return assert.fail(`Unexpected picker: ${picker.title}`);
    },
    showInformationMessage: async () => {},
  } };
  let approved;
  let planCount = 0;
  installReviewPicker(vscode, (picker, accept) => {
    if (++review === 1) return accept(action(picker, "Advanced settings"));
    const childA = picker.items.find(item => item.label === "$(folder) projects/a");
    const childB = picker.items.find(item => item.label === "$(folder) projects/b");
    assert.equal(childA.description, "Files + Git if present");
    assert.equal(childA.detail, "Homebrew recovery (capture)");
    assert.equal(childB.description, "Files only  ·  Workspace only");
    accept(action(picker, "Save catalog"));
  });
  await saveCatalog(vscode, {
    selectChildren: async () => children, dimensions: async () => [], chooseComponents: async sources => {
      assert.deepEqual(sources, [source + "/" + children[0]]);
      return options;
    },
    plan: async selection => {
      approved = selection;
      planCount += 1;
      if (planCount === 1) {
        assert.deepEqual(selection.assignments, children.map(path => ({ path, outcome: "child" })));
        return { approval_sha256: "initial", request: { destination_binding: { ...binding, dimension_id: "test" }, child_evidence: {
          [children[0]]: { capture_mode: "full-repository" },
          [children[1]]: { capture_mode: "full-repository" },
        } } };
      }
      assert.deepEqual(selection.assignments, [
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
    createQuickPick: () => assert.fail("unexpected approval") } };
  vscode.window.showInputBox = async options => options.value;
  await assert.rejects(saveCatalog(vscode, { selectChildren: async () => ["a"], dimensions: async () => [dimension],
    plan: async () => ({ request: { destination_binding: { ...binding, [field === "endpoint" ? "physical_bucket" : field]: value },
      child_evidence: { a: { capture_mode: "full-repository" } } } }),
    save: () => assert.fail("unexpected save") }), /destination.*changed/i);
});

test("32 checked folders remain valid child assignments", async () => {
  const children = Array.from({ length: 32 }, (_, index) => `child-${index}`);
  let pickerCount = 0;
  const vscode = { window: { showOpenDialog: async () => [{ fsPath: "/synthetic/root" }],
    showQuickPick: async rows => { pickerCount += 1; return rows[0]; },
    showInformationMessage: async () => {} } };
  installReviewPicker(vscode, (picker, accept) => {
    assert.match(picker.title, /32 child Rooms/);
    assert.deepEqual(picker.items.filter(item => item.label.startsWith("$(folder) ")).map(item => item.label),
      children.map(child => `$(folder) ${child}`));
    accept(action(picker, "Save catalog"));
  });
  await saveCatalog(vscode, { selectChildren: async () => children,
    chooseComponents: () => assert.fail("unexpected component prompt"),
    dimensions: async () => [{ id: "dimension", provider: "minio" }],
    plan: async selection => {
      assert.deepEqual(selection.assignments, children.map(path => ({ path, outcome: "child" })));
      return { approval_sha256: "digest", request: { destination_binding: binding,
        child_evidence: Object.fromEntries(children.map(path => [path, { capture_mode: "full-repository" }])) } };
    }, save: async () => ({ ok: true }) });
  assert.equal(pickerCount, 1);
});

test("empty checked-folder selection cancels before destination or capture planning", async () => {
  const vscode = { window: { showOpenDialog: async () => [{ fsPath: "/synthetic/root" }],
    showQuickPick: async () => assert.fail("no destination needed without children") } };
  const result = await saveCatalog(vscode, { selectChildren: async () => [],
    dimensions: assert.fail, plan: assert.fail, save: assert.fail });
  assert.equal(result, "cancelled");
});

test("refresh pages all saved child paths and allows choosing a later history page", async () => {
  const reads = [];
  const listChildren = async (_, query) => {
    reads.push(query);
    if (!query.childPath) {
      if (!query.offset) return { ...children, children: [], next_offset: 1 };
      return children;
    }
    if (!query.offset) return { ...childHistory, next_offset: 64 };
    return { ...childHistory, recovery_points: [{ snapshot_id: "jat-older", created_at: "earlier" }] };
  };
  let pick = 0;
  const vscode = { window: { showQuickPick: async items => ++pick === 2 ? items.find(row => row.more) : items[0],
    showWarningMessage: async (_title, _options, action) => action, showInformationMessage: async () => {} } };
  await updateRoom(vscode, { marker: parentMarker, kind: "parent", listChildren,
    remember: async () => {}, plan: async ticket => {
      assert.equal(ticket.pin, "jat-older");
      return { approval_sha256: "approved", snapshot_id: "new-parent", changed: true };
    }, save: async () => ({ ok: true, snapshot_id: "new-parent" }) });
  assert.deepEqual(reads.map(page => [page.offset, page.revision]), [[0, undefined], [1, 1], [0, 1], [64, 1]]);
});

test("Catalog hydration restores only the explicitly checked children at their pins", async () => {
  const marker = { composition_namespace: "1".repeat(32), dimension_id: "synthetic",
    project_id: "parent", snapshot_id: "jat-parent" };
  const children = ["a", "b", "c"].map(path => ({ path, room_id: path, snapshot_id: `jat-${path}` }));
  const entered = [];
  const callbacks = { root: "/synthetic/root", marker,
    listChildren: async () => ({ ok: true, namespace: marker.composition_namespace, revision: 1,
      parent_room_id: "parent", parent_snapshot_id: "jat-parent", children, next_offset: null }),
    enter: async ({ child }) => {
      entered.push(child);
      return { ok: true, marker_verified: true, namespace: marker.composition_namespace,
        room_id: child, snapshot_id: `jat-${child}` };
    },
  };
  const vscode = { window: {
    showQuickPick: async (rows, options) => {
      assert.equal(options.canPickMany, true);
      assert.deepEqual(rows.map(row => row.label), ["a", "b", "c"]);
      return [rows[0], rows[2]];
    },
    showInformationMessage: async () => {},
  } };
  const result = await hydrateChildren(vscode, callbacks);
  assert.deepEqual(entered, ["a", "c"]);
  assert.deepEqual(result.restored.map(row => row.snapshot_id), ["jat-a", "jat-c"]);
  vscode.window.showQuickPick = async rows => rows;
  const all = await hydrateChildren(vscode, callbacks);
  assert.deepEqual(all.restored.map(row => row.snapshot_id), ["jat-a", "jat-b", "jat-c"]);
  vscode.window.showQuickPick = async () => undefined;
  assert.equal(await hydrateChildren(vscode, { ...callbacks, enter: assert.fail }), "cancelled");
  const localState = row => row.path === "a" ? "restored" : row.path === "b" ? "occupied" : "missing";
  vscode.window.showQuickPick = async rows => {
    assert.deepEqual(rows.map(row => row.label), ["c"]);
    return rows;
  };
  assert.equal((await hydrateChildren(vscode, { ...callbacks, localState })).restored[0].snapshot_id, "jat-c");
  await assert.rejects(hydrateChildren(vscode, { ...callbacks, localState, childPaths: ["c", "b"], enter: assert.fail }),
    /selected child is unavailable/);
  let opened;
  assert.equal(await enterChild(vscode, { ...callbacks, childPath: "a", localState, enter: assert.fail,
    open: async target => { opened = target; } }), "opened");
  assert.equal(opened, require("node:path").resolve(callbacks.root, "a"));
});
