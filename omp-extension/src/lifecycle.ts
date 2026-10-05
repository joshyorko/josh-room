import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { parseContextResult, roomStatusText, type RoomContext } from "./contracts.ts";
import type { RoomController } from "./controller.ts";

const PROVENANCE_TYPE = "com.joshyorko.josh-room.checkpoint.v1";

interface Provenance {
	format_version: 1;
	dimension_id: string;
	project_id: string;
	previous_snapshot_id?: string;
	snapshot_id: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isIdentifier(value: unknown): value is string {
	return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value);
}

export function readProvenance(ctx: ExtensionContext): Provenance | undefined {
	const entry = [...ctx.sessionManager.getBranch()].reverse().find((item) => item.type === "custom" && item.customType === PROVENANCE_TYPE);
	if (!entry || entry.type !== "custom") return undefined;
	const data = entry.data;
	if (!isRecord(data) || data.format_version !== 1 || (data.previous_snapshot_id !== undefined && !isIdentifier(data.previous_snapshot_id))) return undefined;
	const parsed = parseContextResult({
		format_version: 1,
		ok: true,
		state: "linked",
		linked: true,
		path_matches: true,
		dimension_id: data.dimension_id,
		project_id: data.project_id,
		display_name: "Provenance",
		snapshot_id: data.snapshot_id,
	});
	if (parsed?.kind !== "linked") return undefined;
	return {
		format_version: 1,
		dimension_id: parsed.dimensionId,
		project_id: parsed.projectId,
		...(typeof data.previous_snapshot_id === "string" ? { previous_snapshot_id: data.previous_snapshot_id } : {}),
		snapshot_id: parsed.snapshotId,
	};
}

async function refreshRoomStatus(ctx: ExtensionContext, controller: RoomController, hadStatus: { value: boolean }): Promise<RoomContext | undefined> {
	const context = await controller.context(ctx.cwd, undefined, { silent: true });
	if (!context || context.kind !== "linked") {
		if (hadStatus.value) ctx.ui.setStatus("josh-room", undefined);
		hadStatus.value = false;
		return context;
	}
	ctx.ui.setStatus("josh-room", roomStatusText(context, undefined));
	hadStatus.value = true;
	return context;
}

export function registerRoomLifecycle(pi: ExtensionAPI, controller: RoomController): void {
	let activeRoom: RoomContext | undefined;
	const hadStatus = { value: false };
	pi.on("session_start", async (_event, ctx) => {
		readProvenance(ctx); // Provenance is intentionally never used to authorize an operation.
		activeRoom = await refreshRoomStatus(ctx, controller, hadStatus);
	});
	pi.on("session_switch", async (_event, ctx) => { activeRoom = await refreshRoomStatus(ctx, controller, hadStatus); });
	pi.on("session_branch", async (_event, ctx) => {
		readProvenance(ctx);
		activeRoom = await refreshRoomStatus(ctx, controller, hadStatus);
	});
	pi.on("session_tree", async (_event, ctx) => {
		readProvenance(ctx);
		activeRoom = await refreshRoomStatus(ctx, controller, hadStatus);
	});
	pi.on("turn_end", async (_event, ctx) => {
		if (activeRoom?.kind === "linked") ctx.ui.setStatus("josh-room", `room:${activeRoom.displayName} ?`);
	});
}
