"use strict";
const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const net = require("node:net");
const crypto = require("node:crypto");
const { comparisonQueries, startComparisonBroker } = require("./room-view-comparison");

function fixture() {
  const binding = { room_id: "test-room", dimension_id: "test-dimension", encryption_domain_id: "synthetic-domain" };
  const dimension = { id: binding.dimension_id, provider: "minio", connection_id: "connection" };
  const points = ["first", "second"].map((version, i) => ({
    ...binding, payload_kind: "room-store-v1", snapshot_id: "jat-" + version,
    created_at: "2026-01-0" + (i + 1) + "T00:00:00Z", ciphertext_sha256: String(i + 1).repeat(64),
    workspace_snapshot_id: "f".repeat(64), tree_id: String(i + 3).repeat(64),
  }));
  const data = Buffer.from("old version\n");
  const sha256 = crypto.createHash("sha256").update(data).digest("hex");
  const file = { type: "file", size: data.length, sha256,
    text_preview: { state: "available", encoding: "base64-utf8", data: data.toString("base64"), size: data.length, sha256 } };
  const calls = [];
  const state = { file, receiptOverrides: {}, trusted: true };
  const query = comparisonQueries(binding, async () => dimension, async argv => {
    calls.push(argv);
    if (argv[0] === "snapshots") return { ok: true, project: binding.room_id, dimension_id: binding.dimension_id,
      latest: points.at(-1).snapshot_id, snapshots: [...points, { snapshot_id: "portable" }] };
    const row = points.find(row => row.snapshot_id === argv[argv.indexOf("--snapshot") + 1]);
    return { ...row, provider: dimension.provider, connection_id: dimension.connection_id,
      path: argv.find(arg => arg.startsWith("--path=")).slice(7), file: state.file, ok: true, ...state.receiptOverrides };
  }, () => { if (!state.trusted) throw new Error("trust revoked"); });
  return { query: request => query(request, new AbortController().signal), points, dimension, state, calls };
}

test("select an older point, retain exact identity across refresh and omit portable bytes", async () => {
  const { query, points, state, calls } = fixture();
  const list = await query({ op: "points" });
  assert.equal(list.portable, 1);
  const point = list.points[0].id;
  assert.equal((await query({ op: "points" })).points[0].id, point);
  const request = { op: "inspect", point, path: "src/-name.py" };
  const result = await query(request);
  assert.equal(result.snapshot, points[0].snapshot_id);
  assert.equal(result.tree_id, points[0].tree_id);
  assert.equal(result.file.sha256, state.file.sha256);
  assert.ok(!calls.flat().includes("latest"));
  state.file = null;
  assert.equal((await query(request)).file, null);
  points[0].tree_id = "d".repeat(64);
  await assert.rejects(query(request), { comparisonCode: "stale" });
  assert.notEqual((await query({ op: "points" })).points[0].id, point);
  await assert.rejects(query(request), { comparisonCode: "stale" });
});

test("an inventory at the controller display bound reports unsupported", async () => {
  const { query, points } = fixture();
  while (points.length < 127) points.push({...points[0], snapshot_id: "point-" + points.length});
  await assert.rejects(query({op: "points"}), {comparisonCode: "unsupported"});
});

test("reject wrong authority, mismatched or forged selection, traversal, and unsafe recovery envelopes", async () => {
  const { query, state, dimension, calls } = fixture();
  const point = (await query({ op: "points" })).points[0].id;
  const req = { op: "inspect", point, path: "src/sample.py" };
  await assert.rejects(query({ ...req, path: "../elsewhere" }), { comparisonCode: "unsupported" });
  await assert.rejects(query({ ...req, point: "unknown" }), { comparisonCode: "stale" });
  assert.equal(calls.length, 1);
  for (const key of ["tree_id", "ciphertext_sha256", "snapshot_id", "path", "room_id", "dimension_id",
    "encryption_domain_id", "provider", "connection_id", "created_at", "workspace_snapshot_id"]) {
    state.receiptOverrides = { [key]: "mismatch" };
    await assert.rejects(query(req), { comparisonCode: "stale" });
  }
  state.receiptOverrides = {};
  state.file.text_preview.data = "AAAA";
  await assert.rejects(query(req), { comparisonCode: "stale" });
  state.file = { type: "file", size: 3432432, sha256: null, text_preview: {state:"oversized"} };
  assert.deepEqual((await query(req)).file, state.file);
  dimension.connection_id = "different";
  await assert.rejects(query(req), { comparisonCode: "stale" });
  state.trusted = false;
  await assert.rejects(query(req), /trust revoked/);
});

test("private socket frames requests, rejects overlap, hides errors and removes its listener", async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "room-view-socket-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const sock = path.join(dir, "s");
  let started, release;
  const entered = new Promise(resolve => { started = resolve; });
  const hold = new Promise(resolve => { release = resolve; });
  const close = await startComparisonBroker(sock, async msg => {
    if (msg.op === "points") { started(); await hold; return { points: [] }; }
    throw new Error("private diagnostic");
  });
  const exchange = msg => new Promise((resolve, reject) => {
    const conn = net.connect(sock, () => conn.write(JSON.stringify(msg) + "\n"));
    let data = "";
    conn.on("data", chunk => { data += chunk; });
    conn.on("end", () => { conn.destroy(); resolve(JSON.parse(data)); });
    conn.on("error", reject);
  });
  try {
    assert.equal((await fs.stat(sock)).mode & 0o777, 0o600);
    const first = exchange({op: "points"});
    await entered;
    assert.deepEqual(await exchange({op: "points"}), {error: "busy"});
    release();
    assert.deepEqual(await first, {points: []});
    assert.deepEqual(await exchange({op: "invalid"}), {error: "unavailable"});
  } finally { release(); await close(); }
  await assert.rejects(fs.stat(sock), {code: "ENOENT"});
});

test("closing the native viewer cancels an outstanding provider read and unlinks its socket", async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), "room-view-cancel-"));
  t.after(() => fs.rm(dir, { recursive: true, force: true }));
  const sock = path.join(dir, "s");
  let entered, cancelled;
  const started = new Promise(resolve => { entered = resolve; });
  const stopped = new Promise(resolve => { cancelled = resolve; });
  const close = await startComparisonBroker(sock, async (_request, signal) => {
    entered();
    return new Promise((_, reject) => signal.addEventListener("abort", () => {
      cancelled();
      reject(new Error("expired"));
    }, {once:true}));
  });
  const conn = net.connect(sock, () => conn.write('{"op":"points"}\n'));
  conn.on("error", () => {});
  await started;
  await close();
  await stopped;
  conn.destroy();
  await assert.rejects(fs.stat(sock), {code:"ENOENT"});
});
