import { spawn as nodeSpawn } from "node:child_process";
import { parseStatusResult } from "./contracts.ts";

const OUTPUT_LIMIT = 256 * 1024;
const UNSUPPORTED_OPTION_STDERR_LIMIT = 4 * 1024;
const TERMINATION_GRACE_MS = 2_000;
const SAFE_ENVIRONMENT = [
	"PATH",
	"HOME",
	"XDG_CONFIG_HOME",
	"XDG_CACHE_HOME",
	"XDG_RUNTIME_DIR",
	"DBUS_SESSION_BUS_ADDRESS",
	"LANG",
	"LC_ALL",
	"SSL_CERT_FILE",
	"SSL_CERT_DIR",
] as const;

export class RoomCliError extends Error {
	readonly kind: "missing-runtime" | "cancelled" | "timeout" | "output-limit" | "invalid-result" | "unsupported-option" | "failed";

	constructor(kind: RoomCliError["kind"], diagnostic?: string) {
		const messages = {
			"missing-runtime": "Josh Room CLI is unavailable. Install the Josh Room runtime to use this command.",
			cancelled: "Josh Room operation cancelled.",
		timeout: "Josh Room operation timed out.",
		"output-limit": "Josh Room returned more data than the extension accepts.",
		"invalid-result": "Josh Room returned an unsupported or invalid JSON result.",
		"unsupported-option": "This Josh Room runtime does not support the requested status option.",
		failed: "Josh Room operation failed. Run `josh-room doctor` for local diagnostics.",
		};
		super(kind === "failed" && diagnostic ? diagnostic : messages[kind]);
		this.name = "RoomCliError";
		this.kind = kind;
	}
}

interface SpawnOptions {
	cwd: string;
	env: Record<string, string>;
	detached: boolean;
	stdio: ["ignore", "pipe", "pipe"];
}

type Spawn = (command: string, args: readonly string[], options: SpawnOptions) => ReturnType<typeof nodeSpawn>;

export function safeChildEnvironment(source: NodeJS.ProcessEnv = process.env): Record<string, string> {
	const result: Record<string, string> = {};
	for (const key of SAFE_ENVIRONMENT) {
		const value = source[key];
		if (typeof value !== "string" || value.length === 0 || value.length > 4096 || value.includes("\0")) continue;
		if (key === "DBUS_SESSION_BUS_ADDRESS" && !/^unix:path=\/run\/user\/\d+\/bus$/.test(value)) continue;
		result[key] = value;
	}
	return result;
}

function processGroupSignal(pid: number | undefined, signal: NodeJS.Signals): void {
	if (pid === undefined) return;
	try {
		process.kill(-pid, signal);
	} catch {
		try {
			process.kill(pid, signal);
		} catch {
			// The process has already exited.
		}
	}
}

export function createRoomProcessRunner(
	spawn: Spawn = nodeSpawn,
	environment: () => Record<string, string> = () => safeChildEnvironment(),
	platform: NodeJS.Platform = process.platform,
) {
	return async function runRoomCli(
		args: readonly string[],
		options: { cwd: string; signal?: AbortSignal; timeoutMs?: number; maxOutputBytes?: number; allowFailure?: boolean },
	): Promise<unknown> {
		if (options.signal?.aborted) throw new RoomCliError("cancelled");
		const cwd = options.cwd;
		if (!cwd || cwd.length > 4096 || cwd.includes("\0")) throw new RoomCliError("failed");
		const limit = options.maxOutputBytes ?? OUTPUT_LIMIT;
		const timeoutMs = options.timeoutMs ?? 120_000;
		const child = spawn("josh-room", [...args], {
			cwd,
			env: environment(),
			detached: platform !== "win32",
			stdio: ["ignore", "pipe", "pipe"],
		});
		const stdout: Uint8Array[] = [];
		const stderr: Uint8Array[] = [];
		let stdoutBytes = 0;
		let stderrBytes = 0;
		let overflow = false;
		let spawnFailed = false;
		let timedOut = false;
		let aborted = false;
		const statusContextProbe = args[0] === "status" && args.includes("--include-context");
		let forceKillTimer: ReturnType<typeof setTimeout> | undefined;
		const killTree = () => {
			processGroupSignal(child.pid, "SIGTERM");
			// Give Josh Room's SIGTERM unwind and owned-child cleanup time to finish;
			// SIGKILL is only the bounded last resort if the CLI remains alive.
			forceKillTimer ??= setTimeout(() => processGroupSignal(child.pid, "SIGKILL"), TERMINATION_GRACE_MS);
		};
		const onAbort = () => {
			aborted = true;
			killTree();
		};
		options.signal?.addEventListener("abort", onAbort, { once: true });
		const timeout = setTimeout(() => {
			timedOut = true;
			killTree();
		}, timeoutMs);
		const finished = new Promise<{ code: number | null; error?: Error }>((resolve) => {
			child.once("error", (error) => {
				spawnFailed = true;
				resolve({ code: null, error });
			});
			child.once("close", (code) => resolve({ code }));
		});
		const collect = (target: Uint8Array[], chunk: Uint8Array, current: number, cap: number): number => {
			if (current + chunk.byteLength > cap) {
				overflow = true;
				killTree();
				return current;
			}
			target.push(chunk);
			return current + chunk.byteLength;
		};
		child.stdout?.on("data", (chunk: Uint8Array) => { stdoutBytes = collect(stdout, chunk, stdoutBytes, limit); });
		child.stderr?.on("data", (chunk: Uint8Array) => {
			// Keep only a small in-memory window to recognize argparse's exact
			// unsupported-option response. Never expose or persist raw diagnostics.
			if (!statusContextProbe || stderrBytes + chunk.byteLength > UNSUPPORTED_OPTION_STDERR_LIMIT) return;
			stderr.push(chunk);
			stderrBytes += chunk.byteLength;
		});
		try {
			const result = await finished;
			if (spawnFailed) throw new RoomCliError("missing-runtime");
			if (overflow) throw new RoomCliError("output-limit");
			const text = new TextDecoder("utf-8", { fatal: true }).decode(concat(stdout, stdoutBytes));
			if (statusContextProbe && result.code === 2 && !text && unsupportedIncludeContextOption(stderr, stderrBytes)) {
				throw new RoomCliError("unsupported-option");
			}
			const interrupted = aborted || options.signal?.aborted === true;
			if (!text && interrupted) throw new RoomCliError("cancelled");
			if (!text && timedOut) throw new RoomCliError("timeout");
			let parsed: unknown;
			try {
				parsed = JSON.parse(text);
			} catch {
				if (interrupted) throw new RoomCliError("cancelled");
				if (timedOut) throw new RoomCliError("timeout");
				throw new RoomCliError("invalid-result");
			}
			if ((result.code === 0 || interrupted) && typeof parsed === "object" && parsed !== null && "ok" in parsed && parsed.ok === true) return parsed;
			if (interrupted) throw new RoomCliError("cancelled");
			if (timedOut) throw new RoomCliError("timeout");
			const statusReceipt = args[0] === "status" ? parseStatusResult(parsed) : undefined;
			const changedStatusExit = result.code === 2 && statusReceipt?.kind === "known" && statusReceipt.state === "changed";
			if (!options.allowFailure && !changedStatusExit && isRecord(parsed) && parsed.ok === false) {
				const diagnostic = "error" in parsed ? safeDiagnostic(parsed.error) : undefined;
				throw new RoomCliError("failed", diagnostic);
			}
			if (result.code !== 0 && !options.allowFailure && !changedStatusExit) throw new RoomCliError("failed");
			return parsed;
		} finally {
			clearTimeout(timeout);
			if (forceKillTimer) clearTimeout(forceKillTimer);
			options.signal?.removeEventListener("abort", onAbort);
		}
	};
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function safeDiagnostic(value: unknown): string | undefined {
	if (typeof value !== "string") return undefined;
	const text = value.replace(/[\r\n\t]+/g, " ").replace(/\s+/g, " ").trim();
	if (
		text.length === 0 || text.length > 220 || /[/\\]|:\/\/|[\u0000-\u001f\u007f]/.test(text) ||
		/secret|credential|identity|token|password|private key|authorization|access key/i.test(text)
	) return undefined;
	return text;
}

function concat(chunks: readonly Uint8Array[], length: number): Uint8Array {
	const result = new Uint8Array(length);
	let offset = 0;
	for (const chunk of chunks) {
		result.set(chunk, offset);
		offset += chunk.byteLength;
	}
	return result;
}

function unsupportedIncludeContextOption(chunks: readonly Uint8Array[], length: number): boolean {
	if (length === 0 || length > UNSUPPORTED_OPTION_STDERR_LIMIT) return false;
	const text = new TextDecoder().decode(concat(chunks, length));
	return /(?:^|\n)(?:[^:\r\n]+: )?error: unrecognized arguments: --include-context(?:\r?\n|$)/.test(text);
}

export const runRoomCli = createRoomProcessRunner();
