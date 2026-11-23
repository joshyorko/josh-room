"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs/promises");
const net = require("node:net");
const { decodePreview } = require("./recovery-text");

const digest = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const failure = code => Object.assign(new Error("Saved file comparison unavailable"), { comparisonCode: code });
const timestamp = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)$/;
const pinFields = ["snapshot_id", "created_at", "ciphertext_sha256", "workspace_snapshot_id", "tree_id"];

function safePath(value) {
  return typeof value === "string" && Buffer.byteLength(value) <= 4096
    && !/[\\:\x00-\x1f\x7f]/.test(value)
    && value.split("/").every(name => name && name !== "." && name !== "..");
}

function admittedFile(file) {
  if (file === null) return null;
  if (file?.type !== "file" || !Number.isSafeInteger(file.size) || file.size < 0
      || !(digest(file.sha256) || (file.sha256 === null && file.size > 1048576))) throw failure("unsupported");
  const preview = file.text_preview;
  const available = preview?.state === "available";
  if (available && (preview.size !== file.size || decodePreview(preview, file.sha256) === undefined)) throw failure("stale");
  const safeStates = ["oversized", "binary", "sensitive-path", "sensitive-content", "unsupported", "unavailable", "excluded", "redacted"];
  const text = available ? { state: "available", encoding: "base64-utf8", size: preview.size, sha256: file.sha256, data: preview.data }
    : { state: safeStates.includes(preview?.state) ? preview.state : "unavailable" };
  return { type: "file", size: file.size, sha256: file.sha256, text_preview: text };
}

function comparisonQueries(binding, resolveDimension, run, assertCurrent) {
  let pins = new Map();
  let initialAuthority;
  async function authority(signal) {
    assertCurrent();
    if (!binding.encryption_domain_id || binding.composition_namespace) throw failure("unsupported");
    const dimension = await resolveDimension(signal);
    if (!dimension || dimension.id !== binding.dimension_id || !["minio", "s3", "r2"].includes(dimension.provider))
      throw failure("unsupported");
    if (dimension.encryption_domain_id && dimension.encryption_domain_id !== binding.encryption_domain_id)
      throw failure("stale");
    const current = JSON.stringify([dimension.id, dimension.provider, dimension.connection_id, dimension.bucket, dimension.endpoint]);
    if (initialAuthority !== undefined && initialAuthority !== current) throw failure("stale");
    initialAuthority = current;
    assertCurrent();
    if (signal.aborted) throw failure("stale");
    return dimension;
  }
  return async function query(request, signal) {
    const isPoints = request?.op === "points" && Object.keys(request).length === 1;
    const isInspect = request?.op === "inspect" && Object.keys(request).length === 3
      && typeof request.point === "string" && safePath(request.path);
    if (!isPoints && !isInspect) throw failure("unsupported");
    const pin = isInspect && pins.get(request.point);
    if (isInspect && !pin) throw failure("stale");
    const dimension = await authority(signal);
    const room = binding.room_id;
    const dim = binding.dimension_id;
    if (isPoints) {
      const history = await run(["snapshots", "list", room, "--dimension", dim, "--backend", dimension.provider], dimension, signal);
      assertCurrent();
      if (history?.ok !== true || history.dimension_id !== dim || history.project !== room || !Array.isArray(history.snapshots))
        throw failure("stale");
      // The CLI display envelope caps lists at 128. At that boundary the remainder is unknown.
      if (history.snapshots.length >= 128) throw failure("unsupported");
      const next = new Map();
      const previous = new Map([...pins].map(([id, row]) => [JSON.stringify(pinFields.map(key => row[key])), id]));
      const seen = new Set();
      let portable = 0;
      for (const row of history.snapshots) {
        if (!row?.payload_kind) { portable++; continue; }
        if (row.payload_kind !== "room-store-v1" || row.dimension_id !== dim || row.room_id !== room
            || row.encryption_domain_id !== binding.encryption_domain_id || seen.has(row.snapshot_id)
            || typeof row.snapshot_id !== "string" || !/^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$/.test(row.snapshot_id)
            || row.snapshot_id === "latest" || typeof row.created_at !== "string" || !timestamp.test(row.created_at)
            || ![row.tree_id, row.ciphertext_sha256, row.workspace_snapshot_id].every(digest)) throw failure("stale");
        const key = JSON.stringify(pinFields.map(field => row[field]));
        const id = previous.get(key) || crypto.randomBytes(16).toString("hex");
        seen.add(row.snapshot_id);
        next.set(id, Object.fromEntries(pinFields.map(field => [field, row[field]])));
      }
      pins = next;
      return { points: Array.from(pins, ([id, row]) => ({ id, snapshot: row.snapshot_id, date: row.created_at })), portable };
    }
    const result = await run(["recovery", "observe", room, "--dimension", dim, "--snapshot", pin.snapshot_id,
      "--expected-ciphertext", pin.ciphertext_sha256, "--expected-tree", pin.tree_id,
      "--expected-workspace-snapshot", pin.workspace_snapshot_id, "--expected-domain", binding.encryption_domain_id,
      "--path=" + request.path, "--include-text"], dimension, signal);
    assertCurrent();
    if (result?.ok !== true || result.room_id !== room || result.dimension_id !== dim
        || result.encryption_domain_id !== binding.encryption_domain_id || result.path !== request.path
        || result.provider !== dimension.provider || (result.connection_id ?? null) !== (dimension.connection_id ?? null)
        || pinFields.some(field => result[field] !== pin[field])) throw failure("stale");
    return { snapshot: pin.snapshot_id, date: pin.created_at, workspace_snapshot_id: pin.workspace_snapshot_id,
      tree_id: pin.tree_id, file: admittedFile(result.file) };
  };
}

// One line per connection. The owning launcher supplies a private 0700 directory.
async function startComparisonBroker(socketPath, query) {
  let active;
  let busy = false;
  const server = net.createServer({ allowHalfOpen: true }, socket => {
    socket.on("error", () => socket.destroy());
    if (busy || active) {
      socket.setTimeout(1000, () => socket.destroy());
      socket.once("data", () => socket.end('{"error":"busy"}\n'));
      socket.once("end", () => socket.destroySoon());
      return;
    }
    active = socket;
    const cancellation = new AbortController();
    const deadline = setTimeout(() => socket.destroy(), 60000);
    socket.on("close", () => {
      clearTimeout(deadline);
      cancellation.abort();
      if (active === socket) active = undefined;
    });
    let frame = Buffer.alloc(0);
    let handled = false;
    socket.on("data", async chunk => {
      if (handled || frame.length + chunk.length > 8192) { socket.destroy(); return; }
      frame = Buffer.concat([frame, chunk]);
      const newline = frame.indexOf(10);
      if (newline < 0) return;
      if (newline !== frame.length - 1) { socket.destroy(); return; }
      handled = true;
      busy = true;
      try {
        const reply = JSON.stringify(await query(JSON.parse(frame.toString("utf8")), cancellation.signal)) + "\n";
        if (Buffer.byteLength(reply) > 65536) throw failure("unsupported");
        if (!socket.destroyed && !cancellation.signal.aborted) socket.end(reply);
      } catch (error) {
        const code = ["busy", "stale", "unsupported"].includes(error?.comparisonCode) ? error.comparisonCode : "unavailable";
        if (!socket.destroyed) socket.end(JSON.stringify({ error: code }) + "\n");
      } finally { busy = false; socket.destroySoon(); }
    });
    socket.on("end", () => socket.destroy());
  });
  server.maxConnections = 4;
  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(socketPath, resolve);
    });
    await fs.chmod(socketPath, 0o600);
  } catch (error) {
    if (server.listening) server.close();
    throw error;
  }
  return async () => {
    active?.destroy();
    await new Promise(resolve => server.close(resolve));
  };
}

module.exports = { comparisonQueries, startComparisonBroker };
