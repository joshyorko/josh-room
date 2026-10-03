import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createRoomProcessRunner, RoomCliError, safeChildEnvironment } from "../src/process.ts";
import { createRoomController } from "../src/controller.ts";

test("child environment copies only the approved non-secret host variables", () => {
	assert.deepEqual(safeChildEnvironment({
		PATH: "/usr/bin",
		HOME: "/home/user",
		XDG_CONFIG_HOME: "/home/user/.config",
		DBUS_SESSION_BUS_ADDRESS: "unix:path=/run/user/1000/bus",
		JOSH_ROOM_IDENTITY: "/secret/identity.txt",
		AWS_SECRET_ACCESS_KEY: "secret",
		OPENAI_API_KEY: "secret",
	}), { PATH: "/usr/bin", HOME: "/home/user", XDG_CONFIG_HOME: "/home/user/.config", DBUS_SESSION_BUS_ADDRESS: "unix:path=/run/user/1000/bus" });
});

test("startup context is a single bounded, local CLI call and malformed output fails quiet", async () => {
	const calls = [];
	const controller = createRoomController(async (args, options) => {
		calls.push({ args, options });
		return { format_version: 2, ok: true, state: "linked", linked: true, path_matches: true };
	});
	assert.equal(await controller.context("/workspace", undefined, { silent: true }), undefined);
	assert.equal(calls.length, 1);
	assert.deepEqual(calls[0].args, ["context", "--workspace", "/workspace", "--json"]);
	assert.equal(calls[0].options.timeoutMs, 2000);
});

test("save uses only the core Save contract and retains prior snapshot provenance", async () => {
	const calls = [];
	const linked = { format_version: 1, ok: true, state: "linked", linked: true, path_matches: true, dimension_id: "local", project_id: "demo-room", display_name: "Demo Room", snapshot_id: "jat-old" };
	const controller = createRoomController(async (args) => {
		calls.push(args);
		return args[0] === "context" ? linked : { ok: true, project_id: "demo-room", snapshot_id: "jat-new", ciphertext_size: 12, producer: { argv: ["/private/path"] } };
	});
	const result = await controller.save("/workspace");
	assert.deepEqual(calls[1], ["snapshot", "create", "demo-room", "--source", "/workspace", "--dimension", "local", "--json"]);
	assert.equal(result.receipt.previousSnapshotId, "jat-old");
	assert.equal(JSON.stringify(result).includes("/private/path"), false);
});

test("abort terminates the CLI process group, including inherited descendants", async () => {
	const directory = await mkdtemp(join(tmpdir(), "josh-room-omp-test-"));
	const marker = join(directory, "child-stopped");
	const ready = `${marker}.ready`;
	const descendant = `process.on('SIGTERM',()=>{require('node:fs').writeFileSync(${JSON.stringify(marker)},'stopped');process.exit(0)});setInterval(()=>{},1000)`;
	const script = `const fs=require('node:fs');const {spawn}=require('node:child_process');spawn(process.execPath,['-e',${JSON.stringify(descendant)}],{stdio:'ignore'});fs.writeFileSync(${JSON.stringify(ready)},'ready');setInterval(()=>{},1000);`;
	const runner = createRoomProcessRunner((command, _args, options) => spawn(process.execPath, ["-e", script], options), () => ({ PATH: process.env.PATH ?? "" }));
	const abort = new AbortController();
	const operation = runner(["status"], { cwd: directory, signal: abort.signal, timeoutMs: 5_000 });
	const deadline = Date.now() + 2_000;
	let isReady = false;
	while (Date.now() < deadline) {
		try {
			await stat(ready);
			isReady = true;
			break;
		} catch {
			await new Promise((resolve) => setTimeout(resolve, 20));
		}
	}
	assert.equal(isReady, true);
	await new Promise((resolve) => setTimeout(resolve, 80));
	abort.abort();
	await assert.rejects(operation, (error) => error instanceof RoomCliError && error.kind === "cancelled");
	let descendantStopped = false;
	try { await stat(marker); descendantStopped = true; } catch { descendantStopped = false; }
	assert.equal(descendantStopped, true);
	await rm(directory, { recursive: true, force: true });
});

test("a completed success receipt wins a late abort", async () => {
	const runner = createRoomProcessRunner((_command, _args, options) => spawn(process.execPath, ["-e", "process.stdout.write(JSON.stringify({ok:true,snapshot_id:'jat-committed'})+'\\n');setInterval(()=>{},1000)"], options));
	const abort = new AbortController();
	const operation = runner(["snapshot", "create"], { cwd: process.cwd(), signal: abort.signal, timeoutMs: 5_000 });
	await new Promise((resolve) => setTimeout(resolve, 80));
	abort.abort();
	assert.deepEqual(await operation, { ok: true, snapshot_id: "jat-committed" });
});

test("CLI diagnostics omit private paths and sensitive terms", async () => {
	const runner = createRoomProcessRunner((_command, _args, options) => {
		const child = spawn(process.execPath, ["-e", "process.stdout.write(JSON.stringify({ok:false,error:'room marker is invalid'}));process.exit(2)"], options);
		return child;
	});
	await assert.rejects(runner(["context"], { cwd: process.cwd() }), /room marker is invalid/);
	const unsafe = createRoomProcessRunner((_command, _args, options) => spawn(process.execPath, ["-e", "process.stdout.write(JSON.stringify({ok:false,error:'credential unavailable at /private/key'}));process.exit(2)"], options));
	await assert.rejects(unsafe(["context"], { cwd: process.cwd() }), /Josh Room operation failed/);
});
