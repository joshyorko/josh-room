import { roomIndicator, snapshotOptions } from "./contracts.ts";
import type { RoomController } from "./controller.ts";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { RoomCliError, SaveOutcomeUnknownError } from "./process.ts";
import { applySaveOutcome, saveFailureMessage, settleFailure, type RoomSessionState, type StatusHost } from "./state.ts";
import { pick, withProgress, type PickerItem, type TuiKit, type UiHost } from "./ui.ts";

type CommandContext = UiHost & StatusHost & {
	cwd: string;
	ui: UiHost["ui"] & StatusHost["ui"] & { confirm(title: string, message: string): Promise<boolean> };
};

export type RoomAction = "status" | "snapshots" | "save" | "inspect" | "rooms" | "doctor";
export type ParsedRoomCommand = { kind: "menu" } | { kind: "action"; action: RoomAction; rest: string[] };

/** Action hub rows; also the source of `/room` argument completion. */
export const ROOM_ACTIONS: ReadonlyArray<{ action: RoomAction; label: string; description: string }> = [
	{ action: "status", label: "Status", description: "Current binding and clean/changed state" },
	{ action: "snapshots", label: "Snapshots", description: "Browse this Room's saved checkpoints" },
	{ action: "save", label: "Save checkpoint", description: "Save the current Room explicitly" },
	{ action: "inspect", label: "Inspect", description: "Show validated Room identity and session checkpoint" },
	{ action: "rooms", label: "Rooms", description: "Browse Rooms in this Dimension (read-only)" },
	{ action: "doctor", label: "Doctor", description: "Diagnose the local Josh Room runtime" },
];

export function parseRoomCommand(args: string): ParsedRoomCommand | undefined {
	const parts = args.trim().split(/\s+/).filter(Boolean);
	const command = parts[0];
	if (command === undefined) return { kind: "menu" };
	const known = ROOM_ACTIONS.find((item) => item.action === command);
	return known ? { kind: "action", action: known.action, rest: parts.slice(1) } : undefined;
}

/** OMP autocomplete items for the first `/room` argument; `null` once a subcommand is complete. */
export function roomArgumentCompletions(argumentPrefix: string): Array<{ value: string; label: string; description: string }> | null {
	const prefix = argumentPrefix.trimStart();
	if (/\s/.test(prefix)) return null;
	const items = ROOM_ACTIONS
		.filter((item) => item.action.startsWith(prefix.toLowerCase()))
		.map((item) => ({ value: item.action, label: item.action, description: item.description }));
	return items.length > 0 ? items : null;
}

/** `/jat` offers only `inspect`; the haul path is the user's explicit local file. */
export function jatArgumentCompletions(argumentPrefix: string): Array<{ value: string; label: string; description: string }> | null {
	const prefix = argumentPrefix.trimStart();
	if (/\s/.test(prefix) || !"inspect".startsWith(prefix.toLowerCase())) return null;
	return [{ value: "inspect ", label: "inspect", description: "Inspect an explicit local JAT haul" }];
}

function errorMessage(error: unknown): string {
	return error instanceof RoomCliError ? error.message : "Josh Room operation failed. Run `josh-room doctor` for local diagnostics.";
}

async function runRoomAction(action: RoomAction, rest: string[], api: ExtensionAPI, controller: RoomController, state: RoomSessionState, kit: TuiKit | undefined, ctx: CommandContext): Promise<void> {
	switch (action) {
		case "status": {
			const { context, status } = await controller.status(ctx.cwd);
			state.show(ctx, context, roomIndicator(status));
			ctx.ui.notify(`Room ${context.displayName} · ${status.kind === "known" ? status.state : "unknown"} · ${status.kind === "known" ? status.snapshotId : context.snapshotId}`, "info");
			return;
		}
		case "inspect": {
			const context = await controller.context(ctx.cwd);
			if (!context || context.kind !== "linked") {
				state.clear(ctx);
				ctx.ui.notify(context?.kind === "invalid" ? "The Josh Room binding is invalid; mutations are blocked." : "This workspace is not a validated Josh Room.", context?.kind === "invalid" ? "warning" : "info");
				return;
			}
			if (state.room?.snapshotId !== context.snapshotId) state.show(ctx, context, "unknown");
			const checkpoint = state.provenance;
			const session = checkpoint ? ` · session checkpoint ${checkpoint.snapshot_id} (provenance only)` : "";
			ctx.ui.notify(`Room ${context.displayName} · ${context.projectId} · Dimension ${context.dimensionId} · latest ${context.snapshotId}${session}`, "info");
			return;
		}
		case "snapshots": {
			const { snapshots } = await controller.snapshots(ctx.cwd);
			const rows = snapshotOptions(snapshots);
			if (rows.length === 0) {
				ctx.ui.notify("This Room has no saved snapshots.", "info");
				return;
			}
			const selected = await pick(ctx, kit, `Room snapshots · ${snapshots.snapshots.length} saved`, rows);
			const row = rows.find((item) => item.value === selected);
			if (row) ctx.ui.notify(row.detail, "info");
			return;
		}
		case "rooms": {
			const { context, rooms } = await controller.rooms(ctx.cwd);
			if (rooms.rooms.length === 0) {
				ctx.ui.notify("This Dimension has no Rooms.", "info");
				return;
			}
			const items: PickerItem[] = rooms.rooms.map((room) => ({
				value: room.projectId,
				label: room.projectId === context.projectId ? `● ${room.displayName}` : `  ${room.displayName}`,
				description: room.projectId === context.projectId ? `${room.projectId} · this workspace` : room.projectId,
			}));
			const selected = await pick(ctx, kit, `Rooms · Dimension ${context.dimensionId}`, items);
			const room = rooms.rooms.find((item) => item.projectId === selected);
			if (room) ctx.ui.notify(`Room ${room.displayName} · ${room.projectId}${room.projectId === context.projectId ? " · this workspace" : ""} · read-only`, "info");
			return;
		}
		case "save": {
			if (rest.length > 0) {
				ctx.ui.notify("The current Josh Room Save contract does not accept a note; run /room save without arguments.", "warning");
				return;
			}
			if (ctx.hasUI && !await ctx.ui.confirm("Save Room checkpoint", "Save the current workspace to its Josh Room Dimension?")) return;
			const name = state.room?.displayName;
			try {
				const { context, receipt } = await withProgress(ctx, kit, name ? `Saving Room ${name}…` : "Saving Josh Room checkpoint…", (signal) => controller.save(ctx.cwd, signal));
				ctx.ui.notify(applySaveOutcome(api, state, ctx, context, receipt).message, "info");
			} catch (error) {
				settleFailure(state, ctx, error);
				const warning = error instanceof SaveOutcomeUnknownError || (error instanceof RoomCliError && error.kind === "cancelled");
				ctx.ui.notify(saveFailureMessage(error, errorMessage(error)), warning ? "warning" : "error");
			}
			return;
		}
		case "doctor": {
			const report = await controller.doctor(ctx.cwd);
			const failing = report.checks.filter((check) => !check.ok).map((check) => check.name);
			const passed = report.checks.length - failing.length;
			const detail = failing.length ? ` (${failing.slice(0, 5).join(", ")}${failing.length > 5 ? ", …" : ""})` : "";
			ctx.ui.notify(`Josh Room local checks: ${passed} passed, ${failing.length} need attention${detail}.`, failing.length ? "warning" : "info");
			return;
		}
	}
}

export function registerRoomCommand(api: ExtensionAPI, controller: RoomController, state: RoomSessionState, kit?: TuiKit): void {
	api.registerCommand("room", {
		description: "Browse and checkpoint the current Josh Room",
		getArgumentCompletions: roomArgumentCompletions,
		handler: async (args, rawContext) => {
			const ctx = rawContext as unknown as CommandContext;
			const parsed = parseRoomCommand(args);
			if (!parsed) {
				ctx.ui.notify("Use /room status, snapshots, save, inspect, rooms, or doctor.", "info");
				return;
			}
			let action: RoomAction;
			if (parsed.kind === "menu") {
				const selected = await pick(ctx, kit, "Josh Room", ROOM_ACTIONS.map((item) => ({ value: item.action, label: item.label, description: item.description })));
				const chosen = ROOM_ACTIONS.find((item) => item.action === selected);
				if (!chosen) return;
				action = chosen.action;
			} else action = parsed.action;
			try {
				await runRoomAction(action, parsed.kind === "action" ? parsed.rest : [], api, controller, state, kit, ctx);
			} catch (error) {
				settleFailure(state, ctx, error);
				ctx.ui.notify(errorMessage(error), "error");
			}
		},
	});
	api.registerCommand("jat", {
		description: "Inspect an explicit local JAT haul",
		getArgumentCompletions: jatArgumentCompletions,
		handler: async (args, rawContext) => {
			const ctx = rawContext as unknown as CommandContext;
			const match = /^inspect\s+(.+)$/.exec(args.trim());
			if (!match) {
				ctx.ui.notify("Use /jat inspect <absolute-local-haul-path>.", "info");
				return;
			}
			try {
				const result = await withProgress(ctx, kit, "Inspecting JAT haul…", (signal) => controller.inspectJat(ctx.cwd, match[1], signal));
				ctx.ui.notify(`JAT contains ${result.imageCount} image${result.imageCount === 1 ? "" : "s"}.`, "info");
			} catch (error) {
				ctx.ui.notify(errorMessage(error), "error");
			}
		},
	});
}
