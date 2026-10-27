import { lstat } from "node:fs/promises";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";
import { ROOM_TOOL_APPROVALS } from "./contracts.ts";
import type { RoomController } from "./controller.ts";
import type { RoomSessionState } from "./state.ts";

const MARKER_NAME = ".josh-room.json";
/** Built-in tools whose success may change workspace files: `changed` for edits, `unknown` for execution. */
const WORKSPACE_EFFECT: Record<string, "changed" | "unknown"> = { edit: "changed", write: "changed", ast_edit: "changed", bash: "unknown", eval: "unknown" };

/** Existence check only; marker validation stays with `josh-room context`. */
export async function markerPresent(cwd: string): Promise<boolean> {
	try {
		await lstat(join(cwd, MARKER_NAME));
		return true;
	} catch {
		return false;
	}
}

async function refreshRoomStatus(ctx: ExtensionContext, controller: RoomController, state: RoomSessionState, hasMarker: (cwd: string) => Promise<boolean>): Promise<void> {
	state.restoreProvenance(ctx);
	// Outside a marker-shaped workspace: no subprocess, status, widget, notification, or network.
	if (!await hasMarker(ctx.cwd)) {
		state.clear(ctx);
		return;
	}
	const context = await controller.context(ctx.cwd, undefined, { silent: true });
	if (context?.kind !== "linked") {
		state.clear(ctx);
		return;
	}
	// Identity only: clean/changed needs the authoritative `/room status` or `room_status`.
	state.show(ctx, context, state.room?.snapshotId === context.snapshotId && state.room.projectId === context.projectId ? state.indicator : "unknown");
}

export function registerRoomLifecycle(pi: ExtensionAPI, controller: RoomController, state: RoomSessionState, hasMarker: (cwd: string) => Promise<boolean> = markerPresent): void {
	const refresh = async (_event: unknown, ctx: ExtensionContext) => {
		if (ctx.agent?.kind === "sub") return;
		await refreshRoomStatus(ctx, controller, state, hasMarker);
	};
	pi.on("session_start", refresh);
	pi.on("session_switch", refresh);
	pi.on("session_branch", refresh);
	pi.on("session_tree", refresh);
	pi.on("tool_execution_end", async (event, ctx) => {
		if (event.isError || !state.room || Object.hasOwn(ROOM_TOOL_APPROVALS, event.toolName) || !Object.hasOwn(WORKSPACE_EFFECT, event.toolName)) return;
		const effect = WORKSPACE_EFFECT[event.toolName];
		// Pessimistic hints only; no re-hash. A known-dirty Room stays dirty after arbitrary execution.
		if (effect === "changed" || state.indicator === "clean") state.mark(ctx, effect);
	});
}
