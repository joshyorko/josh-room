import { checkpointEntry, roomStatusText, snapshotOptions } from "./contracts.ts";
import type { RoomController } from "./controller.ts";
import type { ExtensionAPI } from "@oh-my-pi/pi-coding-agent";
import { RoomCliError } from "./process.ts";

interface CommandContext {
	cwd: string;
	hasUI: boolean;
	ui: {
		select(title: string, options: Array<string | { label: string; description?: string }>, options2?: { signal?: AbortSignal }): Promise<string | undefined>;
		confirm(title: string, message: string, options2?: { signal?: AbortSignal }): Promise<boolean>;
		notify(message: string, type?: "info" | "warning" | "error"): void;
		setStatus(key: string, text: string | undefined): void;
		setWorkingMessage(message?: string): void;
	};
}

export type RoomAction = "status" | "snapshots" | "save" | "inspect" | "rooms" | "doctor";
export type ParsedRoomCommand = { kind: "menu" } | { kind: "action"; action: RoomAction; rest: string[] };

export function parseRoomCommand(args: string): ParsedRoomCommand | undefined {
	const parts = args.trim().split(/\s+/).filter(Boolean);
	const command = parts[0];
	if (command === undefined) return { kind: "menu" };
	switch (command) {
		case "status":
		case "snapshots":
		case "save":
		case "inspect":
		case "rooms":
		case "doctor":
			return { kind: "action", action: command, rest: parts.slice(1) };
		default:
			return undefined;
	}
}

function errorMessage(error: unknown): string {
	return error instanceof RoomCliError ? error.message : "Josh Room operation failed. Run `josh-room doctor` for local diagnostics.";
}

function completion(prefix: string): string[] {
	const values = ["status", "snapshots", "save", "inspect", "rooms", "doctor"];
	return values.filter((value) => value.startsWith(prefix.trim().toLowerCase()));
}

export function registerRoomCommand(api: ExtensionAPI, controller: RoomController): void {
	api.registerCommand("room", {
		description: "Browse and checkpoint the current Josh Room",
		getArgumentCompletions: completion,
		handler: async (args, ctx) => {
			const parsed = parseRoomCommand(args);
			if (!parsed) {
				ctx.ui.notify("Use /room status, snapshots, save, inspect, rooms, or doctor.", "info");
				return;
			}
			let action: RoomAction;
			if (parsed.kind === "menu") {
				const selected = await ctx.ui.select("Josh Room", [
					{ label: "Status", description: "Current binding and clean/changed state" },
					{ label: "Snapshots", description: "Browse saved checkpoints" },
					{ label: "Save checkpoint", description: "Save this Room explicitly" },
					{ label: "Inspect", description: "Show validated Room identity" },
					{ label: "Rooms", description: "Browse Rooms in this Dimension" },
					{ label: "Doctor", description: "Run local Josh Room diagnostics" },
				]);
				if (!selected) return;
				const actions: Record<string, RoomAction> = {
					Status: "status", Snapshots: "snapshots", "Save checkpoint": "save", Inspect: "inspect", Rooms: "rooms", Doctor: "doctor",
				};
				const chosen = actions[selected];
				if (!chosen) return;
				action = chosen;
			} else action = parsed.action;
			try {
				if (action === "status") {
					const { context, status } = await controller.status(ctx.cwd);
					ctx.ui.setStatus("josh-room", roomStatusText(context, status));
					ctx.ui.notify(`Room ${context.displayName} · ${status.kind === "known" ? status.state : "unknown"} · ${status.kind === "known" ? status.snapshotId : context.snapshotId}`, "info");
					return;
				}
				if (action === "inspect") {
					const context = await controller.context(ctx.cwd);
					if (!context || context.kind !== "linked") {
						ctx.ui.notify(context?.kind === "invalid" ? "The Josh Room binding is invalid; mutations are blocked." : "This workspace is not a validated Josh Room.", context?.kind === "invalid" ? "warning" : "info");
						return;
					}
					ctx.ui.notify(`Room ${context.displayName} · ${context.projectId} · latest ${context.snapshotId}`, "info");
					return;
				}
				if (action === "snapshots") {
					const { snapshots } = await controller.snapshots(ctx.cwd);
					const options = snapshotOptions(snapshots);
					const selected = await ctx.ui.select("Room snapshots", options.map(({ label, description }) => ({ label, description })));
					if (selected) ctx.ui.notify(`Snapshot ${selected}`, "info");
					return;
				}
				if (action === "rooms") {
					const result = await controller.rooms(ctx.cwd);
					const selected = await ctx.ui.select("Rooms", result.rooms.map((room) => ({ label: room.displayName, description: room.projectId })));
					if (selected) ctx.ui.notify(`Room ${selected}`, "info");
					return;
				}
				if (action === "save") {
					if (parsed.kind === "action" && parsed.rest.length > 0) {
						ctx.ui.notify("The current Josh Room Save contract does not accept a note.", "warning");
						return;
					}
					if (ctx.hasUI && !await ctx.ui.confirm("Save Room checkpoint", "Save the current workspace to the configured Dimension?")) return;
					ctx.ui.setWorkingMessage("Saving Josh Room checkpoint…");
					try {
						const { context, receipt } = await controller.save(ctx.cwd);
						api.appendEntry("com.joshyorko.josh-room.checkpoint.v1", checkpointEntry(context, receipt));
						ctx.ui.setStatus("josh-room", `room:${context.displayName} ✓`);
						ctx.ui.notify(`Saved Room ${context.displayName} → ${receipt.snapshotId}`, "info");
					} finally {
						ctx.ui.setWorkingMessage();
					}
					return;
				}
				if (action === "doctor") {
					const report = await controller.doctor(ctx.cwd);
					const passed = report.checks.filter((check) => check.ok).length;
					const failed = report.checks.length - passed;
					ctx.ui.notify(`Josh Room local checks: ${passed} passed, ${failed} need attention.`, failed ? "warning" : "info");
					return;
				}
			} catch (error) {
				ctx.ui.notify(errorMessage(error), "error");
			}
		},
	});
	api.registerCommand("jat", {
		description: "Inspect an explicit local JAT haul",
		getArgumentCompletions: (prefix) => prefix.startsWith("inspect ") ? [] : ["inspect"],
		handler: async (args, ctx) => {
			const match = /^inspect\s+(.+)$/.exec(args.trim());
			if (!match) {
				ctx.ui.notify("Use /jat inspect <absolute-local-haul-path>.", "info");
				return;
			}
			try {
				const result = await controller.inspectJat(ctx.cwd, match[1]);
				ctx.ui.notify(`JAT contains ${result.imageCount} image${result.imageCount === 1 ? "" : "s"}.`, "info");
			} catch (error) {
				ctx.ui.notify(errorMessage(error), "error");
			}
		},
	});
}
