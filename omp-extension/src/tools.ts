import { ROOM_TOOL_APPROVALS, roomIndicator } from "./contracts.ts";
import type { RoomController } from "./controller.ts";
import type { ExtensionAPI, ToolDefinition } from "@oh-my-pi/pi-coding-agent";
import type { TSchema } from "@oh-my-pi/pi-ai";
import { RoomCliError, SaveOutcomeUnknownError } from "./process.ts";
import { applySaveOutcome, saveFailureMessage, settleFailure, type RoomSessionState } from "./state.ts";
import type { TuiKit, TuiTheme } from "./ui.ts";

interface ToolResult {
	content: Array<{ type: "text"; text: string }>;
	details: Record<string, unknown>;
	isError?: boolean;
}

function output(text: string, details: Record<string, unknown>): ToolResult {
	return { content: [{ type: "text", text }], details };
}

function errorResult(error: unknown): ToolResult {
	const message = error instanceof RoomCliError ? error.message : "Josh Room operation failed. Run `josh-room doctor` for local diagnostics.";
	return { ...output(message, { ok: false, error: error instanceof RoomCliError ? error.kind : "failed" }), isError: true };
}

/** Bounded, single-line summary of a tool result for the native renderer. */
function summary(result: { content?: Array<{ type: string; text?: string }> }): string {
	const text = result.content?.find((item) => item.type === "text")?.text ?? "";
	const line = text.replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
	return line.length > 160 ? `${line.slice(0, 159)}…` : line;
}

/** Presentation only: success, failure, cancellation and partial progress render distinctly; `details` stays the contract. */
export function renderRoomResult(
	kit: TuiKit,
	result: { content?: Array<{ type: string; text?: string }>; details?: unknown; isError?: boolean },
	options: { expanded?: boolean; isPartial?: boolean },
	theme: TuiTheme,
) {
	const text = summary(result);
	if (options.isPartial) return new kit.Text(theme.fg("muted", `… ${text || "Working"}`), 0, 0);
	const details = typeof result.details === "object" && result.details !== null ? result.details as Record<string, unknown> : {};
	if (details.ok !== true) {
		if (details.error === "cancelled") return new kit.Text(theme.fg("warning", `⊘ ${text}`), 0, 0);
		if (result.isError || typeof details.error === "string") return new kit.Text(theme.fg("error", `✗ ${text}`), 0, 0);
		return new kit.Text(theme.fg("warning", `○ ${text}`), 0, 0);
	}
	const lines = [`${theme.fg("success", "✓")} ${text}`];
	if (options.expanded && Array.isArray(details.snapshots)) {
		for (const row of details.snapshots.slice(0, 10) as Array<{ snapshot_id?: unknown; created_at?: unknown }>) {
			lines.push(theme.fg("dim", `  ${String(row.snapshot_id)} · ${String(row.created_at)}`));
		}
	}
	return new kit.Text(lines.join("\n"), 0, 0);
}

function tool<TParams extends TSchema>(
	kit: TuiKit | undefined,
	name: keyof typeof ROOM_TOOL_APPROVALS,
	label: string,
	description: string,
	parameters: TParams,
	execute: ToolDefinition<TParams>["execute"],
): ToolDefinition<TParams> {
	return {
		name,
		label,
		description,
		approval: ROOM_TOOL_APPROVALS[name],
		// Discoverable: does not bloat every prompt and never edits the session's active-tool set.
		loadMode: "discoverable",
		parameters,
		execute,
		...(kit ? {
			renderCall: (_args, _options, theme: TuiTheme) => new kit.Text(theme.fg("toolTitle", theme.bold(label)), 0, 0),
			renderResult: (result, options, theme: TuiTheme) => renderRoomResult(kit, result, options, theme),
		} : {}),
	} as ToolDefinition<TParams>;
}

export function registerRoomTools(api: ExtensionAPI, controller: RoomController, state: RoomSessionState, kit?: TuiKit): void {
	const z = api.zod;
	const noArguments = z.object({}).strict();
	const haulArguments = z.object({ haul: z.string().min(1).max(4096) }).strict();
	api.registerTool(tool(kit, "room_context", "Room Context", "Show the validated local Josh Room binding (cheap, local, no network).", noArguments, async (_id, _params, signal, _update, ctx) => {
		try {
			const context = await controller.context(ctx.cwd, signal);
			if (!context || context.kind !== "linked") return output("No valid Josh Room binding is available in this workspace.", { ok: false, state: context?.kind ?? "unavailable" });
			const checkpoint = state.room?.projectId === context.projectId ? state.provenance : undefined;
			return output(`Room ${context.displayName} · ${context.projectId} · latest ${context.snapshotId}`, {
				ok: true, state: "linked", dimension_id: context.dimensionId, project_id: context.projectId, snapshot_id: context.snapshotId,
				...(checkpoint ? { session_checkpoint_snapshot_id: checkpoint.snapshot_id } : {}),
			});
		} catch (error) { return errorResult(error); }
	}));
	api.registerTool(tool(kit, "room_status", "Room Status", "Read authoritative Josh Room workspace status (clean or changed).", noArguments, async (_id, _params, signal, _update, ctx) => {
		try {
			const { context, status } = await controller.status(ctx.cwd, signal);
			state.show(ctx, context, roomIndicator(status));
			const stateName = status.kind === "known" ? status.state : "unknown";
			return output(`Room ${context.displayName} · ${stateName} · ${status.kind === "known" ? status.snapshotId : context.snapshotId}`, { ok: true, state: stateName, project_id: context.projectId, snapshot_id: status.kind === "known" ? status.snapshotId : context.snapshotId });
		} catch (error) {
			settleFailure(state, ctx, error);
			return errorResult(error);
		}
	}));
	api.registerTool(tool(kit, "room_snapshots", "Room Snapshots", "List saved snapshots for the current Room, newest first (read-only).", noArguments, async (_id, _params, signal, _update, ctx) => {
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
	api.registerTool(tool(kit, "room_save", "Save Room Checkpoint", "Save the current Josh Room workspace as an explicit checkpoint. Only call when the user asks to save.", noArguments, async (_id, _params, signal, onUpdate, ctx) => {
		try {
			const { context, receipt } = await controller.save(ctx.cwd, signal, (message) => onUpdate?.({ content: [{ type: "text", text: message }], details: { ok: false, state: "running" } }));
			const outcome = applySaveOutcome(api, state, ctx, context, receipt);
			return output(outcome.message, outcome.details);
		} catch (error) {
			settleFailure(state, ctx, error);
			const result = errorResult(error);
			result.content[0].text = saveFailureMessage(error, result.content[0].text);
			if (error instanceof SaveOutcomeUnknownError) result.details.save_outcome = "unknown";
			return result;
		}
	}));
	api.registerTool(tool(kit, "jat_inspect", "Inspect JAT", "Inspect an explicit absolute local JAT haul path through Josh Room.", haulArguments, async (_id, params, signal, _update, ctx) => {
		try {
			const haul = typeof params === "object" && params !== null && "haul" in params ? params.haul : undefined;
			if (typeof haul !== "string") throw new RoomCliError("invalid-result");
			const result = await controller.inspectJat(ctx.cwd, haul, signal);
			return output(`JAT contains ${result.imageCount} image${result.imageCount === 1 ? "" : "s"}.`, { ok: true, image_count: result.imageCount });
		} catch (error) { return errorResult(error); }
	}));
}
