"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { RoomOperations } = require("./room-operations");

function fixture(t) {
  const rendered = [];
  const requests = [];
  const status = [];
  const state = {
    writes: 0,
    select: "",
    phase: undefined,
  };
  const vscode = {
    CancellationTokenSource: class {
      token = { isCancellationRequested: false };
      cancel() { this.token.isCancellationRequested = true; }
      dispose() {}
    },
    window: {
      withProgress(options, callback) {
        rendered.push(options);
        return callback({ report() {} }, { isCancellationRequested: false });
      },
      async showQuickPick(items, options) {
        requests.push({ items, options });
        return items.find(item => item.label.includes(state.select));
      },
    },
  };
  const operations = new RoomOperations(vscode, row => status.push(row?.title), () => state.writes++);
  t.after(() => operations.dispose());
  return { operations, rendered, requests, state, status };
}

test("Rooms shows exact active, queued, and recently restored child rows", async t => {
  const { operations, rendered, requests, state, status } = fixture(t);
  const target = { dimension: "synthetic", namespace: "same", room: "parent", snapshot: "point" };
  const children = ["first", "second", "third"];
  const child = path => ({ dimension: target.dimension, namespace: target.namespace, room: path, path });
  let progress;
  let finish;
  const running = operations.run({ title: "Restoring Synthetic Catalog…", cancellable: true,
    operation: { verb: "restore", target, children } },
  reporter => { progress = reporter; return new Promise(resolve => { finish = resolve; }); });

  assert.deepEqual(rendered[0].location, { viewId: "joshRoom.rooms" });
  assert.equal(status.at(-1), "Restoring Synthetic Catalog");
  assert.deepEqual(operations.decoration(target), { icon: "loading~spin", description: "Restoring…" });
  assert.equal(operations.decoration(child("first")).description, "Queued");
  assert.equal(operations.decoration({ ...target, snapshot: "old-point" }), undefined);
  assert.equal(operations.decoration({ ...child("first"), namespace: "other" }), undefined);

  progress.report({ message: "Restoring child Rooms", detail: "1 of 3 Rooms", index: 1 });
  assert.deepEqual(operations.decoration(child("first")), { icon: "check", description: "Restored" });
  assert.equal(operations.decoration(child("second")).description, "Restoring child Rooms");
  assert.equal(operations.decoration(child("third")).description, "Queued");
  assert.equal(operations.decoration(child("unscheduled")), undefined);

  state.select = "Show Output Log";
  await operations.showMenu({ showLog: () => { state.openedLog = true; } });
  assert.equal(state.openedLog, true);
  assert.equal(requests[0].options.title, "Restoring Synthetic Catalog");
  assert.match(requests[0].options.placeHolder, /1 of 3 Rooms/);
  assert.ok(requests[0].items.some(item => /Cancel operation/.test(item.label)));

  finish({ ok: true });
  await running;
  assert.equal(status.at(-1), undefined);
  assert.equal(operations.decoration(target).description, "Restored");
  operations.last.finished -= 5000;
  operations.tick();
  assert.equal(operations.decoration(target), undefined);
  assert.ok(state.writes > 1);
});

test("Operation menu cancels only active work and retains verified Save receipts across storage refresh", async t => {
  const { operations, requests, state } = fixture(t);
  const target = { dimension: "synthetic", namespace: "same", room: "parent" };
  const active = operations.run({ title: "Restoring Synthetic Catalog", cancellable: true,
    operation: { verb: "restore", target } }, (_progress, token) =>
    new Promise(resolve => { state.finish = () => resolve({ cancelled: token.isCancellationRequested }); }));
  state.select = "Cancel operation";
  await operations.showMenu({});
  state.finish();
  assert.deepEqual(await active, { cancelled: true });
  assert.equal(operations.last.state, "cancelled");

  await operations.run({ title: "Saving Synthetic Catalog", operation: { verb: "save", target } },
    async () => ({ ok: true }));
  const receipt = { dimension_id: "synthetic", namespace: "same", room_id: "parent", snapshot_id: "point" };
  assert.equal(operations.noteOutcome({ target: { ...target, room: "other" }, receipt }), undefined);
  operations.noteOutcome({ target, receipt, folder: "/synthetic/restored" });
  await operations.run({ title: "Loading storage", operation: { quiet: true } }, async () => ({ ok: true }));

  state.select = "Copy Recovery Receipt";
  await operations.showMenu({ copyReceipt: value => { state.copied = value; } });
  assert.equal(requests.at(-1).options.title, "Saving Synthetic Catalog");
  assert.deepEqual(state.copied, receipt);
  state.select = "Open Restored Folder";
  await operations.showMenu({ openFolder: folder => { state.folder = folder; } });
  assert.equal(state.folder, "/synthetic/restored");
  assert.ok(!requests.at(-1).items.some(item => /Cancel operation/.test(item.label)));
});
