const crypto = require("crypto");
const fs = require("fs");
const path = require("path");

function shouldMarkDirty(relativePath) {
  const normalized = String(relativePath).replaceAll("\\", "/").replace(/^\.\//, "");
  if (!normalized || normalized === ".." || normalized.startsWith("../")) return false;
  if (normalized === ".josh-room.json" || normalized === ".DS_Store") return false;
  if (normalized === ".git" || normalized.startsWith(".git/")) return false;
  if (normalized === ".pytest_cache" || normalized.startsWith(".pytest_cache/")) return false;
  if (normalized === ".ruff_cache" || normalized.startsWith(".ruff_cache/")) return false;
  if (normalized === ".venv" || normalized.startsWith(".venv/")) return false;
  if (normalized === "venv" || normalized.startsWith("venv/")) return false;
  if (normalized === "node_modules" || normalized.startsWith("node_modules/")) return false;
  if (normalized.includes("/__pycache__/") || normalized.startsWith("__pycache__/")) return false;
  if (normalized.includes("node_modules/.cache/")) return false;
  return true;
}

async function fingerprintFile(filePath) {
  let stat;
  try {
    stat = await fs.promises.lstat(filePath);
  } catch (error) {
    if (error.code === "ENOENT") return undefined;
    throw error;
  }
  if (stat.isSymbolicLink()) return `link:${stat.mode}:${await fs.promises.readlink(filePath)}`;
  if (stat.isDirectory()) return `directory:${stat.mode}`;
  if (!stat.isFile()) return `special:${stat.mode}`;
  const digest = crypto.createHash("sha256");
  digest.update(`${stat.size}:${stat.mode}:`);
  const handle = await fs.promises.open(filePath, "r");
  try {
    const buffer = Buffer.alloc(1024 * 1024);
    while (true) {
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
      if (!bytesRead) break;
      digest.update(buffer.subarray(0, bytesRead));
    }
  } finally {
    await handle.close();
  }
  return `file:${digest.digest("hex")}`;
}

async function scanWorkspace(root) {
  const files = new Map();
  async function visit(directory, prefix = "") {
    const entries = await fs.promises.readdir(directory, { withFileTypes: true });
    if (!prefix && !entries.length) return;
    if (prefix && !entries.length) {
      files.set(prefix, await fingerprintFile(directory));
      return;
    }
    for (const entry of entries) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (!shouldMarkDirty(relative)) continue;
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
        files.set(relative, await fingerprintFile(absolute));
        await visit(absolute, relative);
      } else {
        files.set(relative, await fingerprintFile(absolute));
      }
    }
  }
  await visit(root);
  return files;
}

class WorkspaceBaseline {
  constructor(root) {
    this.root = path.resolve(root);
    this.files = new Map();
    this.dirty = new Set();
    this.sequence = new Map();
  }

  async capture() {
    this.files = await scanWorkspace(this.root);
    this.dirty.clear();
    this.sequence.clear();
  }

  async check(relativePath) {
    const relative = String(relativePath).replaceAll("\\", "/").replace(/^\.\//, "");
    if (!shouldMarkDirty(relative)) return this.dirty.size > 0;
    const sequence = (this.sequence.get(relative) || 0) + 1;
    this.sequence.set(relative, sequence);
    const current = await fingerprintFile(path.join(this.root, relative));
    if (this.sequence.get(relative) !== sequence) return this.dirty.size > 0;
    if (current === "directory") return this.compare();
    if (this.files.get(relative) === current) this.dirty.delete(relative);
    else this.dirty.add(relative);
    return this.dirty.size > 0;
  }

  async compare() {
    const current = await scanWorkspace(this.root);
    this.dirty.clear();
    for (const relative of new Set([...this.files.keys(), ...current.keys()])) {
      if (this.files.get(relative) !== current.get(relative)) this.dirty.add(relative);
    }
    return this.dirty.size > 0;
  }
}

module.exports = { WorkspaceBaseline, fingerprintFile, shouldMarkDirty };

function workspaceFingerprint(files) {
  const digest = crypto.createHash("sha256");
  for (const [relative, fingerprint] of [...files.entries()].sort(([left], [right]) => left.localeCompare(right))) {
    digest.update(relative);
    digest.update("\0");
    digest.update(fingerprint || "missing");
    digest.update("\n");
  }
  return digest.digest("hex");
}

class AuthoritativeWorkspaceBaseline {
  constructor(root, { savedFingerprint, currentFingerprint, fingerprintProvider } = {}) {
    this.root = path.resolve(root);
    this.files = new Map();
    this.dirty = new Set();
    this.sequence = new Map();
    this.savedFingerprint = savedFingerprint;
    this.currentFingerprint = currentFingerprint;
    this.fingerprintProvider = fingerprintProvider;
  }

  async capture({ savedFingerprint, currentFingerprint, fingerprintProvider } = {}) {
    if (savedFingerprint !== undefined) this.savedFingerprint = savedFingerprint;
    if (currentFingerprint !== undefined) this.currentFingerprint = currentFingerprint;
    if (fingerprintProvider !== undefined) this.fingerprintProvider = fingerprintProvider;
    this.files = await scanWorkspace(this.root);
    this.dirty.clear();
    this.sequence.clear();
    if (this.savedFingerprint) {
      const current = this.currentFingerprint || workspaceFingerprint(this.files);
      this.currentFingerprint = current;
      if (current !== this.savedFingerprint) this.dirty.add(".");
    }
  }

  async check(relativePath) {
    const relative = String(relativePath).replaceAll("\\", "/").replace(/^\.\//, "");
    if (!shouldMarkDirty(relative)) return this.dirty.size > 0;
    const sequence = (this.sequence.get(relative) || 0) + 1;
    this.sequence.set(relative, sequence);
    if (this.fingerprintProvider) {
      const current = await this.fingerprintProvider();
      if (this.sequence.get(relative) !== sequence) return this.dirty.size > 0;
      this.currentFingerprint = current;
      if (this.savedFingerprint && current === this.savedFingerprint) this.dirty.clear();
      else this.dirty.add(".");
      return this.dirty.size > 0;
    }
    const current = await fingerprintFile(path.join(this.root, relative));
    if (this.sequence.get(relative) !== sequence) return this.dirty.size > 0;
    if (current === "directory") return this.compare();
    if (this.files.get(relative) === current) this.dirty.delete(relative);
    else this.dirty.add(relative);
    if (this.savedFingerprint && this.dirty.size === 0) {
      const fingerprint = workspaceFingerprint(await scanWorkspace(this.root));
      this.currentFingerprint = fingerprint;
      if (fingerprint !== this.savedFingerprint) this.dirty.add(".");
    }
    return this.dirty.size > 0;
  }

  async compare() {
    if (this.fingerprintProvider) {
      this.currentFingerprint = await this.fingerprintProvider();
      this.dirty.clear();
      if (this.savedFingerprint && this.currentFingerprint !== this.savedFingerprint) this.dirty.add(".");
      return this.dirty.size > 0;
    }
    const current = await scanWorkspace(this.root);
    this.dirty.clear();
    for (const relative of new Set([...this.files.keys(), ...current.keys()])) {
      if (this.files.get(relative) !== current.get(relative)) this.dirty.add(relative);
    }
    if (this.savedFingerprint && workspaceFingerprint(current) !== this.savedFingerprint) this.dirty.add(".");
    return this.dirty.size > 0;
  }

  reset({ savedFingerprint, currentFingerprint } = {}) {
    this.savedFingerprint = savedFingerprint;
    this.currentFingerprint = currentFingerprint || savedFingerprint;
    this.dirty.clear();
    this.sequence.clear();
  }
}

module.exports.WorkspaceBaseline = AuthoritativeWorkspaceBaseline;
module.exports.workspaceFingerprint = workspaceFingerprint;

function isRoomMarker(marker) {
  const digest = (value) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  return Boolean(
    marker && [1, 2].includes(marker.format_version)
    && typeof marker.project_id === "string"
    && typeof marker.display_name === "string"
    && marker.display_name.length > 0
    && (marker.format_version === 1 || (
      typeof marker.snapshot_id === "string"
      && typeof marker.dimension_id === "string"
      && digest(marker.workspace_fingerprint)
      && digest(marker.workspace_path_sha256)
    )),
  );
}

module.exports.isRoomMarker = isRoomMarker;
