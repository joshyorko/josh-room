import {
	formatRoomStatus,
	id,
	isRecord,
	presentSaveOutcome,
	type RoomContext,
	type RoomIndicator,
	type RoomStatusTheme,
	type SaveOutcomePresentation,
	type SaveReceipt,
} from "./contracts.ts";
import { RoomCliError, SaveOutcomeUnknownError } from "./process.ts";

export const STATUS_KEY = "josh-room";
export const PROVENANCE_TYPE = "com.joshyorko.josh-room.checkpoint.v1";

type LinkedRoom = Extract<RoomContext, { kind: "linked" }>;

export interface Provenance {
	format_version: 1;
	dimension_id: string;
	project_id: string;
	previous_snapshot_id?: string;
	snapshot_id: string;
}

/** The subset of the OMP handler context that Room presentation state touches. */
export interface StatusHost {
	mode?: string;
	ui: { setStatus(key: string, text: string | undefined): void; theme?: RoomStatusTheme };
}

interface BranchHost {
	sessionManager: { getBranch(): Iterable<{ type: string; customType?: string; data?: unknown }> };
}

/** Read the newest namespaced checkpoint entry on the current branch. Convenience metadata only. */
export function readProvenance(ctx: BranchHost): Provenance | undefined {
	const entry = [...ctx.sessionManager.getBranch()].reverse().find((item) => item.type === "custom" && item.customType === PROVENANCE_TYPE);
	if (!entry) return undefined;
	const data = entry.data;
	if (
		!isRecord(data) || data.format_version !== 1 || !id(data.dimension_id) || !id(data.project_id) || !id(data.snapshot_id) ||
		(data.previous_snapshot_id !== undefined && !id(data.previous_snapshot_id))
	) return undefined;
	return {
		format_version: 1,
		dimension_id: data.dimension_id,
		project_id: data.project_id,
		...(typeof data.previous_snapshot_id === "string" ? { previous_snapshot_id: data.previous_snapshot_id } : {}),
		snapshot_id: data.snapshot_id,
	};
}

/**
 * Per-session presentation state for the Room indicator. It is a hint only: every mutation re-validates
 * through Josh Room, and session provenance is never consulted to authorize an operation.
 */
export class RoomSessionState {
	#room: LinkedRoom | undefined;
	#indicator: RoomIndicator = "unknown";
	#shown = false;
	#provenance: Provenance | undefined;

	get room(): LinkedRoom | undefined {
		return this.#room;
	}

	get indicator(): RoomIndicator {
		return this.#indicator;
	}

	/** Provenance for the current Room only; a checkpoint from another Room or Dimension is ignored. */
	get provenance(): Provenance | undefined {
		const room = this.#room;
		const provenance = this.#provenance;
		if (!room || !provenance || provenance.dimension_id !== room.dimensionId || provenance.project_id !== room.projectId) return undefined;
		return provenance;
	}

	restoreProvenance(ctx: BranchHost): void {
		this.#provenance = readProvenance(ctx);
	}

	recordCheckpoint(entry: Record<string, unknown>): void {
		this.#provenance = readProvenance({ sessionManager: { getBranch: () => [{ type: "custom", customType: PROVENANCE_TYPE, data: entry }] } });
	}

	show(ctx: StatusHost, room: LinkedRoom, indicator: RoomIndicator): void {
		this.#room = room;
		this.#indicator = indicator;
		this.#publish(ctx);
	}

	/** Update the indicator for the Room already on display; no-op outside a validated Room. */
	mark(ctx: StatusHost, indicator: RoomIndicator): void {
		if (!this.#room || this.#indicator === indicator) return;
		this.#indicator = indicator;
		this.#publish(ctx);
	}

	clear(ctx: StatusHost): void {
		this.#room = undefined;
		this.#indicator = "unknown";
		if (!this.#shown) return;
		ctx.ui.setStatus(STATUS_KEY, undefined);
		this.#shown = false;
	}

	#publish(ctx: StatusHost): void {
		if (!this.#room) return;
		// Styled text only for the interactive TUI; RPC/ACP clients receive plain text instead of escapes.
		const theme = ctx.mode === "tui" ? ctx.ui.theme : undefined;
		ctx.ui.setStatus(STATUS_KEY, formatRoomStatus(this.#room.displayName, this.#indicator, theme));
		this.#shown = true;
	}
}

/** Shared by `/room save` and `room_save`: status and provenance come only from the real receipt. */
export function applySaveOutcome(
	api: { appendEntry(customType: string, data: Record<string, unknown>): void },
	state: RoomSessionState,
	ctx: StatusHost,
	context: LinkedRoom,
	receipt: SaveReceipt,
): SaveOutcomePresentation {
	const outcome = presentSaveOutcome(context, receipt);
	if (outcome.checkpointEntry) {
		try {
			api.appendEntry(PROVENANCE_TYPE, outcome.checkpointEntry);
		} catch (error) {
			throw new SaveOutcomeUnknownError(error);
		}
		state.recordCheckpoint(outcome.checkpointEntry);
	}
	state.show(ctx, outcome.room, outcome.indicator);
	return outcome;
}

/** Settle the indicator after a failed Room operation; never claims a state Josh Room did not report. */
export function settleFailure(state: RoomSessionState, ctx: StatusHost, error: unknown): void {
	if (!(error instanceof RoomCliError)) return;
	if (error.kind === "not-linked") state.clear(ctx);
	else if (error.kind === "invalid-result") state.mark(ctx, "blocked");
	else if (error instanceof SaveOutcomeUnknownError) state.mark(ctx, "unknown");
}

/** Shared `/room save` and `room_save` failure wording; `detail` is the bounded, path-free error message. */
export function saveFailureMessage(error: unknown, detail: string): string {
	if (error instanceof SaveOutcomeUnknownError) return `Room save outcome unknown — a checkpoint may have been created; run /room status (or room_status) before relying on the previous snapshot. ${detail}`;
	if (error instanceof RoomCliError && error.kind === "cancelled") return "Room save cancelled — previous snapshot remains authoritative.";
	return `Room save failed — previous snapshot remains authoritative. ${detail}`;
}
