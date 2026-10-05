import {
	parseContextResult,
	parseDoctorResult,
	parseInspectResult,
	parseRoomsResult,
	parseSaveResult,
	parseSnapshotsResult,
	parseStatusEnvelope,
	parseStatusResult,
	type RoomContext,
	type RoomStatus,
	type SaveReceipt,
	type SnapshotList,
	type RoomList,
	type DoctorResult,
	type JatInspection,
} from "./contracts.ts";
import { runRoomCli, RoomCliError } from "./process.ts";

export type CliRunner = (args: readonly string[], options: { cwd: string; signal?: AbortSignal; timeoutMs?: number; allowFailure?: boolean }) => Promise<unknown>;

export interface RoomController {
	context(cwd: string, signal?: AbortSignal, options?: { silent?: boolean }): Promise<RoomContext | undefined>;
	status(cwd: string, signal?: AbortSignal): Promise<{ context: Extract<RoomContext, { kind: "linked" }>; status: RoomStatus }>;
	snapshots(cwd: string, signal?: AbortSignal): Promise<{ context: Extract<RoomContext, { kind: "linked" }>; snapshots: SnapshotList }>;
	rooms(cwd: string, signal?: AbortSignal): Promise<RoomList>;
	doctor(cwd: string, signal?: AbortSignal): Promise<DoctorResult>;
	save(cwd: string, signal?: AbortSignal, onProgress?: (message: string) => void): Promise<{ context: Extract<RoomContext, { kind: "linked" }>; receipt: SaveReceipt }>;
	inspectJat(cwd: string, haul: string, signal?: AbortSignal): Promise<JatInspection>;
}

function requireLinked(context: RoomContext | undefined): Extract<RoomContext, { kind: "linked" }> {
	if (!context || context.kind !== "linked") throw new RoomCliError("invalid-result");
	return context;
}

function requireResult<T>(value: unknown, parser: (input: unknown) => T | undefined): T {
	const result = parser(value);
	if (!result) throw new RoomCliError("invalid-result");
	return result;
}

function requireMatchingStatus(value: unknown, context: Extract<RoomContext, { kind: "linked" }>): RoomStatus {
	const status = requireResult(value, parseStatusResult);
	if (status.kind === "known" && (
		status.dimensionId !== context.dimensionId || status.projectId !== context.projectId || status.snapshotId !== context.snapshotId
	)) throw new RoomCliError("invalid-result");
	return status;
}

async function legacyStatus(run: CliRunner, cwd: string, signal?: AbortSignal) {
	const context = requireLinked(requireResult(
		await run(["context", "--workspace", cwd, "--json"], { cwd, signal, timeoutMs: 2_000 }),
		parseContextResult,
	));
	const status = requireMatchingStatus(
		await run(["status", "--workspace", cwd, "--json"], { cwd, signal, timeoutMs: 30_000 }),
		context,
	);
	return { context, status };
}

export function createRoomController(run: CliRunner = runRoomCli): RoomController {
	return {
		async context(cwd, signal, options) {
			try {
				const parsed = parseContextResult(await run(["context", "--workspace", cwd, "--json"], { cwd, signal, timeoutMs: 2_000 }));
				if (!parsed) throw new RoomCliError("invalid-result");
				return parsed;
			} catch (error) {
				if (signal?.aborted) throw new RoomCliError("cancelled");
				if (options?.silent) return undefined;
				throw error;
			}
		},
		async status(cwd, signal) {
			try {
				return requireResult(
					await run(["status", "--workspace", cwd, "--include-context", "--json"], { cwd, signal, timeoutMs: 30_000 }),
					parseStatusEnvelope,
				);
			} catch (error) {
				if (error instanceof RoomCliError && error.kind === "unsupported-option") return legacyStatus(run, cwd, signal);
				throw error;
			}
		},
		async snapshots(cwd, signal) {
			const context = requireLinked(await this.context(cwd, signal));
			const snapshots = requireResult(
				await run(["snapshots", "list", context.projectId, "--dimension", context.dimensionId, "--json"], { cwd, signal, timeoutMs: 60_000 }),
				parseSnapshotsResult,
			);
			return { context, snapshots };
		},
		async rooms(cwd, signal) {
			const context = requireLinked(await this.context(cwd, signal));
			return requireResult(
				await run(["projects", "list", "--dimension", context.dimensionId, "--json"], { cwd, signal, timeoutMs: 60_000 }),
				parseRoomsResult,
			);
		},
		async doctor(cwd, signal) {
			return requireResult(
				await run(["doctor", "--backend", "local", "--ide", "terminal", "--json"], { cwd, signal, timeoutMs: 120_000, allowFailure: true }),
				parseDoctorResult,
			);
		},
		async save(cwd, signal, onProgress) {
			const context = requireLinked(await this.context(cwd, signal));
			onProgress?.("Saving Josh Room checkpoint…");
			const raw = await run(
				["snapshot", "create", context.projectId, "--source", cwd, "--dimension", context.dimensionId, "--json"],
				{ cwd, signal, timeoutMs: 30 * 60_000 },
			);
			const parsed = requireResult(raw, parseSaveResult);
			if (parsed.projectId !== context.projectId) throw new RoomCliError("invalid-result");
			if (parsed.kind === "already-saved") {
				if (parsed.snapshotId !== context.snapshotId) throw new RoomCliError("invalid-result");
				return { context, receipt: parsed };
			}
			if (parsed.snapshotId === context.snapshotId) throw new RoomCliError("invalid-result");
			return {
				context,
				receipt: { ...parsed, previousSnapshotId: parsed.previousSnapshotId ?? context.snapshotId },
			};
		},
		async inspectJat(cwd, haul, signal) {
			if (!haul || haul.length > 4096 || haul.includes("\0") || !isAbsoluteLocalPath(haul)) throw new RoomCliError("invalid-result");
			const raw = await run(["jat", "inspect", "--haul", haul, "--json"], { cwd, signal, timeoutMs: 10 * 60_000 });
			return requireResult(raw, parseInspectResult);
		},
	};
}

function isAbsoluteLocalPath(value: string): boolean {
	return value.startsWith("/") && !value.startsWith("//") || /^[A-Za-z]:\\/.test(value);
}

export const roomController = createRoomController();
