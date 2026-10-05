import test from "node:test";
import assert from "node:assert/strict";
import { parseRoomCommand, registerRoomCommand } from "../src/commands.ts";

test("direct Room command routing is bounded and completion exposes supported actions", () => {
	assert.deepEqual(parseRoomCommand("status"), { kind: "action", action: "status", rest: [] });
	assert.deepEqual(parseRoomCommand("snapshots"), { kind: "action", action: "snapshots", rest: [] });
	assert.deepEqual(parseRoomCommand(""), { kind: "menu" });
	assert.equal(parseRoomCommand("remove something"), undefined);
});

test("cancelling the native action selector performs no controller call or notification", async () => {
	let handler;
	let status = 0;
	let notifications = 0;
	const api = {
		registerCommand(name, definition) { if (name === "room") handler = definition.handler; },
		appendEntry() {},
	};
	const controller = new Proxy({}, { get() { return async () => { status += 1; throw new Error("must remain unused"); }; } });
	registerRoomCommand(api, controller);
	await handler("", {
		cwd: "/workspace", hasUI: true,
		ui: {
			async select() { return undefined; },
			async confirm() { return false; },
			notify() { notifications += 1; },
			setStatus() {}, setWorkingMessage() {},
		},
	});
	assert.equal(status, 0);
	assert.equal(notifications, 0);
});

test("explicit no-op Save reports Already saved without appending new checkpoint provenance", async () => {
	let handler;
	const appended = [];
	const notifications = [];
	const statuses = [];
	const api = {
		registerCommand(name, definition) { if (name === "room") handler = definition.handler; },
		appendEntry(...args) { appended.push(args); },
	};
	const context = { kind: "linked", dimensionId: "local", projectId: "demo-room", displayName: "Demo Room", snapshotId: "logical-current" };
	const controller = { async save() { return { context, receipt: { kind: "already-saved", status: "already-saved", projectId: "demo-room", snapshotId: "logical-current", dataAddedBytes: 0 } }; } };
	registerRoomCommand(api, controller);
	await handler("save", { cwd: "/workspace", hasUI: true, ui: { async select() {}, async confirm() { return true; }, notify(...args) { notifications.push(args); }, setStatus(...args) { statuses.push(args); }, setWorkingMessage() {} } });
	assert.equal(appended.length, 0);
	assert.match(notifications[0][0], /^Already saved/);
	assert.match(notifications[0][0], /0 bytes added/);
	assert.deepEqual(statuses, [["josh-room", "room:Demo Room ✓"]]);
});

test("explicit saved-but-dirty Save appends its checkpoint and does not show clean status", async () => {
	let handler;
	const appended = [];
	const notifications = [];
	const statuses = [];
	const api = {
		registerCommand(name, definition) { if (name === "room") handler = definition.handler; },
		appendEntry(...args) { appended.push(args); },
	};
	const context = { kind: "linked", dimensionId: "local", projectId: "demo-room", displayName: "Demo Room", snapshotId: "logical-old" };
	const controller = { async save() { return { context, receipt: { kind: "saved", status: "saved-but-dirty", projectId: "demo-room", snapshotId: "logical-new", previousSnapshotId: "logical-old", dataAddedBytes: 7 } }; } };
	registerRoomCommand(api, controller);
	await handler("save", { cwd: "/workspace", hasUI: true, ui: { async select() {}, async confirm() { return true; }, notify(...args) { notifications.push(args); }, setStatus(...args) { statuses.push(args); }, setWorkingMessage() {} } });
	assert.equal(appended.length, 1);
	assert.match(notifications[0][0], /workspace changed during Save/);
	assert.deepEqual(statuses, [["josh-room", "room:Demo Room ●"]]);
});
