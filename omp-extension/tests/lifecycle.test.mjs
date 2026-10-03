import test from "node:test";
import assert from "node:assert/strict";
import { registerRoomLifecycle, readProvenance } from "../src/lifecycle.ts";

test("outside-Room session startup is silent and only probes cheap context", async () => {
	const handlers = new Map();
	const statusCalls = [];
	let probeCalls = 0;
	const pi = { on(name, handler) { handlers.set(name, handler); } };
	const controller = { async context() { probeCalls += 1; return { kind: "unlinked" }; } };
	registerRoomLifecycle(pi, controller);
	const context = {
		cwd: "/workspace",
		sessionManager: { getBranch: () => [] },
		ui: { setStatus(...args) { statusCalls.push(args); } },
	};
	await handlers.get("session_start")({}, context);
	assert.equal(probeCalls, 1);
	assert.deepEqual(statusCalls, []);
});

test("stale custom provenance is parsed only as convenience state", () => {
	const valid = { type: "custom", customType: "com.joshyorko.josh-room.checkpoint.v1", data: { format_version: 1, dimension_id: "local", project_id: "demo", previous_snapshot_id: "jat-old", snapshot_id: "jat-new" } };
	const ctx = { sessionManager: { getBranch: () => [valid] } };
	assert.deepEqual(readProvenance(ctx), valid.data);
	assert.equal(readProvenance({ sessionManager: { getBranch: () => [{ ...valid, data: { ...valid.data, snapshot_id: "../../bad" } }] } }), undefined);
});
