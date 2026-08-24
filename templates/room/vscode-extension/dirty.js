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
  if (stat.isSymbolicLink()) return `link:${await fs.promises.readlink(filePath)}`;
  if (stat.isDirectory()) return "directory";
  if (!stat.isFile()) return `special:${stat.mode}`;
  const digest = crypto.createHash("sha256");
  digest.update(`${stat.size}:`);
  if (stat.size <= 4 * 1024 * 1024) {
    digest.update(await fs.promises.readFile(filePath));
  } else {
    const sampleSize = 64 * 1024;
    const offsets = [0, Math.max(0, Math.floor(stat.size / 2) - sampleSize / 2), Math.max(0, stat.size - sampleSize)];
    const handle = await fs.promises.open(filePath, "r");
    try {
      for (const offset of offsets) {
        const buffer = Buffer.alloc(Math.min(sampleSize, stat.size - offset));
        const { bytesRead } = await handle.read(buffer, 0, buffer.length, offset);
        digest.update(buffer.subarray(0, bytesRead));
      }
    } finally {
      await handle.close();
    }
  }
  return `file:${digest.digest("hex")}`;
}

async function scanWorkspace(root) {
  const files = new Map();
  async function visit(directory, prefix = "") {
    const entries = await fs.promises.readdir(directory, { withFileTypes: true });
    for (const entry of entries) {
      const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
      if (!shouldMarkDirty(relative)) continue;
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory() && !entry.isSymbolicLink()) {
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
