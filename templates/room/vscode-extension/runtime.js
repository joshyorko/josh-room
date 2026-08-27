const childProcess = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const https = require("https");
const os = require("os");
const path = require("path");

const DIGEST = /^[0-9a-f]{64}$/;
const VERSION = /^v\d+\.\d+\.\d+$/;
const MANIFEST_PATH = path.join(__dirname, "runtime", "manifest.json");

function resolvePlatform(platform = process.platform, arch = process.arch) {
  if (platform === "linux" && arch === "x64") return "linux-x64";
  throw new Error(`Josh Room runtime platform is not supported: ${platform}/${arch}`);
}

function readManifest(source = MANIFEST_PATH) {
  const manifest = typeof source === "string"
    ? JSON.parse(fs.readFileSync(source, "utf8"))
    : source;
  if (!manifest || typeof manifest !== "object" || manifest.schema_version !== 1) {
    throw new Error("Josh Room runtime manifest has an invalid schema");
  }
  if (typeof manifest.extension_version !== "string" || !manifest.extension_version) {
    throw new Error("Josh Room runtime manifest is missing extension_version");
  }
  const rcc = manifest.rcc;
  if (!rcc || typeof rcc !== "object" || !VERSION.test(rcc.version)) {
    throw new Error("Josh Room runtime manifest is missing an exact RCC version");
  }
  if (!rcc.platforms || typeof rcc.platforms !== "object") {
    throw new Error("Josh Room runtime manifest is missing RCC platform pins");
  }
  for (const [platform, pin] of Object.entries(rcc.platforms)) {
    if (!pin || typeof pin !== "object"
      || typeof pin.asset !== "string" || !pin.asset
      || typeof pin.url !== "string" || !pin.url.startsWith("https://")
      || typeof pin.sha256 !== "string" || !DIGEST.test(pin.sha256)) {
      throw new Error(`RCC platform pin is invalid: ${platform}`);
    }
    if (!pin.url.startsWith("https://github.com/joshyorko/rcc/releases/download/")) {
      throw new Error(`RCC platform pin is outside the official RCC release host: ${platform}`);
    }
  }
  return manifest;
}

function privatePaths(context) {
  const storageRoot = path.resolve(context.globalStorageUri.fsPath);
  return {
    storageRoot,
    runtimeRoot: path.join(storageRoot, "runtime"),
    rccHome: path.join(storageRoot, "robocorp"),
    configRoot: path.join(storageRoot, "config"),
    instanceRoot: path.join(storageRoot, "state", "josh-room"),
    runtimeRootDirectory: path.join(storageRoot, "runtime", "josh-room"),
    jatRoot: path.join(storageRoot, "runtime", "jat"),
    jatArtifactRoot: path.join(storageRoot, "runtime", "jat-artifact"),
    controllerRoot: path.join(storageRoot, "runtime", "controller"),
    logsRoot: path.join(storageRoot, "logs"),
  };
}

async function ensureManagedRcc(context, manifestSource = MANIFEST_PATH, options = {}) {
  const manifest = readManifest(manifestSource);
  const platform = options.platform || resolvePlatform();
  const pin = manifest.rcc.platforms[platform];
  if (!pin) throw new Error(`Josh Room has no RCC runtime pin for ${platform}`);
  const paths = privatePaths(context);
  const executable = path.join(paths.runtimeRoot, "rcc", manifest.rcc.version, platform, "rcc");
  const directory = path.dirname(executable);
  await fs.promises.mkdir(directory, { recursive: true, mode: 0o700 });
  const verifyVersion = options.verifyVersion || verifyRccVersion;
  const download = options.download || downloadFile;

  if (await isRegularFile(executable)) {
    const observed = await sha256File(executable);
    if (observed !== pin.sha256) {
      throw new Error(`cached RCC checksum does not match ${manifest.rcc.version}`);
    }
    await verifyVersion(executable, manifest.rcc.version);
    return { executable, storageRoot: paths.storageRoot, platform, version: manifest.rcc.version };
  }
  if (await pathExists(executable)) {
    throw new Error(`managed RCC path is not a regular file: ${executable}`);
  }

  const temporaryDirectory = await fs.promises.mkdtemp(path.join(directory, ".rcc-download-"));
  const temporary = path.join(temporaryDirectory, pin.asset);
  try {
    await download(pin.url, temporary);
    const observed = await sha256File(temporary);
    if (observed !== pin.sha256) {
      throw new Error(`downloaded RCC checksum mismatch: expected ${pin.sha256}, got ${observed}`);
    }
    await fs.promises.chmod(temporary, 0o700);
    await fs.promises.rename(temporary, executable);
  } finally {
    await fs.promises.rm(temporaryDirectory, { recursive: true, force: true });
  }
  await verifyVersion(executable, manifest.rcc.version);
  return { executable, storageRoot: paths.storageRoot, platform, version: manifest.rcc.version };
}

async function ensureJatRuntime(context, manifestSource, rccRuntime, options = {}) {
  const manifest = readManifest(manifestSource);
  const platform = options.platform || resolvePlatform();
  const jat = manifest.jat;
  const artifact = jat?.environment_artifact;
  const archivePin = artifact?.archive;
  if (!jat || typeof jat.git_sha !== "string" || !/^[0-9a-f]{40}$/.test(jat.git_sha)
    || !archivePin || typeof artifact.digest !== "string" || !/^sha256:[0-9a-f]{64}$/.test(artifact.digest)
    || typeof archivePin.asset !== "string" || typeof archivePin.url !== "string"
    || !archivePin.url.startsWith("https://") || typeof archivePin.sha256 !== "string"
    || !DIGEST.test(archivePin.sha256)
    || archivePin.size !== undefined && (!Number.isInteger(archivePin.size) || archivePin.size < 1)) {
    throw new Error("Josh Room runtime manifest is missing a valid JAT environment artifact pin");
  }
  if (!rccRuntime?.executable || !rccRuntime?.version) {
    throw new Error("managed RCC runtime is required for JAT acquisition");
  }
  const paths = privatePaths(context);
  await fs.promises.mkdir(paths.jatArtifactRoot, { recursive: true, mode: 0o700 });
  const archivePath = path.join(paths.jatArtifactRoot, archivePin.asset);
  const download = options.download || downloadFile;
  if (await isRegularFile(archivePath)) {
    if (archivePin.size !== undefined && (await fs.promises.stat(archivePath)).size !== archivePin.size) {
      throw new Error("cached JAT environment archive size mismatch");
    }
    const observed = await sha256File(archivePath);
    if (observed !== archivePin.sha256) {
      throw new Error("cached JAT environment archive checksum mismatch");
    }
  } else if (await pathExists(archivePath)) {
    throw new Error(`JAT environment archive is not a regular file: ${archivePath}`);
  } else {
    const temporaryDirectory = await fs.promises.mkdtemp(path.join(paths.jatArtifactRoot, ".download-"));
    const temporary = path.join(temporaryDirectory, archivePin.asset);
    try {
      await download(archivePin.url, temporary);
      if (archivePin.size !== undefined && (await fs.promises.stat(temporary)).size !== archivePin.size) {
        throw new Error("downloaded JAT environment archive size mismatch");
      }
      const observed = await sha256File(temporary);
      if (observed !== archivePin.sha256) {
        throw new Error(`downloaded JAT environment checksum mismatch: expected ${archivePin.sha256}, got ${observed}`);
      }
      await fs.promises.chmod(temporary, 0o600);
      await fs.promises.rename(temporary, archivePath);
    } finally {
      await fs.promises.rm(temporaryDirectory, { recursive: true, force: true });
    }
  }
  const ensureSource = options.ensureSource || ensureJatSource;
  const jatRoot = await ensureSource(context, jat, options);
  const environment = {
    ...process.env,
    ROBOCORP_HOME: paths.rccHome,
    RCC_HOLOTREE_MODE: "private",
  };
  const runJson = options.runJson || runJsonCommand;
  const acquired = await runJson(
    rccRuntime.executable,
    ["env", "acquire", "--archive", archivePath, "--permissive-local", "--json"],
    { cwd: paths.storageRoot, env: environment },
  );
  if (acquired.artifactDigest !== artifact.digest || acquired.verification?.valid !== true) {
    throw new Error("RCC did not validate the pinned JAT environment artifact");
  }
  const executed = await runJson(
    rccRuntime.executable,
    ["env", "exec", "--artifact", artifact.digest, "--permissive-local", "--json", "--", "hauler", "version"],
    { cwd: paths.storageRoot, env: environment },
  );
  if (executed.artifactDigest !== artifact.digest || executed.exitCode !== 0) {
    throw new Error("acquired JAT environment failed Hauler version verification");
  }
  return {
    artifact: artifact.digest,
    archive: archivePath,
    jatRoot,
    sourceSha: jat.git_sha,
  };
}

function runtimeEnvironment(context, runtime, workspace) {
  const paths = privatePaths(context);
  const values = runtime || {};
  return {
    RCC_HOLOTREE_MODE: "private",
    ROBOCORP_HOME: paths.rccHome,
    JOSH_ROOM_RCC_HOME: paths.rccHome,
    JOSH_ROOM_RCC_EXE: values.rccExecutable || "",
    JOSH_ROOM_CONTROLLER_ROOT: values.controllerRoot || paths.controllerRoot,
    JOSH_ROOM_JAT_ROOT: values.jatRoot || paths.jatRoot,
    JOSH_ROOM_JAT_ARTIFACT: values.jatArtifact || "",
    JOSH_ROOM_JAT_SHA: values.jatSourceSha || "",
    JOSH_ROOM_INSTANCE: paths.instanceRoot,
    JOSH_ROOM_CONFIG_DIR: paths.configRoot,
    XDG_RUNTIME_DIR: path.join(paths.runtimeRoot, "xdg-runtime"),
    JOSH_ROOM_EXTENSION_MODE: "1",
    ...(workspace ? { JOSH_ROOM_WORKSPACE_ROOT: path.resolve(workspace) } : {}),
  };
}

async function isRegularFile(filename) {
  try {
    const stat = await fs.promises.lstat(filename);
    return stat.isFile() && !stat.isSymbolicLink();
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

async function pathExists(filename) {
  try {
    await fs.promises.lstat(filename);
    return true;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

function sha256File(filename) {
  return new Promise((resolve, reject) => {
    const digest = crypto.createHash("sha256");
    const stream = fs.createReadStream(filename);
    stream.on("data", (chunk) => digest.update(chunk));
    stream.on("error", reject);
    stream.on("end", () => resolve(digest.digest("hex")));
  });
}

function downloadFile(url, destination, redirects = 0) {
  if (redirects > 5) return Promise.reject(new Error("runtime download redirected too many times"));
  let parsed;
  try {
    parsed = new URL(url);
  } catch (error) {
    return Promise.reject(new Error(`runtime download URL is invalid: ${error.message}`));
  }
  if (parsed.protocol !== "https:") return Promise.reject(new Error("runtime downloads require HTTPS"));
  const client = parsed.protocol === "https:" ? https : http;
  return new Promise((resolve, reject) => {
    const request = client.get(parsed, { headers: { "User-Agent": "Josh-Room-VSCode" } }, (response) => {
      if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
        response.resume();
        downloadFile(new URL(response.headers.location, parsed).toString(), destination, redirects + 1)
          .then(resolve, reject);
        return;
      }
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`runtime download returned HTTP ${response.statusCode}`));
        return;
      }
      const output = fs.createWriteStream(destination, { mode: 0o600 });
      response.pipe(output);
      output.on("finish", () => output.close(resolve));
      output.on("error", (error) => {
        output.destroy();
        reject(error);
      });
    });
    request.on("error", reject);
  });
}

function verifyRccVersion(executable, expected) {
  return new Promise((resolve, reject) => {
    const child = childProcess.spawn(executable, ["version"], {
      stdio: ["ignore", "pipe", "pipe"],
    });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk.toString(); });
    child.stderr.on("data", (chunk) => { output += chunk.toString(); });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0 || !output.includes(expected)) {
        reject(new Error(`managed RCC failed version verification for ${expected}`));
        return;
      }
      resolve();
    });
  });
}

function runJsonCommand(executable, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = childProcess.spawn(executable, args, {
      cwd: options.cwd,
      env: options.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk.toString(); });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.on("error", reject);
    child.on("close", (code) => {
      const start = stdout.indexOf("{");
      const end = stdout.lastIndexOf("}");
      if (code !== 0 && (start < 0 || end < start)) {
        reject(new Error(stderr || `managed RCC exited with status ${code}`));
        return;
      }
      if (start < 0 || end < start) {
        reject(new Error("managed RCC returned no JSON result"));
        return;
      }
      try {
        resolve(JSON.parse(stdout.slice(start, end + 1)));
      } catch (error) {
        reject(new Error(`managed RCC returned invalid JSON: ${error.message}`));
      }
    });
  });
}

async function ensureJatSource(context, jat, options = {}) {
  const paths = privatePaths(context);
  const source = jat.source_archive;
  if (!source || typeof source.asset !== "string" || typeof source.url !== "string"
    || !source.url.startsWith("https://") || typeof source.sha256 !== "string" || !DIGEST.test(source.sha256)) {
    throw new Error("Josh Room runtime manifest is missing a valid JAT source pin");
  }
  const target = path.join(paths.jatRoot, jat.git_sha);
  const marker = path.join(target, ".josh-room-source");
  if (await isRegularFile(marker) && (await fs.promises.readFile(marker, "utf8")).trim() === jat.git_sha) {
    return target;
  }
  if (await pathExists(target)) {
    throw new Error(`JAT source target is not a verified directory: ${target}`);
  }
  await fs.promises.mkdir(paths.jatRoot, { recursive: true, mode: 0o700 });
  const temporaryDirectory = await fs.promises.mkdtemp(path.join(paths.jatRoot, ".source-"));
  const archivePath = path.join(temporaryDirectory, source.asset);
  const download = options.download || downloadFile;
  try {
    await download(source.url, archivePath);
    if (await sha256File(archivePath) !== source.sha256) {
      throw new Error("downloaded JAT source checksum mismatch");
    }
    const staged = path.join(temporaryDirectory, "source");
    await extractGzipTar(archivePath, staged);
    await fs.promises.writeFile(path.join(staged, ".josh-room-source"), `${jat.git_sha}\n`, { mode: 0o600 });
    await fs.promises.rename(staged, target);
  } finally {
    await fs.promises.rm(temporaryDirectory, { recursive: true, force: true });
  }
  return target;
}

async function extractGzipTar(archivePath, destination) {
  const zlib = require("zlib");
  const compressed = await fs.promises.readFile(archivePath);
  const content = zlib.gunzipSync(compressed);
  await fs.promises.mkdir(destination, { recursive: true, mode: 0o700 });
  for (let offset = 0; offset + 512 <= content.length;) {
    const header = content.subarray(offset, offset + 512);
    if (header.every((byte) => byte === 0)) break;
    const name = header.subarray(0, 100).toString("utf8").replace(/\0.*$/, "");
    const prefix = header.subarray(345, 500).toString("utf8").replace(/\0.*$/, "");
    const member = prefix ? `${prefix}/${name}` : name;
    const sizeText = header.subarray(124, 136).toString("ascii").replace(/\0.*$/, "").trim();
    const size = sizeText ? parseInt(sizeText, 8) : 0;
    const type = header[156];
    if (!member || member.includes("\0") || path.posix.normalize(member).startsWith("../") || path.posix.isAbsolute(member)) {
      throw new Error("JAT source archive contains an unsafe path");
    }
    const relative = member.split("/").slice(1).join("/");
    if (relative) {
      const target = path.join(destination, relative);
      if (type === 53) {
        await fs.promises.mkdir(target, { recursive: true, mode: 0o700 });
      } else if (type === 0 || type === 48) {
        await fs.promises.mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
        await fs.promises.writeFile(target, content.subarray(offset + 512, offset + 512 + size), { mode: 0o600 });
      } else {
        throw new Error("JAT source archive contains an unsupported entry type");
      }
    }
    offset += 512 + Math.ceil(size / 512) * 512;
  }
}

module.exports = {
  ensureManagedRcc,
  ensureJatRuntime,
  ensureJatSource,
  privatePaths,
  readManifest,
  resolvePlatform,
  runtimeEnvironment,
  sha256File,
};
