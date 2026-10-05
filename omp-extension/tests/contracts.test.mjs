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
	presentSaveOutcome,
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

test("status accepts the authoritative path-bound changed receipt and filters private fields", () => {
	assert.deepEqual(parseStatusResult({ ok: true, state: "clean", path_matches: true, fingerprint_matches: true, project_id: "demo-room", snapshot_id: "jat-a1b2c3", workspace: "/tmp/synthetic-workspace", workspace_fingerprint: "a".repeat(64) }), {
		kind: "known",
		state: "clean",
		projectId: "demo-room",
		snapshotId: "jat-a1b2c3",
	});
	assert.deepEqual(parseStatusResult({
		ok: false,
		state: "changed",
		path_matches: true,
		fingerprint_matches: false,
		dimension_id: "synthetic",
		project_id: "smoke-room",
		snapshot_id: "jat-old",
		workspace: "/tmp/synthetic-workspace",
		workspace_path_sha256: "b".repeat(64),
		workspace_fingerprint: "c".repeat(64),
	}), {
		kind: "known",
		state: "changed",
		projectId: "smoke-room",
		snapshotId: "jat-old",
	});
	assert.deepEqual(parseStatusResult({ ok: false, state: "unlinked" }), undefined);
	assert.equal(parseStatusResult({ ok: false, state: "changed", path_matches: false, fingerprint_matches: false, project_id: "demo-room", snapshot_id: "jat-a1b2c3" }), undefined);
	assert.equal(parseStatusResult({ ok: false, state: "changed", path_matches: true, fingerprint_matches: true, project_id: "demo-room", snapshot_id: "jat-a1b2c3" }), undefined);
	assert.equal(parseStatusResult({ ok: true, state: "changed", path_matches: true, fingerprint_matches: false, project_id: "demo-room", snapshot_id: "jat-a1b2c3" }), undefined);
	assert.equal(parseStatusResult({ ok: false, state: "future-state", path_matches: true, fingerprint_matches: false, project_id: "demo-room", snapshot_id: "jat-a1b2c3" }), undefined);
	assert.equal(parseStatusResult({ ok: false, state: "changed", path_matches: true, fingerprint_matches: false, project_id: "../private", snapshot_id: "jat-a1b2c3" }), undefined);
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
		kind: "saved", status: "saved", projectId: "demo-room", snapshotId: "jat-new", previousSnapshotId: "jat-old", ciphertextSize: 4, dataAddedBytes: 4,
	});
	assert.deepEqual(parseInspectResult({ ok: true, success: true, operation: "inspect", exit_status: 0, images: [{ name: "private.registry/team/image" }], haul: "/private/file.tar" }), {
		kind: "jat-inspection", imageCount: 1,
	});
	assert.equal(parseInspectResult({ ok: true, success: true, operation: "inspect", exit_status: 0, images: "future-shape" }), undefined);
	assert.equal(parseInspectResult({ ok: true, success: true, operation: "inspect", exit_status: 0, images: [{}] }), undefined);
	assert.deepEqual(parseRoomsResult({ ok: true, projects: [{ id: "demo-room", display_name: "Demo Room", private_endpoint: "https://secret.invalid" }] }), { kind: "rooms", rooms: [{ projectId: "demo-room", displayName: "Demo Room" }] });
	assert.deepEqual(parseDoctorResult({ format_version: 1, product: "josh-room", ok: false, selected_backend: "local", selected_ide: "terminal", checks: [{ name: "age", ok: false, remediation: "/private/path" }] }), { kind: "doctor", ok: false, checks: [{ name: "age", ok: false }] });
	assert.equal(parseDoctorResult({ format_version: 2, product: "josh-room", ok: true, selected_backend: "local", selected_ide: "terminal", checks: [] }), undefined);
	assert.deepEqual(checkpointEntry({ kind: "linked", dimensionId: "local", projectId: "demo-room", displayName: "Demo Room", snapshotId: "jat-old" }, { kind: "saved", status: "saved", projectId: "demo-room", snapshotId: "jat-new", previousSnapshotId: "jat-old", ciphertextSize: 4, dataAddedBytes: 4 }), {
		format_version: 1, dimension_id: "local", project_id: "demo-room", previous_snapshot_id: "jat-old", snapshot_id: "jat-new",
	});
});

test("native logical Save receipts prefer aggregate data bytes, preserve dirty status and accept no-op without ciphertext", () => {
	const saved = parseSaveResult({
		ok: true, status: "saved", project_id: "demo-room", snapshot_id: "logical-new",
		workspace_snapshot_id: "restic-new", workspace_parent_snapshot_id: "restic-old",
		data_added_bytes: 1234, scanned_bytes: 4096, has_external_components: true,
		ciphertext_size: 2048,
	});
	assert.deepEqual(saved, {
		kind: "saved", status: "saved", projectId: "demo-room", snapshotId: "logical-new",
		ciphertextSize: 2048, dataAddedBytes: 1234,
	});
	assert.deepEqual(parseSaveResult({
		ok: true, status: "saved-but-dirty", project_id: "demo-room", snapshot_id: "logical-dirty",
		data_added_bytes: 512, has_external_components: false,
	}), {
		kind: "saved", status: "saved-but-dirty", projectId: "demo-room", snapshotId: "logical-dirty",
		dataAddedBytes: 512,
	});
	assert.deepEqual(parseSaveResult({ ok: true, status: "already-saved", project_id: "demo-room", snapshot_id: "logical-current", data_added_bytes: 0 }), {
		kind: "already-saved", status: "already-saved", projectId: "demo-room", snapshotId: "logical-current", dataAddedBytes: 0,
	});
	assert.equal(parseSaveResult({ ok: true, status: "future-save-state", project_id: "demo-room", snapshot_id: "logical-new", data_added_bytes: 1, ciphertext_size: 1 }), undefined);
	assert.equal(parseSaveResult({ ok: true, status: "already-saved", project_id: "demo-room", snapshot_id: "logical-current", data_added_bytes: 1 }), undefined);
	assert.equal(parseSaveResult({ ok: true, status: "saved", project_id: "demo-room", snapshot_id: "logical-new", data_added_bytes: 1.5 }), undefined);
});

test("Save outcome does not create provenance for already-saved and stays dirty after saved-but-dirty", () => {
	const context = { kind: "linked", dimensionId: "local", projectId: "demo-room", displayName: "Demo Room", snapshotId: "logical-old" };
	const already = presentSaveOutcome(context, { kind: "already-saved", status: "already-saved", projectId: "demo-room", snapshotId: "logical-old", dataAddedBytes: 0 });
	assert.match(already.message, /^Already saved/);
	assert.match(already.message, /0 bytes added/);
	assert.equal(already.statusText, "room:Demo Room ✓");
	assert.equal(already.checkpointEntry, undefined);
	assert.equal(already.details.status, "already-saved");
	assert.equal(already.details.data_added_bytes, 0);

	const dirty = presentSaveOutcome(context, { kind: "saved", status: "saved-but-dirty", projectId: "demo-room", snapshotId: "logical-new", previousSnapshotId: "logical-old", dataAddedBytes: 12 });
	assert.match(dirty.message, /workspace changed during Save/);
	assert.equal(dirty.statusText, "room:Demo Room ●");
	assert.equal(dirty.details.status, "saved-but-dirty");
	assert.equal(dirty.details.data_added_bytes, 12);
	assert.equal(dirty.checkpointEntry.snapshot_id, "logical-new");
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
