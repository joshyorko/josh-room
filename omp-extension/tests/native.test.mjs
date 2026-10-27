import test from "node:test";
import assert from "node:assert/strict";
import { jatArgumentCompletions, registerRoomCommand, roomArgumentCompletions } from "../src/commands.ts";
import { createRoomController } from "../src/controller.ts";
import { registerRoomLifecycle } from "../src/lifecycle.ts";
import { RoomCliError } from "../src/process.ts";
import { PROVENANCE_TYPE, RoomSessionState } from "../src/state.ts";
import { registerRoomTools, renderRoomResult } from "../src/tools.ts";

const linked = { kind: "linked", dimensionId: "local", projectId: "demo-room", displayName: "Demo Room", snapshotId: "jat-old" };
const theme = { fg: (color, text) => `<${color}>${text}</${color}>`, bold: (text) => `*${text}*` };

class FakeComponent {
	constructor(...args) { this.args = args; this.children = []; }
	addChild(child) { this.children.push(child); }
	render() { return [String(this.args[0] ?? "")]; }
	invalidate() {}
}
class FakeSelectList extends FakeComponent {
	constructor(items, maxVisible, listTheme) { super(items, maxVisible, listTheme); this.items = items; }
	handleInput(data) {
		if (data === "\r") this.onSelect?.(this.items[this.index ?? 0]);
		else if (data === "\u001b") this.onCancel?.();
		else if (data === "j") this.index = (this.index ?? 0) + 1;
	}
}
class FakeLoader extends FakeComponent {
	#controller = new AbortController();
	get signal() { return this.#controller.signal; }
	handleInput(data) { if (data === "\u001b") this.#controller.abort(); }
}
const kit = {
	Container: FakeComponent, Text: FakeComponent, SelectList: FakeSelectList, DynamicBorder: FakeComponent, BorderedLoader: FakeLoader,
	getSelectListTheme: () => ({}), truncateToWidth: (text, width) => text.slice(0, width),
};

function harness({ mode = "tui", custom, select, confirm = async () => true } = {}) {
	const commands = new Map();
	const tools = new Map();
	const appended = [];
	const notifications = [];
	const statuses = [];
	const working = [];
	const api = {
		registerCommand(name, definition) { commands.set(name, definition); },
		registerTool(definition) { tools.set(definition.name, definition); },
		appendEntry(...args) { appended.push(args); },
		setActiveTools() { throw new Error("must not mutate the active tool set"); },
		zod: fakeZod,
	};
	const ctx = {
		cwd: "/workspace", mode, hasUI: mode !== "print",
		ui: {
			theme,
			select: select ?? (async () => undefined),
			confirm,
			notify(...args) { notifications.push(args); },
			setStatus(...args) { statuses.push(args); },
			setWorkingMessage(...args) { working.push(args); },
			...(custom ? { custom } : {}),
		},
	};
	return { api, commands, tools, appended, notifications, statuses, working, ctx };
}

const fakeZod = {
	object(shape) { return { kind: "object", shape, strict() { return { ...this, strictMode: true }; } }; },
	string() { const schema = { kind: "string", min(n) { return { ...schema, minLength: n, max(m) { return { ...schema, minLength: n, maxLength: m }; } }; } }; return schema; },
};

/** Drives an OMP `ctx.ui.custom` factory with scripted key input. */
function scriptedCustom(keys) {
	const mounted = [];
	return {
		mounted,
		custom: (factory) => new Promise((resolve) => {
			const component = factory({}, theme, { matches: () => false }, resolve);
			mounted.push(component);
			for (const key of keys) component.handleInput?.(key);
		}),
	};
}

test("completions return OMP autocomplete objects for /room and /jat", () => {
	const items = roomArgumentCompletions("s");
	assert.deepEqual(items.map((item) => item.value), ["status", "snapshots", "save"]);
	for (const item of items) assert.equal(typeof item.label, "string");
	assert.equal(roomArgumentCompletions("save note"), null);
	assert.equal(roomArgumentCompletions("zzz"), null);
	assert.deepEqual(jatArgumentCompletions("in"), [{ value: "inspect ", label: "inspect", description: "Inspect an explicit local JAT haul" }]);
	assert.equal(jatArgumentCompletions("inspect /tmp/haul.tar.zst"), null);
	const h = harness();
	registerRoomCommand(h.api, {}, new RoomSessionState(), kit);
	assert.deepEqual(h.commands.get("room").getArgumentCompletions("doc"), [{ value: "doctor", label: "doctor", description: "Diagnose the local Josh Room runtime" }]);
	assert.equal(h.commands.get("jat").getArgumentCompletions("")[0].value, "inspect ");
});

test("/room opens a native SelectList hub in the TUI and routes the chosen action", async () => {
	const ui = scriptedCustom(["j", "\r"]);
	const h = harness({ custom: ui.custom });
	let snapshotsCalled = 0;
	const controller = { async snapshots() { snapshotsCalled += 1; return { context: linked, snapshots: { kind: "snapshots", projectId: "demo-room", latestSnapshotId: "jat-old", snapshots: [] } }; } };
	registerRoomCommand(h.api, controller, new RoomSessionState(), kit);
	await h.commands.get("room").handler("", h.ctx);
	assert.equal(snapshotsCalled, 1);
	const hub = ui.mounted[0];
	const lines = hub.render(12);
	assert.ok(lines.every((line) => line.length <= 12), "picker lines are truncated to narrow widths");
});

test("/room hub cancel performs no controller call", async () => {
	const ui = scriptedCustom(["\u001b"]);
	const h = harness({ custom: ui.custom });
	const controller = new Proxy({}, { get() { return async () => { throw new Error("must remain unused"); }; } });
	registerRoomCommand(h.api, controller, new RoomSessionState(), kit);
	await h.commands.get("room").handler("", h.ctx);
	assert.deepEqual(h.notifications, []);
});

test("RPC/ACP fall back to the host select dialog and map labels back to values", async () => {
	const seen = [];
	const h = harness({ mode: "rpc", select: async (title, options) => { seen.push(title, options); return options[1].label; } });
	h.ctx.ui.custom = () => { throw new Error("custom UI must not be used outside the TUI"); };
	const snapshots = { kind: "snapshots", projectId: "demo-room", latestSnapshotId: "jat-new", snapshots: [
		{ snapshotId: "jat-new", createdAt: "2026-02-01T00:00:00Z", ciphertextSize: 4 },
		{ snapshotId: "jat-old", createdAt: "2026-01-01T00:00:00Z", ciphertextSize: 3 },
	] };
	registerRoomCommand(h.api, { async snapshots() { return { context: linked, snapshots }; } }, new RoomSessionState(), kit);
	await h.commands.get("room").handler("snapshots", h.ctx);
	assert.match(seen[0], /Room snapshots · 2 saved/);
	assert.match(h.notifications[0][0], /^Snapshot jat-old · created 2026-01-01T00:00:00Z .* read-only$/);
});

test("RPC/ACP fallback keeps same-named Rooms distinct and returns the chosen project", async () => {
	const seen = [];
	const h = harness({ mode: "rpc", select: async (_title, options) => { seen.push(options); return options[1].label; } });
	const rooms = { kind: "rooms", rooms: [{ projectId: "app-a", displayName: "App" }, { projectId: "app-b", displayName: "App" }] };
	registerRoomCommand(h.api, { async rooms() { return { context: linked, rooms }; } }, new RoomSessionState(), kit);
	await h.commands.get("room").handler("rooms", h.ctx);
	assert.equal(new Set(seen[0].map((option) => option.label)).size, 2, "host dialog labels are unique");
	assert.match(h.notifications[0][0], /^Room App · app-b · read-only$/);
});

test("explicit Save shows native progress, updates status from the receipt, and records provenance", async () => {
	const ui = scriptedCustom([]);
	const h = harness({ custom: ui.custom });
	const state = new RoomSessionState();
	state.show(h.ctx, linked, "changed");
	let signalSeen;
	const controller = { async save(_cwd, signal) { signalSeen = signal; return { context: linked, receipt: { kind: "saved", status: "saved", projectId: "demo-room", snapshotId: "jat-new", previousSnapshotId: "jat-old", dataAddedBytes: 9 } }; } };
	registerRoomCommand(h.api, controller, state, kit);
	await h.commands.get("room").handler("save", h.ctx);
	assert.ok(ui.mounted[0] instanceof FakeLoader);
	assert.ok(signalSeen instanceof AbortSignal);
	assert.deepEqual(h.appended, [[PROVENANCE_TYPE, { format_version: 1, dimension_id: "local", project_id: "demo-room", previous_snapshot_id: "jat-old", snapshot_id: "jat-new" }]]);
	assert.equal(state.room.snapshotId, "jat-new");
	assert.equal(state.indicator, "clean");
	assert.match(h.statuses.at(-1)[1], /<success>✓<\/success>$/);
	assert.match(h.notifications[0][0], /^Saved Room Demo Room → jat-new/);
});

test("failed or cancelled Save keeps the previous snapshot authoritative", async () => {
	for (const [error, pattern] of [[new RoomCliError("failed", "workspace changed during capture"), /^Room save failed — previous snapshot remains authoritative\. workspace changed/], [new RoomCliError("cancelled"), /^Room save cancelled/]]) {
		const h = harness({ mode: "rpc" });
		const state = new RoomSessionState();
		state.show(h.ctx, linked, "changed");
		registerRoomCommand(h.api, { async save() { throw error; } }, state, kit);
		await h.commands.get("room").handler("save", h.ctx);
		assert.equal(h.appended.length, 0);
		assert.equal(state.room.snapshotId, "jat-old");
		assert.equal(state.indicator, "changed");
		assert.match(h.notifications[0][0], pattern);
		assert.deepEqual(h.working, [["Saving Room Demo Room…"], []], "working message is restored");
	}
});

const linkedContextJson = { format_version: 1, ok: true, state: "linked", linked: true, path_matches: true, dimension_id: "local", project_id: "demo-room", display_name: "Demo Room", snapshot_id: "jat-old" };

test("Save failures after snapshot create started report an unknown outcome and demand a refresh", async () => {
	for (const [kind, indicator] of [["timeout", "unknown"], ["cancelled", "unknown"], ["output-limit", "unknown"], ["invalid-result", "blocked"]]) {
		const h = harness({ mode: "rpc" });
		const state = new RoomSessionState();
		state.show(h.ctx, linked, "clean");
		const controller = createRoomController(async (args) => {
			if (args[0] === "context") return linkedContextJson;
			throw new RoomCliError(kind);
		});
		registerRoomCommand(h.api, controller, state, kit);
		await h.commands.get("room").handler("save", h.ctx);
		assert.equal(h.appended.length, 0);
		assert.equal(state.indicator, indicator, kind);
		assert.doesNotMatch(h.notifications[0][0], /previous snapshot remains authoritative/, kind);
		assert.match(h.notifications[0][0], /\/room status/, kind);
	}
	const h = harness({ mode: "rpc" });
	const state = new RoomSessionState();
	state.show(h.ctx, linked, "clean");
	registerRoomCommand(h.api, createRoomController(async (args) => {
		if (args[0] === "context") return linkedContextJson;
		throw new RoomCliError("failed", "workspace changed during capture");
	}), state, kit);
	await h.commands.get("room").handler("save", h.ctx);
	assert.equal(state.indicator, "clean", "a failure Josh Room reported leaves the indicator alone");
	assert.match(h.notifications[0][0], /previous snapshot remains authoritative/);
});

test("a provenance write failure after a real receipt is an unknown Save outcome", async () => {
	const h = harness();
	h.api.appendEntry = () => { throw new Error("session store unavailable"); };
	const state = new RoomSessionState();
	state.show(h.ctx, linked, "clean");
	const controller = { async save() { return { context: linked, receipt: { kind: "saved", status: "saved", projectId: "demo-room", snapshotId: "jat-new", previousSnapshotId: "jat-old", dataAddedBytes: 1 } }; } };
	registerRoomTools(h.api, controller, state, kit);
	const result = await h.tools.get("room_save").execute("id", {}, undefined, undefined, h.ctx);
	assert.equal(result.isError, true);
	assert.equal(result.details.save_outcome, "unknown");
	assert.equal(state.indicator, "unknown");
	assert.doesNotMatch(result.content[0].text, /previous snapshot remains authoritative/);
});

test("an action that finds no linked Room clears the stale indicator", async () => {
	const h = harness({ mode: "rpc" });
	const state = new RoomSessionState();
	state.show(h.ctx, linked, "clean");
	registerRoomCommand(h.api, { async snapshots() { throw new RoomCliError("not-linked"); } }, state, kit);
	await h.commands.get("room").handler("snapshots", h.ctx);
	assert.equal(state.room, undefined);
	assert.deepEqual(h.statuses.at(-1), ["josh-room", undefined]);
});

test("Escape on the Save loader aborts the owned operation and reports cancellation after it settles", async () => {
	const ui = scriptedCustom(["\u001b"]);
	const h = harness({ custom: ui.custom });
	const controller = { save: (_cwd, signal) => new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new RoomCliError("cancelled")))) };
	registerRoomCommand(h.api, controller, new RoomSessionState(), kit);
	const run = h.commands.get("room").handler("save", h.ctx);
	await new Promise((resolve) => setImmediate(resolve));
	ui.mounted[0].handleInput("\u001b");
	await run;
	assert.match(h.notifications[0][0], /cancelled/);
});

test("model tools are bounded, discoverable, use native approval tiers, and never touch active tools", async () => {
	const h = harness();
	registerRoomTools(h.api, {}, new RoomSessionState(), kit);
	assert.deepEqual([...h.tools.keys()], ["room_context", "room_status", "room_snapshots", "room_save", "jat_inspect"]);
	const approvals = Object.fromEntries([...h.tools.values()].map((tool) => [tool.name, tool.approval]));
	assert.deepEqual(approvals, { room_context: "read", room_status: "read", room_snapshots: "read", room_save: "write", jat_inspect: "read" });
	for (const tool of h.tools.values()) {
		assert.equal(tool.loadMode, "discoverable");
		assert.equal(tool.parameters.strictMode, true);
		assert.equal(typeof tool.renderCall, "function");
		assert.equal(typeof tool.renderResult, "function");
	}
	assert.deepEqual(Object.keys(h.tools.get("jat_inspect").parameters.shape), ["haul"]);
	assert.equal(h.tools.get("jat_inspect").parameters.shape.haul.maxLength, 4096);
	for (const name of ["room_context", "room_status", "room_snapshots", "room_save"]) assert.deepEqual(h.tools.get(name).parameters.shape, {});
});

test("room_save tool records provenance; failures are errors without private paths", async () => {
	const h = harness();
	const state = new RoomSessionState();
	let fail = false;
	const controller = { async save() {
		if (fail) throw new Error("credential unavailable at /home/user/.secret");
		return { context: linked, receipt: { kind: "saved", status: "saved", projectId: "demo-room", snapshotId: "jat-new", previousSnapshotId: "jat-old", dataAddedBytes: 1 } };
	} };
	registerRoomTools(h.api, controller, state, kit);
	const ok = await h.tools.get("room_save").execute("id", {}, undefined, undefined, h.ctx);
	assert.equal(ok.details.snapshot_id, "jat-new");
	assert.equal(ok.details.previous_snapshot_id, "jat-old");
	assert.equal(h.appended[0][1].snapshot_id, ok.details.snapshot_id, "provenance corroborates the tool details");
	fail = true;
	const failed = await h.tools.get("room_save").execute("id", {}, undefined, undefined, h.ctx);
	assert.equal(failed.isError, true);
	assert.match(failed.content[0].text, /previous snapshot remains authoritative/);
	assert.doesNotMatch(JSON.stringify(failed), /\/home|secret|credential/);
	assert.equal(state.room.snapshotId, "jat-new");
});

test("tool renderers distinguish success, failure, cancellation and partial progress", () => {
	const text = (component) => component.args[0];
	assert.match(text(renderRoomResult(kit, { content: [{ type: "text", text: "Room Demo Room · clean · jat-x" }], details: { ok: true } }, { expanded: false, isPartial: false }, theme)), /^<success>✓<\/success> Room Demo Room/);
	assert.match(text(renderRoomResult(kit, { content: [{ type: "text", text: "Josh Room operation failed." }], details: { ok: false, error: "failed" }, isError: true }, { isPartial: false }, theme)), /^<error>✗ /);
	assert.match(text(renderRoomResult(kit, { content: [{ type: "text", text: "Josh Room operation cancelled." }], details: { ok: false, error: "cancelled" }, isError: true }, { isPartial: false }, theme)), /^<warning>⊘ /);
	assert.match(text(renderRoomResult(kit, { content: [{ type: "text", text: "Saving Josh Room checkpoint…" }], details: { ok: false, state: "running" } }, { isPartial: true }, theme)), /^<muted>… Saving/);
	assert.match(text(renderRoomResult(kit, { content: [{ type: "text", text: "No valid Josh Room binding" }], details: { ok: false, state: "unlinked" } }, { isPartial: false }, theme)), /^<warning>○ /);
	const expanded = text(renderRoomResult(kit, { content: [{ type: "text", text: "2 snapshots" }], details: { ok: true, snapshots: [{ snapshot_id: "jat-a", created_at: "2026-01-01T00:00:00Z" }] } }, { expanded: true, isPartial: false }, theme));
	assert.match(expanded, /jat-a · 2026-01-01/);
	assert.doesNotMatch(text(renderRoomResult(kit, { content: [{ type: "text", text: "bad\u001b[31m\nline" }], details: { ok: true } }, { isPartial: false }, theme)), /\u001b|\n/);
});

test("startup outside a marker-shaped workspace spawns nothing and stays silent", async () => {
	const handlers = new Map();
	let probes = 0;
	const h = harness();
	registerRoomLifecycle({ on(name, handler) { handlers.set(name, handler); } }, { async context() { probes += 1; return linked; } }, new RoomSessionState(), async () => false);
	await handlers.get("session_start")({}, { ...h.ctx, sessionManager: { getBranch: () => [] } });
	assert.equal(probes, 0);
	assert.deepEqual(h.statuses, []);
	assert.deepEqual(h.notifications, []);
});

test("valid Room shows themed status; edits mark dirty, execution marks unknown, Room tools are ignored", async () => {
	const handlers = new Map();
	const h = harness();
	const state = new RoomSessionState();
	registerRoomLifecycle({ on(name, handler) { handlers.set(name, handler); } }, { async context() { return linked; } }, state, async () => true);
	const ctx = { ...h.ctx, sessionManager: { getBranch: () => [] } };
	await handlers.get("session_start")({}, ctx);
	assert.deepEqual(h.statuses.at(-1), ["josh-room", "<dim>room:</dim><accent>Demo Room</accent> <muted>?</muted>"]);
	state.mark(ctx, "clean");
	await handlers.get("tool_execution_end")({ toolName: "bash", isError: false }, ctx);
	assert.equal(state.indicator, "unknown");
	await handlers.get("tool_execution_end")({ toolName: "write", isError: false }, ctx);
	assert.equal(state.indicator, "changed");
	await handlers.get("tool_execution_end")({ toolName: "bash", isError: false }, ctx);
	assert.equal(state.indicator, "changed", "known-dirty stays dirty");
	state.mark(ctx, "clean");
	await handlers.get("tool_execution_end")({ toolName: "room_status", isError: false }, ctx);
	await handlers.get("tool_execution_end")({ toolName: "write", isError: false }, { ...ctx, agent: { kind: "sub" } });
	assert.equal(state.indicator, "clean", "Room tools and subagent events leave the parent indicator alone");
	await handlers.get("tool_execution_end")({ toolName: "apply_patch", isError: false }, ctx);
	assert.equal(state.indicator, "changed");
	state.mark(ctx, "clean");
	await handlers.get("tool_execution_end")({ toolName: "edit", isError: true }, ctx);
	assert.equal(state.indicator, "unknown", "a failed mutating tool may have partially written");
});

test("invalid or stale binding clears status; missing runtime is silent at startup", async () => {
	const handlers = new Map();
	const h = harness();
	const state = new RoomSessionState();
	let result = linked;
	registerRoomLifecycle({ on(name, handler) { handlers.set(name, handler); } }, { async context() { return result; } }, state, async () => true);
	const ctx = { ...h.ctx, sessionManager: { getBranch: () => [] } };
	await handlers.get("session_start")({}, ctx);
	result = { kind: "invalid" };
	await handlers.get("session_switch")({}, ctx);
	assert.deepEqual(h.statuses.at(-1), ["josh-room", undefined]);
	result = undefined; // controller.context(..., { silent: true }) maps missing runtime to undefined
	await handlers.get("session_tree")({}, ctx);
	assert.deepEqual(h.notifications, []);
	assert.equal(state.room, undefined);
});

test("session provenance is restored per branch, scoped to the current Room, and never consulted for Save", async () => {
	const handlers = new Map();
	const h = harness();
	const state = new RoomSessionState();
	let branch = [{ type: "custom", customType: PROVENANCE_TYPE, data: { format_version: 1, dimension_id: "local", project_id: "demo-room", snapshot_id: "jat-old" } }];
	registerRoomLifecycle({ on(name, handler) { handlers.set(name, handler); } }, { async context() { return linked; } }, state, async () => true);
	const ctx = { ...h.ctx, sessionManager: { getBranch: () => branch } };
	await handlers.get("session_start")({}, ctx);
	assert.equal(state.provenance.snapshot_id, "jat-old");
	branch = [{ type: "custom", customType: PROVENANCE_TYPE, data: { format_version: 1, dimension_id: "other", project_id: "foreign", snapshot_id: "jat-x" } }];
	await handlers.get("session_branch")({}, ctx);
	assert.equal(state.provenance, undefined, "provenance from another Room is ignored");
	let saveContextSource;
	registerRoomCommand(h.api, { async save(cwd) { saveContextSource = cwd; return { context: linked, receipt: { kind: "already-saved", status: "already-saved", projectId: "demo-room", snapshotId: "jat-old", dataAddedBytes: 0 } }; } }, state, kit);
	await h.commands.get("room").handler("save", { ...ctx, mode: "rpc" });
	assert.equal(saveContextSource, "/workspace", "Save re-resolves the Room through the controller");
});
