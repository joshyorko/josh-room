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
