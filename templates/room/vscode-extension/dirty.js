const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");
const readline = require("readline");

const MAX_PENDING_EVENTS = 128;
const MAX_IGNORE_BYTES = 64 * 1024;
const MAX_IGNORE_RULES = 128;
const MAX_RULE_LENGTH = 1024;
const MAX_PATTERN_SEGMENTS = 128;
const SORT_RUN_SIZE = 256;
const SORT_MERGE_FAN_IN = 16;
const DEFAULTS_PATH = path.resolve(__dirname, "runtime/controller/josh_room/workspace_capture_defaults.json");

function readDefaultCapturePolicy() {
  return fs.readFileSync(DEFAULTS_PATH, "utf8");
}

function patternParts(pattern, label = "capture pattern") {
  if (typeof pattern !== "string" || !pattern || pattern.startsWith("/")
    || /^[A-Za-z]:\//.test(pattern) || pattern.includes("\\") || pattern.length > MAX_RULE_LENGTH) {
    throw new Error(`${label} is invalid`);
  }
  const rawParts = pattern.split("/");
  if (rawParts.length > MAX_PATTERN_SEGMENTS) throw new Error(`${label} has too many path components`);
  const parts = rawParts.filter((part, index) => part !== "**" || index === 0 || rawParts[index - 1] !== "**");
  if (parts.some((part) => !part || part === "." || part === ".." || /[!\[\]]/.test(part))) {
    throw new Error(`${label} is invalid`);
  }
  if (parts.some((part) => part.includes("**") && part !== "**")) {
    throw new Error(`${label} uses unsupported glob syntax`);
  }
  if (parts.length === 1 && parts[0] === "**") throw new Error(`${label} cannot exclude the whole workspace`);
  return parts;
}

function segmentMatches(value, pattern) {
  let expression = "^";
  for (const character of pattern) {
    if (character === "*") expression += "[^/]*";
    else if (character === "?") expression += "[^/]";
    else expression += character.replace(/[|\\{}()[\]^$+?.]/g, "\\$&");
  }
  return new RegExp(`${expression}$`, "u").test(value);
}

function globMatches(pattern, value) {
  const memo = new Map();
  function match(patternIndex, valueIndex) {
    const key = `${patternIndex}:${valueIndex}`;
    if (memo.has(key)) return memo.get(key);
    let result;
    if (patternIndex === pattern.length) result = valueIndex === value.length;
    else if (pattern[patternIndex] === "**") {
      result = match(patternIndex + 1, valueIndex)
        || (valueIndex < value.length && match(patternIndex, valueIndex + 1));
    } else {
      result = valueIndex < value.length
        && segmentMatches(value[valueIndex], pattern[patternIndex])
        && match(patternIndex + 1, valueIndex + 1);
    }
    memo.set(key, result);
    return result;
  }
  return match(0, 0);
}

function ruleMatches(parts, pattern) {
  if (pattern.length === 1 && pattern[0] !== "**") {
    return parts.some((part) => globMatches(pattern, [part]));
  }
  for (let length = 1; length <= parts.length; length += 1) {
    if (globMatches(pattern, parts.slice(0, length))) return true;
  }
  return false;
}

function compileCapturePolicy(defaults = readDefaultCapturePolicy(), { ignoreText, activeRuntimeRelative } = {}) {
  const defaultsRaw = typeof defaults === "string" ? defaults : JSON.stringify(defaults);
  let body;
  try {
    body = JSON.parse(defaultsRaw);
  } catch (error) {
    throw new Error("workspace capture defaults are invalid", { cause: error });
  }
  if (!body || body.version !== 1 || !Array.isArray(body.exclude) || !body.exclude.length
    || Object.keys(body).some((key) => !["version", "exclude"].includes(key))) {
    throw new Error("unsupported workspace capture defaults");
  }
  if (body.exclude.length > MAX_IGNORE_RULES) throw new Error("workspace capture defaults have too many rules");
  const patterns = body.exclude.map((item) => patternParts(item, "workspace capture default"));
  if (ignoreText !== undefined && ignoreText !== null) {
    if (typeof ignoreText !== "string") throw new Error("workspace ignore file must be text");
    if (Buffer.byteLength(ignoreText, "utf8") > MAX_IGNORE_BYTES) throw new Error("workspace ignore file exceeds 64 KiB");
    let userRuleCount = 0;
    for (const line of ignoreText.split("\n")) {
      const rule = line.trim();
      if (!rule || rule.startsWith("#")) continue;
      if (rule.startsWith("!")) throw new Error("workspace ignore rules cannot reinclude paths");
      if (userRuleCount >= MAX_IGNORE_RULES) throw new Error("workspace ignore file has too many rules");
      userRuleCount += 1;
      patterns.push(patternParts(rule, "workspace ignore rule"));
    }
  }
  if (activeRuntimeRelative !== undefined && activeRuntimeRelative !== null) {
    const activeParts = patternParts(activeRuntimeRelative, "active runtime path");
    if (activeParts.some((part) => /[*?\[\]]/.test(part))) {
      throw new Error("active runtime path contains unsupported glob characters");
    }
    activeRuntimeRelative = activeParts.join("/");
  } else activeRuntimeRelative = null;

  const digest = crypto.createHash("sha256");
  digest.update(defaultsRaw, "utf8");
  digest.update(ignoreText === undefined || ignoreText === null ? "\0ignore-absent\0" : "\0ignore-present\0", "utf8");
  if (ignoreText !== undefined && ignoreText !== null) digest.update(ignoreText, "utf8");
  digest.update("\0active-runtime\0", "utf8");
  digest.update(activeRuntimeRelative || "", "utf8");
  const sha256 = digest.digest("hex");

  function isExcluded(relativePath) {
    const normalized = String(relativePath);
    if (!normalized || normalized.startsWith("/") || normalized.includes("\\")) return false;
    const parts = normalized.split("/");
    if (parts.some((part) => !part || part === "." || part === "..")) return false;
    if (activeRuntimeRelative) {
      const runtime = activeRuntimeRelative.split("/");
      if (runtime.every((part, index) => parts[index] === part)) return true;
    }
    return patterns.some((pattern) => ruleMatches(parts, pattern));
  }
  const resticExcludes = () => {
    const result = [];
    for (const pattern of patterns) {
      const raw = pattern.join("/");
      const rendered = pattern.length === 1 && pattern[0] !== "**" ? `**/${raw}` : raw;
      result.push(rendered);
      if (pattern[pattern.length - 1] === "**") {
        const base = pattern.slice(0, -1).join("/");
        if (base) result.push(base);
      } else result.push(`${rendered}/**`);
    }
    if (activeRuntimeRelative) result.push(activeRuntimeRelative, `${activeRuntimeRelative}/**`);
    return [...new Set(result)];
  };
  return Object.freeze({ sha256, patterns: Object.freeze(patterns), activeRuntimeRelative, isExcluded, resticExcludes });
}

function loadCapturePolicy(workspaceRoot, { activeRuntimeRoot } = {}) {
  const root = fs.realpathSync(workspaceRoot);
  if (!fs.statSync(root).isDirectory()) throw new Error("workspace must be a directory");
  const ignorePath = path.join(root, ".josh-roomignore");
  let ignoreText;
  let metadata;
  try {
    metadata = fs.lstatSync(ignorePath);
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  if (metadata) {
    if (!metadata.isFile() || metadata.size > MAX_IGNORE_BYTES) {
      throw new Error("workspace ignore file must be a regular file of at most 64 KiB");
    }
    let descriptor;
    try {
      const flags = fs.constants.O_RDONLY
        | (fs.constants.O_NONBLOCK || 0)
        | (fs.constants.O_NOFOLLOW || 0);
      descriptor = fs.openSync(ignorePath, flags);
      const opened = fs.fstatSync(descriptor);
      if (!opened.isFile() || opened.dev !== metadata.dev || opened.ino !== metadata.ino
        || opened.size > MAX_IGNORE_BYTES) {
        throw new Error("workspace ignore file changed while opening");
      }
      const bytes = Buffer.allocUnsafe(MAX_IGNORE_BYTES + 1);
      let offset = 0;
      while (offset <= MAX_IGNORE_BYTES) {
        const count = fs.readSync(descriptor, bytes, offset, MAX_IGNORE_BYTES + 1 - offset, null);
        if (!count) break;
        offset += count;
      }
      if (offset > MAX_IGNORE_BYTES) throw new Error("workspace ignore file exceeds 64 KiB");
      try {
        ignoreText = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes.subarray(0, offset));
      } catch (error) {
        throw new Error("workspace ignore file must be valid UTF-8", { cause: error });
      }
    } catch (error) {
      if (error.code === "ENOENT" || error.code === "ELOOP") {
        throw new Error("workspace ignore file changed while opening", { cause: error });
      }
      throw error;
    } finally {
      if (descriptor !== undefined) fs.closeSync(descriptor);
    }
  }

  let activeRuntimeRelative;
  if (activeRuntimeRoot) {
    const runtime = fs.realpathSync(activeRuntimeRoot);
    if (!fs.statSync(runtime).isDirectory()) throw new Error("active runtime root must be a directory");
    const relative = path.relative(root, runtime);
    if (!relative) throw new Error("active runtime root cannot be the workspace root");
    if (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative)) {
      activeRuntimeRelative = relative.split(path.sep).join("/");
    }
  }
  return compileCapturePolicy(readDefaultCapturePolicy(), { ignoreText, activeRuntimeRelative });
}

const DEFAULT_CAPTURE_POLICY = compileCapturePolicy();

function shouldMarkDirty(relativePath, policy = DEFAULT_CAPTURE_POLICY) {
  const normalized = String(relativePath).replaceAll("\\", "/").replace(/^\.\//, "");
  if (!normalized || normalized === ".." || normalized.startsWith("../")) return false;
  return !policy.isExcluded(normalized);
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

async function traverseWorkspace(root, onEntry, policy = DEFAULT_CAPTURE_POLICY) {
  async function visit(directory, prefix = "") {
    for await (const name of sortedDirectoryNames(directory)) {
      const relative = prefix ? `${prefix}/${name}` : name;
      if (!shouldMarkDirty(relative, policy)) continue;
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

async function fingerprintWorkspace(root, policy = DEFAULT_CAPTURE_POLICY) {
  const digest = crypto.createHash("sha256");
  await traverseWorkspace(root, async (relative, absolute) => {
    const fingerprint = await fingerprintFile(absolute);
    digest.update(relative);
    digest.update("\0");
    digest.update(fingerprint || "missing");
    digest.update("\n");
  }, policy);
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
  constructor(root, { savedFingerprint, currentFingerprint, fingerprintProvider, capturePolicy = DEFAULT_CAPTURE_POLICY } = {}) {
    this.root = path.resolve(root);
    this.files = new Map();
    this.dirty = new Set();
    this.sequence = new Map();
    this.eventSequence = 0;
    this.savedFingerprint = savedFingerprint;
    this.currentFingerprint = currentFingerprint;
    this.fingerprintProvider = fingerprintProvider;
    this.capturePolicy = capturePolicy;
  }

  async capture({ savedFingerprint, currentFingerprint, fingerprintProvider } = {}) {
    if (savedFingerprint !== undefined) this.savedFingerprint = savedFingerprint;
    if (currentFingerprint !== undefined) this.currentFingerprint = currentFingerprint;
    if (fingerprintProvider !== undefined) this.fingerprintProvider = fingerprintProvider;
    this.dirty.clear();
    this.sequence.clear();
    this.eventSequence = 0;
    this.files = new Map();
    const current = this.currentFingerprint || await fingerprintWorkspace(this.root, this.capturePolicy);
    this.currentFingerprint = current;
    if (!this.savedFingerprint) this.savedFingerprint = current;
    if (current !== this.savedFingerprint) this.dirty.add(".");
  }

  async check(relativePath) {
    const relative = String(relativePath).replaceAll("\\", "/").replace(/^\.\//, "");
    if (!shouldMarkDirty(relative, this.capturePolicy)) return this.dirty.size > 0;
    const sequence = nextSequence(this.sequence, relative, this.eventSequence);
    this.eventSequence = sequence;
    const current = this.fingerprintProvider
      ? await this.fingerprintProvider()
      : await fingerprintWorkspace(this.root, this.capturePolicy);
    if (this.sequence.get(relative) !== sequence) return this.dirty.size > 0;
    this.currentFingerprint = current;
    if (this.savedFingerprint && current === this.savedFingerprint) this.dirty.clear();
    else this.dirty.add(".");
    return this.dirty.size > 0;
  }

  async compare() {
    this.currentFingerprint = this.fingerprintProvider
      ? await this.fingerprintProvider()
      : await fingerprintWorkspace(this.root, this.capturePolicy);
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
  loadCapturePolicy,
  compileCapturePolicy,
  shouldMarkDirty,
};
module.exports.WorkspaceBaseline = AuthoritativeWorkspaceBaseline;
module.exports.workspaceFingerprint = workspaceFingerprint;

function isRoomMarker(marker) {
  const digest = (value) => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
  return Boolean(
    marker && [1, 2, 3].includes(marker.format_version)
    && typeof marker.project_id === "string"
    && typeof marker.display_name === "string"
    && marker.display_name.length > 0
    && (marker.format_version === 1 || (marker.format_version === 2 && (
      typeof marker.snapshot_id === "string"
      && typeof marker.dimension_id === "string"
      && digest(marker.workspace_fingerprint)
      && digest(marker.workspace_path_sha256)
    )) || (marker.format_version === 3 && (
      typeof marker.snapshot_id === "string"
      && typeof marker.dimension_id === "string"
      && typeof marker.encryption_domain_id === "string"
      && digest(marker.workspace_signature)
      && marker.signature_algorithm === "josh-room-stat-v1"
      && digest(marker.capture_policy_sha256)
      && digest(marker.workspace_path_sha256)
    ))),
  );
}

module.exports.isRoomMarker = isRoomMarker;
