import {
	ROOM_TOOL_APPROVALS,
	presentSaveOutcome,
	roomStatusText,
	snapshotOptions,
	type RoomContext,
	type RoomStatus,
} from "./contracts.ts";
import type { RoomController } from "./controller.ts";
import { Text } from "@oh-my-pi/pi-tui";
import type { ExtensionAPI, ToolDefinition } from "@oh-my-pi/pi-coding-agent";
import type { TSchema } from "@oh-my-pi/pi-ai";
import { RoomCliError } from "./process.ts";

interface ToolResult {
	content: Array<{ type: "text"; text: string }>;
	details: Record<string, unknown>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

const PROVENANCE_TYPE = "com.joshyorko.josh-room.checkpoint.v1";

function output(text: string, details: Record<string, unknown>): ToolResult {
	return { content: [{ type: "text", text }], details };
}

function errorResult(error: unknown): ToolResult {
	const message = error instanceof RoomCliError ? error.message : "Josh Room operation failed. Run `josh-room doctor` for local diagnostics.";
	return output(message, { ok: false, error: error instanceof RoomCliError ? error.kind : "failed" });
}

function tool<TParams extends TSchema>(
	name: string,
	label: string,
	description: string,
	approval: "read" | "write",
	parameters: TParams,
	execute: ToolDefinition<TParams>["execute"],
): ToolDefinition<TParams> {
	return {
		name,
		label,
		description,
		approval,
		parameters,
		execute,
		renderCall: (_args, _options, theme) => new Text(theme.fg("accent", label), 0, 0),
		renderResult: (_result, _options, theme) => new Text(theme.fg("success", "Josh Room complete"), 0, 0),
	};
}

export function registerRoomTools(api: ExtensionAPI, controller: RoomController): void {
	const z = api.zod;
	const noArguments = z.object({}).strict();
	const haulArguments = z.object({ haul: z.string().min(1).max(4096) }).strict();
	api.registerTool(tool("room_context", "Room Context", "Show the validated local Josh Room binding.", ROOM_TOOL_APPROVALS.room_context, noArguments, async (_id, _params, signal, _update, ctx) => {
		try {
			const context = await controller.context(ctx.cwd, signal);
			if (!context || context.kind !== "linked") return output("No valid Josh Room binding is available in this workspace.", { ok: false, state: context?.kind ?? "unavailable" });
			return output(`Room ${context.displayName} · ${context.projectId} · latest ${context.snapshotId}`, { ok: true, state: "linked", dimension_id: context.dimensionId, project_id: context.projectId, snapshot_id: context.snapshotId });
		} catch (error) { return errorResult(error); }
	}));
	api.registerTool(tool("room_status", "Room Status", "Read authoritative Josh Room workspace status.", ROOM_TOOL_APPROVALS.room_status, noArguments, async (_id, _params, signal, _update, ctx) => {
		try {
			const { context, status } = await controller.status(ctx.cwd, signal);
			ctx.ui.setStatus("josh-room", roomStatusText(context, status));
			return output(`Room ${context.displayName} · ${status.kind === "known" ? status.state : "unknown"}`, { ok: true, state: status.kind === "known" ? status.state : "unknown", project_id: context.projectId, snapshot_id: status.kind === "known" ? status.snapshotId : context.snapshotId });
		} catch (error) { return errorResult(error); }
	}));
	api.registerTool(tool("room_snapshots", "Room Snapshots", "Browse saved snapshots for the current Room.", ROOM_TOOL_APPROVALS.room_snapshots, noArguments, async (_id, _params, signal, _update, ctx) => {
		try {
			const { context, snapshots } = await controller.snapshots(ctx.cwd, signal);
			const visibleSnapshots = snapshots.snapshots.slice(0, 25);
			return output(`${snapshots.snapshots.length} snapshots · latest ${snapshots.latestSnapshotId}${visibleSnapshots.length < snapshots.snapshots.length ? ` · showing ${visibleSnapshots.length}` : ""}`, {
				ok: true,
				project_id: context.projectId,
				latest_snapshot_id: snapshots.latestSnapshotId,
				snapshot_count: snapshots.snapshots.length,
				snapshots: visibleSnapshots.map((item) => ({ snapshot_id: item.snapshotId, created_at: item.createdAt, ciphertext_size: item.ciphertextSize })),
			});
		} catch (error) { return errorResult(error); }
	}));
	api.registerTool(tool("room_save", "Save Room Checkpoint", "Save the current Josh Room workspace as an explicit checkpoint.", ROOM_TOOL_APPROVALS.room_save, noArguments, async (_id, _params, signal, onUpdate, ctx) => {
		try {
			const { context, receipt } = await controller.save(ctx.cwd, signal, (message) => onUpdate?.({ content: [{ type: "text", text: message }] }));
			const outcome = presentSaveOutcome(context, receipt);
			if (outcome.checkpointEntry) api.appendEntry(PROVENANCE_TYPE, outcome.checkpointEntry);
			ctx.ui.setStatus("josh-room", outcome.statusText);
			return output(outcome.message, outcome.details);
		} catch (error) { return errorResult(error); }
	}));
	api.registerTool(tool("jat_inspect", "Inspect JAT", "Inspect a local JAT haul through Josh Room.", ROOM_TOOL_APPROVALS.jat_inspect, haulArguments, async (_id, params, signal, _update, ctx) => {
		try {
			if (!isRecord(params) || typeof params.haul !== "string") throw new RoomCliError("invalid-result");
			const result = await controller.inspectJat(ctx.cwd, params.haul, signal);
			return output(`JAT contains ${result.imageCount} image${result.imageCount === 1 ? "" : "s"}.`, { ok: true, image_count: result.imageCount });
		} catch (error) { return errorResult(error); }
	}));
}

export function renderRoomStatus(context: RoomContext, status: RoomStatus | undefined): string | undefined {
	return roomStatusText(context, status);
}

export function snapshotSelectOptions(snapshots: Parameters<typeof snapshotOptions>[0]) {
	return snapshotOptions(snapshots);
}
