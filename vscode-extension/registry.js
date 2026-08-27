const fs = require("fs");
const http = require("http");

const REGISTRY_URL = "http://127.0.0.1:5000";

function probeRegistry() {
  return new Promise((resolve, reject) => {
    const request = http.get(`${REGISTRY_URL}/v2/_catalog`, { timeout: 1000 }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => { body += chunk; });
      response.on("end", () => {
        if (response.statusCode !== 200) {
          reject(new Error(`registry returned HTTP ${response.statusCode}`));
          return;
        }
        try {
          resolve(JSON.parse(body));
        } catch (error) {
          reject(new Error(`registry returned invalid JSON: ${error.message}`));
        }
      });
    });
    request.on("timeout", () => request.destroy(new Error("registry probe timed out")));
    request.on("error", reject);
  });
}

async function waitForRegistry(
  probe = probeRegistry,
  { timeoutMs = 120000, intervalMs = 500 } = {},
) {
  const deadline = Date.now() + timeoutMs;
  let lastError = new Error("registry did not become ready");
  do {
    try {
      return await probe();
    } catch (error) {
      lastError = error;
    }
    if (Date.now() >= deadline) break;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  } while (true);
  throw new Error(`Hauler registry did not become ready: ${lastError.message}`);
}

function cleanLogLine(line) {
  return String(line).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "").trim();
}

function stageForLog(line) {
  const normalized = cleanLogLine(line).toLowerCase();
  if (normalized.includes("listening on") && normalized.includes(":5000")) return "Registry is ready";
  if (normalized.includes("starting registry on port")) return "Starting read-only registry";
  if (normalized.includes("copied artifacts to")) return "Loading registry images";
  if (normalized.includes("hauler/") && normalized.includes(":latest")) return "Inspecting haul contents";
  if (normalized.includes("restore space from library") || normalized.includes("holotree")) {
    return "Preparing RCC environment";
  }
  return undefined;
}

function followLogFile(logPath, onLine, { intervalMs = 100 } = {}) {
  let previous = "";
  try {
    previous = fs.readFileSync(logPath, "utf8");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  let pending = "";
  const poll = () => {
    let current;
    try {
      current = fs.readFileSync(logPath, "utf8");
    } catch (error) {
      if (error.code === "ENOENT") return;
      onLine(`Unable to read ${logPath}: ${error.message}`);
      return;
    }
    if (current === previous) return;
    const added = current.startsWith(previous) ? current.slice(previous.length) : current;
    previous = current;
    const lines = (pending + added).split(/\r?\n/);
    pending = lines.pop() || "";
    for (const line of lines) {
      const cleaned = cleanLogLine(line);
      if (cleaned) onLine(cleaned);
    }
  };
  const timer = setInterval(poll, intervalMs);
  timer.unref?.();
  return {
    dispose: () => {
      poll();
      clearInterval(timer);
    },
  };
}

module.exports = { REGISTRY_URL, cleanLogLine, followLogFile, probeRegistry, stageForLog, waitForRegistry };

const PROVIDER_LABELS = {
  r2: "Cloudflare R2",
  minio: "MinIO",
  local: "Local Object Store",
};
const { connectionRecords, dimensionConnection } = require("./provider");

function providerKey(value) {
  const normalized = String(value || "r2").trim().toLowerCase();
  if (normalized.includes("cloudflare") || normalized === "s3") return "r2";
  if (normalized.includes("minio")) return "minio";
  if (normalized.includes("local")) return "local";
  return normalized || "r2";
}

function providerLabel(value) {
  const key = providerKey(value);
  return PROVIDER_LABELS[key] || String(value || key).trim() || "Storage Provider";
}

function connectionLabel(state) {
  if (state === "connected") return "✓ Connected";
  if (state === "expired") return "⚠ Session expired";
  if (state === "disconnected") return "⚠ Disconnected";
  return "⚠ Not connected";
}

function dimensionDisplayName(dimension, counts) {
  const id = dimension && (dimension.id || dimension.dimension_id);
  const configuredName = dimension && (dimension.display_name || dimension.name || id);
  const name = id === "r2" && configuredName === "Cloudflare R2" ? "Default" : configuredName;
  return counts.get(name) > 1 ? `${name} (${id})` : name;
}

function records(value, idField = "id") {
  if (Array.isArray(value)) return value.map((item) => ({ ...item }));
  if (!value || typeof value !== "object") return [];
  return Object.entries(value).map(([id, item]) => ({
    ...(item && typeof item === "object" ? item : {}),
    [idField]: item?.[idField] || id,
  }));
}

function snapshotRecords(project) {
  const source = project?.snapshots || project?.jats || [];
  return records(source, "snapshot_id").map((snapshot) => ({
    ...snapshot,
    id: snapshot.snapshot_id || snapshot.id,
    display_name: snapshot.display_name || snapshot.name || snapshot.snapshot_id || snapshot.id,
  }));
}

function buildProviderTree(catalog = {}) {
  const dimensions = records(catalog.dimensions);
  const connections = connectionRecords(catalog);
  const fallbackProjects = records(catalog.projects, "id");
  const sourceDimensions = dimensions.length ? dimensions : [{
    id: catalog.dimension_id || "r2",
    display_name: catalog.dimension_name || "Default",
    provider: catalog.provider || "r2",
    synthetic: !catalog.dimension_id,
    projects: fallbackProjects,
  }];
  const dimensionNames = new Map();
  for (const dimension of sourceDimensions) {
    const name = dimension.display_name || dimension.name || dimension.id || dimension.dimension_id;
    dimensionNames.set(name, (dimensionNames.get(name) || 0) + 1);
  }
  const grouped = new Map();
  const connectionNodes = new Map();
  for (const dimension of sourceDimensions) {
    const displayName = dimensionDisplayName(dimension, dimensionNames);
    const providerId = providerKey(dimension.provider || dimension.provider_id || dimension.kind);
    if (!grouped.has(providerId)) {
      grouped.set(providerId, {
        kind: "provider",
        id: providerId,
        label: providerLabel(dimension.provider || providerId),
        provider: providerId,
        children: [],
      });
    }
    const projects = records(
      Object.prototype.hasOwnProperty.call(dimension, "projects")
        ? dimension.projects
        : Object.prototype.hasOwnProperty.call(dimension, "rooms")
          ? dimension.rooms
          : dimensions.length ? [] : fallbackProjects,
      "id",
    );
    const roomNodes = projects.map((project) => ({
      kind: "room",
      id: project.id || project.project_id,
      label: `${project.display_name || project.name || project.id || project.project_id} · ${displayName}`,
      project,
      dimension,
      children: snapshotRecords(project).map((snapshot) => ({
        kind: "jat",
        id: snapshot.id,
        label: snapshot.display_name,
        snapshot,
        project,
        dimension,
      })),
    }));
    const connection = dimensionConnection(dimension, connections);
    const connectionId = connection.id || connection.connection_id;
    const connectionState = dimension.connection_state || connection.connection_state
      || (connection.auth_state === "disconnected" ? "disconnected" : connection.auth_state === "expired" ? "expired" : undefined)
      || (providerId === "r2" && !dimensions.length ? catalog.auth_state : undefined)
      || "connected";
    dimension.connection = connection;
    const loadError = dimension.load_error || dimension.error;
    const dimensionNode = {
      kind: "dimension",
      id: dimension.id || dimension.dimension_id,
      label: displayName,
      description: loadError?.description || "",
      provider: providerId,
      state: loadError ? "error" : connectionState,
      dimension,
      children: loadError ? [{
        kind: "dimension-error",
        id: "error",
        label: loadError.label || "Could not load bucket — Retry",
        description: loadError.description || "Refresh this storage or edit its provider connection.",
        error: loadError,
        dimension,
        connection,
      }] : connectionState === "connected" ? roomNodes : [],
    };
    let connectionNode = connectionNodes.get(`${providerId}:${connectionId}`);
    if (!connectionNode) {
      connectionNode = {
        kind: "connection",
        id: connectionId,
        label: providerId === "r2"
          ? connectionLabel(connectionState)
          : connectionState === "connected"
            ? connection.display_name || connection.name || connection.label || connection.endpoint || connectionId
            : connectionLabel(connectionState),
        description: providerId === "r2"
          ? connectionState === "expired" ? "Reconnect Cloudflare" : connectionState === "connected" ? "Connected" : "Connect Cloudflare"
          : connectionState === "expired" || connectionState === "disconnected" ? "Reconnect" : connectionState === "connected" ? "Connected" : "Connect",
        state: connectionState,
        provider: providerId,
        connection,
        dimension,
        children: [],
      };
      connectionNodes.set(`${providerId}:${connectionId}`, connectionNode);
      grouped.get(providerId).children.push(connectionNode);
    }
    connectionNode.children.push(dimensionNode);
  }
  return [...grouped.values()];
}

function dimensionArgs(args, dimension) {
  const original = [...args];
  if (!dimension) return original;
  const routed = [];
  let found = false;
  for (let index = 0; index < original.length; index += 1) {
    const arg = original[index];
    if (arg === "--dimension") {
      found = true;
      index += 1;
      routed.push("--dimension", String(dimension));
      continue;
    }
    if (arg.startsWith("--dimension=")) {
      found = true;
      routed.push("--dimension", String(dimension));
      continue;
    }
    routed.push(arg);
  }
  if (!found) routed.push("--dimension", String(dimension));
  return routed;
}

Object.assign(module.exports, {
  buildProviderTree,
  dimensionArgs,
  flattenDimensionRooms,
  dimensionLabel: providerLabel,
  connectionLabel,
  providerKey,
  providerLabel,
});

function routedDimensionArgs(args, dimension) {
  const original = [...args];
  const routed = [];
  let found = false;
  for (let index = 0; index < original.length; index += 1) {
    const arg = original[index];
    if (arg === "--backend") {
      index += 1;
      continue;
    }
    if (arg.startsWith("--backend=")) continue;
    if (arg === "--dimension") {
      found = true;
      index += 1;
      routed.push("--dimension", String(dimension));
      continue;
    }
    if (arg.startsWith("--dimension=")) {
      found = true;
      routed.push("--dimension", String(dimension));
      continue;
    }
    routed.push(arg);
  }
  if (dimension && !found) routed.push("--dimension", String(dimension));
  return routed;
}

function snapshotCopyArgs(source, target) {
  const sourceDimension = source && (source.dimension_id || source.source_dimension || source.dimension);
  const destinationDimension = target && (target.dimension_id || target.destination_dimension || target.id || target.dimension);
  const destinationRoom = target && (target.destination_room || target.room_id || (target.kind === "room" ? target.id : undefined));
  if (source && source.kind === "jat") {
    return [
      "snapshot", "copy", source.project && (source.project.id || source.project.project_id),
      "--snapshot", source.id || (source.snapshot && source.snapshot.snapshot_id),
      "--source-dimension", sourceDimension,
      "--destination-dimension", destinationDimension,
      "--destination-room", destinationRoom,
    ];
  }
  return [
    "snapshot", "copy", "--source-folder", source && source.path,
    "--destination-dimension", destinationDimension,
    "--destination-room", destinationRoom,
  ];
}

module.exports.dimensionArgs = routedDimensionArgs;
module.exports.snapshotCopyArgs = snapshotCopyArgs;

function identityValue(value) {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return undefined;
  return value.id || value.dimension_id || value.project_id;
}

function exactSnapshotCopyArgs(source, target) {
  const sourceDimension = identityValue(
    source && (source.source_dimension || source.dimension_id || source.dimension),
  );
  const targetDimension = identityValue(
    target && (target.destination_dimension || target.dimension_id
      || (target.destination_room ? target : undefined)
      || (target.kind === "dimension" ? target : target.dimension)),
  );
  const destinationRoom = target && (
    target.destination_room || target.room_id || (target.kind === "room" ? target.id : undefined)
  );
  if (source && source.kind === "jat") {
    return [
      "snapshot", "copy", identityValue(source.project),
      "--snapshot", source.id || source.snapshot && source.snapshot.snapshot_id,
      "--source-dimension", sourceDimension,
      "--destination-dimension", targetDimension,
      "--destination-room", destinationRoom,
    ];
  }
  return [
    "snapshot", "copy", "--source-folder", source && source.path,
    "--destination-dimension", targetDimension, "--destination-room", destinationRoom,
  ];
}

module.exports.snapshotCopyArgs = exactSnapshotCopyArgs;

function flattenDimensionRooms(catalog = {}) {
  const dimensions = records(catalog.dimensions);
  if (!dimensions.length) {
    const dimension = catalog.dimension_id
      ? { id: catalog.dimension_id, display_name: catalog.dimension_name, provider: catalog.provider }
      : undefined;
    return records(catalog.projects, "id").map((project) => decorateRoom(project, dimension));
  }
  const dimensionNames = new Map();
  for (const dimension of dimensions) {
    const name = dimension.display_name || dimension.name || dimension.id || dimension.dimension_id;
    dimensionNames.set(name, (dimensionNames.get(name) || 0) + 1);
  }
  return dimensions.flatMap((dimension) => {
    const displayName = dimensionDisplayName(dimension, dimensionNames);
    const source = Object.prototype.hasOwnProperty.call(dimension, "projects")
      ? dimension.projects
      : dimension.rooms;
    return records(source, "id").map((project) => decorateRoom(project, dimension, displayName));
  });
}

function decorateRoom(project, dimension, dimensionDisplayNameValue) {
  const id = project.id || project.project_id;
  return {
    ...project,
    id,
    project_id: project.project_id || id,
    ...(project.snapshots ? {} : project.jats ? { snapshots: project.jats } : {}),
    dimension_id: dimension && (dimension.id || dimension.dimension_id),
    dimension_display_name: dimensionDisplayNameValue,
    dimension,
  };
}
