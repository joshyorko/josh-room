import test from "node:test";
import assert from "node:assert/strict";
import {
	parseContextResult,
	parseStatusResult,
	parseSnapshotsResult,
	parseSaveResult,
	parseInspectResult,
	parseRoomsResult,
	parseDoctorResult,
	roomStatusText,
	snapshotOptions,
	checkpointEntry,
	ROOM_TOOL_APPROVALS,
} from "../src/contracts.ts";

test("context accepts only the v1 bounded Room identity contract", () => {
	assert.deepEqual(
		parseContextResult({
			format_version: 1,
			ok: true,
			state: "linked",
			linked: true,
			path_matches: true,
			dimension_id: "local",
			project_id: "demo-room",
			display_name: "Demo Room",
			snapshot_id: "jat-a1b2c3",
			workspace: "/must/not/escape",
		}),
		{
			kind: "linked",
			dimensionId: "local",
			projectId: "demo-room",
			displayName: "Demo Room",
			snapshotId: "jat-a1b2c3",
		},
	);
	assert.deepEqual(parseContextResult({ format_version: 1, ok: true, state: "unlinked", linked: false, path_matches: false }), { kind: "unlinked" });
	assert.equal(parseContextResult({ format_version: 2, ok: true, state: "linked", linked: true, path_matches: true }), undefined);
	assert.equal(parseContextResult({ format_version: 1, ok: true, state: "linked", linked: true, path_matches: false }), undefined);
	assert.equal(parseContextResult({ format_version: 1, ok: true, state: "linked", linked: true, path_matches: true, project_id: "../private" }), undefined);
});

test("status and snapshots keep only bounded public fields", () => {
	assert.deepEqual(parseStatusResult({ ok: true, state: "clean", path_matches: true, project_id: "demo-room", snapshot_id: "jat-a1b2c3", workspace: "/secret", workspace_fingerprint: "sensitive" }), {
		kind: "known",
		state: "clean",
		projectId: "demo-room",
		snapshotId: "jat-a1b2c3",
	});
	assert.equal(parseStatusResult({ ok: true, state: "future-state" }), undefined);
	const result = parseSnapshotsResult({
		ok: true,
		project: "demo-room",
		latest: "jat-new",
		snapshots: [
			{ snapshot_id: "jat-old", created_at: "2026-01-01T00:00:00Z", ciphertext_size: 10, object_key: "private/key" },
			{ snapshot_id: "jat-new", created_at: "2026-02-01T00:00:00Z", ciphertext_size: 20, bucket: "private" },
		],
	});
	assert.deepEqual(result, {
		kind: "snapshots",
		projectId: "demo-room",
		latestSnapshotId: "jat-new",
		snapshots: [
			{ snapshotId: "jat-new", createdAt: "2026-02-01T00:00:00Z", ciphertextSize: 20 },
			{ snapshotId: "jat-old", createdAt: "2026-01-01T00:00:00Z", ciphertextSize: 10 },
		],
	});
});

test("save, JAT inspection and session provenance omit paths and secrets", () => {
	assert.deepEqual(parseSaveResult({ ok: true, project_id: "demo-room", snapshot_id: "jat-new", previous_snapshot_id: "jat-old", ciphertext_size: 4, source: "/private/workspace" }), {
		kind: "saved", projectId: "demo-room", snapshotId: "jat-new", previousSnapshotId: "jat-old", ciphertextSize: 4,
	});
	assert.deepEqual(parseInspectResult({ ok: true, success: true, operation: "inspect", exit_status: 0, images: [{ name: "private.registry/team/image" }], haul: "/private/file.tar" }), {
		kind: "jat-inspection", imageCount: 1,
	});
	assert.equal(parseInspectResult({ ok: true, success: true, operation: "inspect", exit_status: 0, images: "future-shape" }), undefined);
	assert.equal(parseInspectResult({ ok: true, success: true, operation: "inspect", exit_status: 0, images: [{}] }), undefined);
	assert.deepEqual(parseRoomsResult({ ok: true, projects: [{ id: "demo-room", display_name: "Demo Room", private_endpoint: "https://secret.invalid" }] }), { kind: "rooms", rooms: [{ projectId: "demo-room", displayName: "Demo Room" }] });
	assert.deepEqual(parseDoctorResult({ format_version: 1, product: "josh-room", ok: false, selected_backend: "local", selected_ide: "terminal", checks: [{ name: "age", ok: false, remediation: "/private/path" }] }), { kind: "doctor", ok: false, checks: [{ name: "age", ok: false }] });
	assert.equal(parseDoctorResult({ format_version: 2, product: "josh-room", ok: true, selected_backend: "local", selected_ide: "terminal", checks: [] }), undefined);
	assert.deepEqual(checkpointEntry({ kind: "linked", dimensionId: "local", projectId: "demo-room", displayName: "Demo Room", snapshotId: "jat-old" }, { kind: "saved", projectId: "demo-room", snapshotId: "jat-new", previousSnapshotId: "jat-old", ciphertextSize: 4 }), {
		format_version: 1, dimension_id: "local", project_id: "demo-room", previous_snapshot_id: "jat-old", snapshot_id: "jat-new",
	});
});

test("status glyph and theme stay presentation-only", () => {
	assert.equal(roomStatusText({ kind: "linked", dimensionId: "local", projectId: "demo-room", displayName: "Demo Room", snapshotId: "jat-x" }, { kind: "known", state: "clean", projectId: "demo-room", snapshotId: "jat-x" }), "room:Demo Room ✓");
	assert.equal(roomStatusText({ kind: "linked", dimensionId: "local", projectId: "demo-room", displayName: "Demo Room", snapshotId: "jat-x" }, { kind: "known", state: "changed", projectId: "demo-room", snapshotId: "jat-x" }), "room:Demo Room ●");
	assert.equal(roomStatusText({ kind: "linked", dimensionId: "local", projectId: "demo-room", displayName: "Demo Room", snapshotId: "jat-x" }, undefined), "room:Demo Room ?");
	assert.equal(roomStatusText({ kind: "invalid" }, undefined), undefined);
});

test("snapshot rows sort newest first and use only validated fields", () => {
	const rows = snapshotOptions({
		kind: "snapshots", projectId: "demo-room", latestSnapshotId: "jat-new",
		snapshots: [
			{ snapshotId: "jat-old", createdAt: "2026-01-01T00:00:00Z", ciphertextSize: 3 },
			{ snapshotId: "jat-new", createdAt: "2026-02-01T00:00:00Z", ciphertextSize: 4 },
		],
	});
	assert.equal(rows[0].value, "jat-new");
	assert.match(rows[0].description, /latest/);
});

test("tool definitions use additive native approval tiers", () => {
	assert.deepEqual(ROOM_TOOL_APPROVALS, {
		room_context: "read",
		room_status: "read",
		room_snapshots: "read",
		room_save: "write",
		jat_inspect: "read",
	});
});
