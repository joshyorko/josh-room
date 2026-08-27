const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");

const MAX_PENDING_EVENTS = 128;
const SORT_RUN_SIZE = 256;
const SORT_MERGE_FAN_IN = 16;

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
  if (normalized === "__pycache__"
    || normalized.startsWith("__pycache__/")
    || normalized.endsWith("/__pycache__")
    || normalized.includes("/__pycache__/")) return false;
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

async function traverseWorkspace(root, onEntry) {
  async function visit(directory, prefix = "") {
    for await (const name of sortedDirectoryNames(directory)) {
      const relative = prefix ? `${prefix}/${name}` : name;
      if (!shouldMarkDirty(relative)) continue;
      const absolute = path.join(directory, name);
      let stat;
      try {
        stat = await fs.promises.lstat(absolute);
      } catch (error) {
        if (error.code === "ENOENT") continue;
        throw error;
      }
      await onEntry(relative, absolute, stat);
      if (stat.isDirectory() && !stat.isSymbolicLink()) await visit(absolute, relative);
    }
  }
  await visit(root);
}

function compareUtf8(left, right) {
  return Buffer.from(left, "utf8").compare(Buffer.from(right, "utf8"));
}

async function* readLines(filePath) {
  const input = fs.createReadStream(filePath, { encoding: "utf8" });
  const lines = readline.createInterface({ input, crlfDelay: Infinity });
  try {
    for await (const line of lines) yield line;
  } finally {
    lines.close();
    input.destroy();
  }
}

async function writeRun(directory, names, index) {
  names.sort(compareUtf8);
  const runPath = path.join(directory, `run-${index}.jsonl`);
  const body = names.map((name) => `${JSON.stringify(name)}\n`).join("");
  await fs.promises.writeFile(runPath, body, { mode: 0o600 });
  return runPath;
}

async function* mergeRuns(runPaths) {
  const readers = runPaths.map((runPath) => readLines(runPath)[Symbol.asyncIterator]());
  const heap = [];
  const push = (item) => {
    heap.push(item);
    let index = heap.length - 1;
    while (index > 0) {
      const parent = Math.floor((index - 1) / 2);
      if (compareUtf8(heap[parent].name, heap[index].name) <= 0) break;
      [heap[parent], heap[index]] = [heap[index], heap[parent]];
      index = parent;
    }
  };
  const pop = () => {
    const first = heap[0];
    const last = heap.pop();
    if (heap.length) {
      heap[0] = last;
      let index = 0;
      while (true) {
        const left = index * 2 + 1;
        const right = left + 1;
        let smallest = index;
        if (left < heap.length && compareUtf8(heap[left].name, heap[smallest].name) < 0) smallest = left;
        if (right < heap.length && compareUtf8(heap[right].name, heap[smallest].name) < 0) smallest = right;
        if (smallest === index) break;
        [heap[index], heap[smallest]] = [heap[smallest], heap[index]];
        index = smallest;
      }
    }
    return first;
  };
  try {
    for (const [index, reader] of readers.entries()) {
      const next = await reader.next();
      if (!next.done) push({ name: JSON.parse(next.value), index });
    }
    while (heap.length) {
      const item = pop();
      yield item.name;
      const next = await readers[item.index].next();
      if (!next.done) push({ name: JSON.parse(next.value), index: item.index });
    }
  } finally {
    for (const reader of readers) await reader.return?.();
  }
}

async function mergeRunGroup(runPaths, directory, index) {
  const runPath = path.join(directory, `run-merged-${index}.jsonl`);
  const handle = await fs.promises.open(runPath, "w", 0o600);
  try {
    for await (const name of mergeRuns(runPaths)) await handle.write(`${JSON.stringify(name)}\n`);
  } finally {
    await handle.close();
  }
  await Promise.all(runPaths.map((runPathToRemove) => fs.promises.rm(runPathToRemove, { force: true })));
  return runPath;
}

async function* sortedDirectoryNames(directory) {
  const sortDirectory = await fs.promises.mkdtemp(path.join(os.tmpdir(), "josh-room-sort-"));
  await fs.promises.chmod(sortDirectory, 0o700);
  const manifest = path.join(sortDirectory, "manifest-0.jsonl");
  let manifestHandle;
  let runIndex = 0;
  let runCount = 0;
  try {
    manifestHandle = await fs.promises.open(manifest, "w", 0o600);
    let names = [];
    const directoryHandle = await fs.promises.opendir(directory);
    try {
      for await (const entry of directoryHandle) {
        names.push(entry.name);
        if (names.length === SORT_RUN_SIZE) {
          const runPath = await writeRun(sortDirectory, names, runIndex);
          await manifestHandle.write(`${JSON.stringify(runPath)}\n`);
          runIndex += 1;
          runCount += 1;
          names = [];
        }
      }
    } finally {
      try {
        await directoryHandle.close();
      } catch (error) {
        if (error.code !== "ERR_DIR_CLOSED") throw error;
      }
    }
    if (names.length) {
      const runPath = await writeRun(sortDirectory, names, runIndex);
      await manifestHandle.write(`${JSON.stringify(runPath)}\n`);
      runCount += 1;
    }
    await manifestHandle.close();
    manifestHandle = undefined;

    let currentManifest = manifest;
    let pass = 0;
    while (runCount > SORT_MERGE_FAN_IN) {
      const nextManifest = path.join(sortDirectory, `manifest-${pass + 1}.jsonl`);
      const nextHandle = await fs.promises.open(nextManifest, "w", 0o600);
      let group = [];
      let mergedIndex = 0;
      try {
        for await (const line of readLines(currentManifest)) {
          group.push(JSON.parse(line));
          if (group.length === SORT_MERGE_FAN_IN) {
            const mergedPath = await mergeRunGroup(group, sortDirectory, `${pass}-${mergedIndex}`);
            await nextHandle.write(`${JSON.stringify(mergedPath)}\n`);
            mergedIndex += 1;
            group = [];
          }
        }
        if (group.length) {
          const mergedPath = await mergeRunGroup(group, sortDirectory, `${pass}-${mergedIndex}`);
          await nextHandle.write(`${JSON.stringify(mergedPath)}\n`);
          mergedIndex += 1;
        }
      } finally {
        await nextHandle.close();
      }
      await fs.promises.rm(currentManifest, { force: true });
      currentManifest = nextManifest;
      runCount = mergedIndex;
      pass += 1;
    }

    const finalRuns = [];
    for await (const line of readLines(currentManifest)) finalRuns.push(JSON.parse(line));
    for await (const name of mergeRuns(finalRuns)) yield name;
  } finally {
    if (manifestHandle) await manifestHandle.close();
    await fs.promises.rm(sortDirectory, { recursive: true, force: true });
  }
}

async function fingerprintWorkspace(root) {
  const digest = crypto.createHash("sha256");
  await traverseWorkspace(root, async (relative, absolute) => {
    const fingerprint = await fingerprintFile(absolute);
    digest.update(relative);
    digest.update("\0");
    digest.update(fingerprint || "missing");
    digest.update("\n");
  });
  return digest.digest("hex");
}

function nextSequence(sequence, key, counter) {
  const value = counter + 1;
  sequence.set(key, value);
  if (sequence.size > MAX_PENDING_EVENTS) sequence.delete(sequence.keys().next().value);
  return value;
}

function workspaceFingerprint(files) {
  const digest = crypto.createHash("sha256");
  for (const [relative, fingerprint] of files) {
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
    this.eventSequence = 0;
    this.savedFingerprint = savedFingerprint;
    this.currentFingerprint = currentFingerprint;
    this.fingerprintProvider = fingerprintProvider;
  }

  async capture({ savedFingerprint, currentFingerprint, fingerprintProvider } = {}) {
    if (savedFingerprint !== undefined) this.savedFingerprint = savedFingerprint;
    if (currentFingerprint !== undefined) this.currentFingerprint = currentFingerprint;
    if (fingerprintProvider !== undefined) this.fingerprintProvider = fingerprintProvider;
    this.dirty.clear();
    this.sequence.clear();
    this.eventSequence = 0;
    this.files = new Map();
    const current = this.currentFingerprint || await fingerprintWorkspace(this.root);
    this.currentFingerprint = current;
    if (!this.savedFingerprint) this.savedFingerprint = current;
    if (current !== this.savedFingerprint) this.dirty.add(".");
  }

  async check(relativePath) {
    const relative = String(relativePath).replaceAll("\\", "/").replace(/^\.\//, "");
    if (!shouldMarkDirty(relative)) return this.dirty.size > 0;
    const sequence = nextSequence(this.sequence, relative, this.eventSequence);
    this.eventSequence = sequence;
    const current = this.fingerprintProvider
      ? await this.fingerprintProvider()
      : await fingerprintWorkspace(this.root);
    if (this.sequence.get(relative) !== sequence) return this.dirty.size > 0;
    this.currentFingerprint = current;
    if (this.savedFingerprint && current === this.savedFingerprint) this.dirty.clear();
    else this.dirty.add(".");
    return this.dirty.size > 0;
  }

  async compare() {
    this.currentFingerprint = this.fingerprintProvider
      ? await this.fingerprintProvider()
      : await fingerprintWorkspace(this.root);
    this.dirty.clear();
    if (this.savedFingerprint && this.currentFingerprint !== this.savedFingerprint) this.dirty.add(".");
    return this.dirty.size > 0;
  }

  reset({ savedFingerprint, currentFingerprint } = {}) {
    this.savedFingerprint = savedFingerprint;
    this.currentFingerprint = currentFingerprint || savedFingerprint;
    this.dirty.clear();
    this.sequence.clear();
    this.eventSequence = 0;
    this.files = new Map();
  }
}

module.exports = {
  WorkspaceBaseline: AuthoritativeWorkspaceBaseline,
  fingerprintFile,
  fingerprintWorkspace,
  shouldMarkDirty,
};
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
