export type RoomContext =
	| { kind: "linked"; dimensionId: string; projectId: string; displayName: string; snapshotId: string }
	| { kind: "unlinked" }
	| { kind: "invalid" };

export type RoomStatus =
	| { kind: "known"; state: "clean" | "changed"; dimensionId: string; projectId: string; snapshotId: string }
	| { kind: "unknown" };

export interface SnapshotInfo {
	snapshotId: string;
	createdAt: string;
	ciphertextSize: number;
}

export interface SnapshotList {
	kind: "snapshots";
	projectId: string;
	latestSnapshotId: string;
	snapshots: SnapshotInfo[];
}

export interface SavedReceipt {
	kind: "saved";
	status: "saved" | "saved-but-dirty";
	projectId: string;
	snapshotId: string;
	previousSnapshotId?: string;
	ciphertextSize?: number;
	dataAddedBytes: number;
}

export interface AlreadySavedReceipt {
	kind: "already-saved";
	status: "already-saved";
	projectId: string;
	snapshotId: string;
	dataAddedBytes: 0;
}

export type SaveReceipt = SavedReceipt | AlreadySavedReceipt;

export interface JatInspection {
	kind: "jat-inspection";
	imageCount: number;
}

export interface RoomList {
	kind: "rooms";
	rooms: Array<{ projectId: string; displayName: string }>;
}

export interface DoctorResult {
	kind: "doctor";
	ok: boolean;
	checks: Array<{ name: string; ok: boolean }>;
}

const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const DISPLAY = /^[\p{L}\p{N}\p{M} ._'’()-]{1,80}$/u;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/;

function record(value: unknown): Record<string, unknown> | undefined {
	return isRecord(value) ? value : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function id(value: unknown): value is string {
	return typeof value === "string" && ID.test(value);
}

function display(value: unknown): value is string {
	return typeof value === "string" && DISPLAY.test(value);
}

export function parseContextResult(value: unknown): RoomContext | undefined {
	const body = record(value);
	if (!body || body.format_version !== 1 || typeof body.ok !== "boolean" || typeof body.linked !== "boolean" || typeof body.path_matches !== "boolean") return undefined;
	if (body.state === "unlinked" && body.ok && !body.linked && !body.path_matches) return { kind: "unlinked" };
	if (body.state === "invalid" && !body.ok && !body.linked) return { kind: "invalid" };
	if (
		body.state === "linked" && body.ok && body.linked && body.path_matches &&
		id(body.dimension_id) && id(body.project_id) && display(body.display_name) && id(body.snapshot_id)
	) {
		return {
			kind: "linked",
			dimensionId: body.dimension_id,
			projectId: body.project_id,
			displayName: body.display_name,
			snapshotId: body.snapshot_id,
		};
	}
	return undefined;
}

export function parseStatusResult(value: unknown): RoomStatus | undefined {
	const body = record(value);
	if (!body || typeof body.ok !== "boolean") return undefined;
	if (!id(body.dimension_id) || !id(body.project_id) || !id(body.snapshot_id) || body.path_matches !== true) return undefined;
	const hasV2Status = "fingerprint_matches" in body;
	const hasV3Status = "signature_matches" in body || "policy_matches" in body || "signature_algorithm" in body;
	if (hasV2Status === hasV3Status) return undefined;
	if (hasV2Status) {
		if (typeof body.fingerprint_matches !== "boolean") return undefined;
		if (body.ok === true && body.state === "clean" && body.fingerprint_matches) {
			return { kind: "known", state: "clean", dimensionId: body.dimension_id, projectId: body.project_id, snapshotId: body.snapshot_id };
		}
		if (body.ok === false && body.state === "changed" && !body.fingerprint_matches) {
			return { kind: "known", state: "changed", dimensionId: body.dimension_id, projectId: body.project_id, snapshotId: body.snapshot_id };
		}
		return undefined;
	}
	if (
		typeof body.signature_matches !== "boolean" || typeof body.policy_matches !== "boolean" ||
		body.signature_algorithm !== "josh-room-stat-v1" ||
		!isSha256(body.workspace_path_sha256) || !isSha256(body.workspace_signature) || !isSha256(body.capture_policy_sha256)
	) return undefined;
	if (body.ok === true && body.state === "clean" && body.signature_matches && body.policy_matches) {
		return { kind: "known", state: "clean", dimensionId: body.dimension_id, projectId: body.project_id, snapshotId: body.snapshot_id };
	}
	if (body.ok === false && body.state === "changed" && (!body.signature_matches || !body.policy_matches)) {
		return { kind: "known", state: "changed", dimensionId: body.dimension_id, projectId: body.project_id, snapshotId: body.snapshot_id };
	}
	return undefined;
}

function isSha256(value: unknown): value is string {
	return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

export function parseSnapshotsResult(value: unknown): SnapshotList | undefined {
	const body = record(value);
	if (!body || body.ok !== true || !id(body.project) || !id(body.latest) || !Array.isArray(body.snapshots) || body.snapshots.length > 500) return undefined;
	const snapshots: SnapshotInfo[] = [];
	for (const value of body.snapshots) {
		const snapshot = record(value);
		if (
			!snapshot || !id(snapshot.snapshot_id) || typeof snapshot.created_at !== "string" || !ISO_DATE.test(snapshot.created_at) || !Number.isFinite(Date.parse(snapshot.created_at)) ||
			typeof snapshot.ciphertext_size !== "number" || !Number.isSafeInteger(snapshot.ciphertext_size) || snapshot.ciphertext_size < 0
		) return undefined;
		snapshots.push({ snapshotId: snapshot.snapshot_id, createdAt: snapshot.created_at, ciphertextSize: snapshot.ciphertext_size });
	}
	if (!snapshots.some((snapshot) => snapshot.snapshotId === body.latest)) return undefined;
	snapshots.sort((a, b) => b.createdAt.localeCompare(a.createdAt));
	return { kind: "snapshots", projectId: body.project, latestSnapshotId: body.latest, snapshots };
}

export function parseSaveResult(value: unknown): SaveReceipt | undefined {
	const body = record(value);
	if (!body || body.ok !== true || !id(body.project_id) || !id(body.snapshot_id)) return undefined;
	if (body.previous_snapshot_id !== undefined && !id(body.previous_snapshot_id)) return undefined;
	const ciphertextSize = body.ciphertext_size;
	if (ciphertextSize !== undefined && (typeof ciphertextSize !== "number" || !Number.isSafeInteger(ciphertextSize) || ciphertextSize < 0)) return undefined;
	const rawAddedBytes = body.data_added_bytes;
	if (rawAddedBytes !== undefined && (typeof rawAddedBytes !== "number" || !Number.isSafeInteger(rawAddedBytes) || rawAddedBytes < 0)) return undefined;
	if (body.status === "already-saved") {
		if (rawAddedBytes !== 0) return undefined;
		return { kind: "already-saved", status: "already-saved", projectId: body.project_id, snapshotId: body.snapshot_id, dataAddedBytes: 0 };
	}
	if (body.status !== undefined && body.status !== "saved" && body.status !== "saved-but-dirty") return undefined;
	const dataAddedBytes = rawAddedBytes ?? ciphertextSize;
	if (dataAddedBytes === undefined) return undefined;
	return {
		kind: "saved",
		status: body.status === "saved-but-dirty" ? "saved-but-dirty" : "saved",
		projectId: body.project_id,
		snapshotId: body.snapshot_id,
		...(typeof body.previous_snapshot_id === "string" ? { previousSnapshotId: body.previous_snapshot_id } : {}),
		...(ciphertextSize !== undefined ? { ciphertextSize } : {}),
		dataAddedBytes,
	};
}

export function parseInspectResult(value: unknown): JatInspection | undefined {
	const body = record(value);
	if (!body || body.ok !== true || body.operation !== "inspect" || body.success !== true || body.exit_status !== 0 || !Array.isArray(body.images) || body.images.length > 1000) return undefined;
	let imageCount = 0;
	for (const image of body.images) {
		if (typeof image === "string" && boundedMetadata(image)) imageCount += 1;
		else {
			const item = record(image);
			const name = item?.name ?? item?.reference;
			if (!boundedMetadata(name)) return undefined;
			imageCount += 1;
		}
	}
	return { kind: "jat-inspection", imageCount };
}

function boundedMetadata(value: unknown): value is string {
	return typeof value === "string" && value.length > 0 && value.length <= 4096 && !/[\u0000-\u001f\u007f]/.test(value);
}

export function parseRoomsResult(value: unknown): RoomList | undefined {
	const body = record(value);
	if (!body || body.ok !== true || !Array.isArray(body.projects) || body.projects.length > 500) return undefined;
	const rooms: RoomList["rooms"] = [];
	for (const project of body.projects) {
		const item = record(project);
		if (!item || !id(item.id) || !display(item.display_name)) return undefined;
		rooms.push({ projectId: item.id, displayName: item.display_name });
	}
	return { kind: "rooms", rooms };
}

export function parseDoctorResult(value: unknown): DoctorResult | undefined {
	const body = record(value);
	if (
		!body || body.format_version !== 1 || body.product !== "josh-room" || typeof body.ok !== "boolean" ||
		body.selected_backend !== "local" || body.selected_ide !== "terminal" || !Array.isArray(body.checks) || body.checks.length > 100
	) return undefined;
	const checks: DoctorResult["checks"] = [];
	for (const value of body.checks) {
		const check = record(value);
		if (!check || typeof check.name !== "string" || !/^[a-z][a-z0-9-]{0,63}$/.test(check.name) || typeof check.ok !== "boolean") return undefined;
		checks.push({ name: check.name, ok: check.ok });
	}
	return { kind: "doctor", ok: body.ok, checks };
}

export function roomStatusText(context: RoomContext, status: RoomStatus | undefined): string | undefined {
	if (context.kind !== "linked") return undefined;
	const glyph = status?.kind !== "known" ? "?" : status.state === "clean" ? "✓" : "●";
	return `room:${context.displayName} ${glyph}`;
}

export function snapshotOptions(result: SnapshotList): Array<{ label: string; description: string; value: string }> {
	return [...result.snapshots].sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt)).slice(0, 100).map((snapshot) => ({
		label: snapshot.snapshotId === result.latestSnapshotId ? `✓ ${snapshot.snapshotId}` : snapshot.snapshotId,
		description: `${snapshot.createdAt} · ${formatBytes(snapshot.ciphertextSize)}${snapshot.snapshotId === result.latestSnapshotId ? " · latest" : ""}`,
		value: snapshot.snapshotId,
	}));
}

export function checkpointEntry(context: Extract<RoomContext, { kind: "linked" }>, receipt: SavedReceipt): Record<string, unknown> {
	return {
		format_version: 1,
		dimension_id: context.dimensionId,
		project_id: receipt.projectId,
		...(receipt.previousSnapshotId ? { previous_snapshot_id: receipt.previousSnapshotId } : {}),
		snapshot_id: receipt.snapshotId,
	};
}

export interface SaveOutcomePresentation {
	message: string;
	statusText: string;
	details: Record<string, unknown>;
	checkpointEntry?: Record<string, unknown>;
}

export function presentSaveOutcome(
	context: Extract<RoomContext, { kind: "linked" }>,
	receipt: SaveReceipt,
): SaveOutcomePresentation {
	const alreadySaved = receipt.kind === "already-saved";
	const savedSnapshotId = receipt.snapshotId;
	const statusState = receipt.kind === "saved" && receipt.status === "saved-but-dirty" ? "changed" : "clean";
	const statusContext = alreadySaved ? context : { ...context, snapshotId: savedSnapshotId };
	const message = alreadySaved
		? `Already saved Room ${context.displayName}; 0 bytes added.`
		: receipt.status === "saved-but-dirty"
			? `Saved Room ${context.displayName} → ${savedSnapshotId}; workspace changed during Save; ${receipt.dataAddedBytes} bytes added.`
			: `Saved Room ${context.displayName} → ${savedSnapshotId}; ${receipt.dataAddedBytes} bytes added.`;
	const details: Record<string, unknown> = {
		ok: true,
		status: receipt.status,
		project_id: receipt.projectId,
		snapshot_id: savedSnapshotId,
		data_added_bytes: receipt.dataAddedBytes,
	};
	if (receipt.kind === "saved") {
		if (receipt.previousSnapshotId) details.previous_snapshot_id = receipt.previousSnapshotId;
		if (receipt.ciphertextSize !== undefined) details.ciphertext_size = receipt.ciphertextSize;
	}
	const statusText = roomStatusText(statusContext, {
		kind: "known",
		state: statusState,
		dimensionId: context.dimensionId,
		projectId: receipt.projectId,
		snapshotId: savedSnapshotId,
	}) ?? `room:${context.displayName} ?`;
	return {
		message,
		statusText,
		details,
		...(receipt.kind === "saved" ? { checkpointEntry: checkpointEntry(context, receipt) } : {}),
	};
}

function formatBytes(size: number): string {
	if (size < 1024) return `${size} B`;
	const units = ["KiB", "MiB", "GiB", "TiB"];
	let value = size / 1024;
	let unit = units[0];
	for (let index = 1; value >= 1024 && index < units.length; index += 1) {
		value /= 1024;
		unit = units[index];
	}
	return `${value.toFixed(1)} ${unit}`;
}

export const ROOM_TOOL_APPROVALS = {
	room_context: "read",
	room_status: "read",
	room_snapshots: "read",
	room_save: "write",
	jat_inspect: "read",
} satisfies Record<"room_context" | "room_status" | "room_snapshots" | "room_save" | "jat_inspect", "read" | "write">;
