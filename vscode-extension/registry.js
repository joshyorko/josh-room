const fs = require("fs");
const http = require("http");

function serveUrl(value) {
  if (typeof value !== "string" || !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(value)) {
    throw new Error("JAT reported an invalid or non-loopback Serve endpoint");
  }
  const url = new URL(value);
  if (Number(url.port) > 65535) throw new Error("JAT reported an invalid Serve port");
  return url.origin;
}

function validateServeStartup(value, requestedMode = "auto") {
  const serve = value?.serve;
  if (value?.schema_version !== 1 || value.state !== "ready" || value.operation !== "serve"
      || !Number.isSafeInteger(value.pid) || value.pid <= 0
      || !["files", "registry", "both"].includes(serve?.mode)
      || (requestedMode !== "auto" && requestedMode !== serve.mode)) {
    throw new Error("JAT returned an incompatible Serve startup receipt");
  }
  for (const [key, active] of [["fileserver", serve.mode !== "registry"], ["registry", serve.mode !== "files"]]) {
    if (active) {
      serveUrl(serve[`${key}_url`]);
      const allowedBindings = key === "fileserver" ? ["loopback", "all-interfaces"] : ["loopback"];
      if (!allowedBindings.includes(serve[`${key}_bind`])) throw new Error("JAT reported an unsupported Serve listener scope");
    } else if (serve[`${key}_url`] !== null || serve[`${key}_bind`] !== null) {
      throw new Error("JAT reported an unexpected Serve endpoint");
    }
  }
  if (serve.fileserver_url && serve.fileserver_url === serve.registry_url) throw new Error("JAT Serve endpoints collide");
  return { ...serve, session_id: value.session_id, pid: value.pid };
}

function readServeJson(file, limit = 1024 * 1024) {
  try {
    const info = fs.lstatSync(file);
    if (!info.isFile() || info.size > limit) throw new Error("Invalid Serve receipt file");
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    if (error.code === "ENOENT") return undefined;
    throw error;
  }
}

async function waitForServeStartup(startupFile, {
  mode = "auto", timeoutMs = 120000, intervalMs = 100, checkStopped = () => {},
} = {}) {
  const deadline = Date.now() + timeoutMs;
  do {
    checkStopped();
    const value = readServeJson(startupFile);
    if (value) return validateServeStartup(value, mode);
    if (Date.now() >= deadline) break;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  } while (true);
  throw new Error("JAT did not report selected endpoints; the acquired runtime may lack Serve startup support");
}

function verifiedPull(serve, image) {
  if (serve.registry_bind !== "loopback" || !serve.session_id || !serve.registry_url) return;
  const reference = image.pull_reference;
  if (typeof reference !== "string" || typeof image.digest !== "string") return;
  const authority = serve.registry_url.slice("http://".length);
  const parts = reference.split("/");
  if (parts.shift() !== authority) return;
  const pieces = parts.join("/").split("@");
  if (pieces.length !== 2 || pieces[1] !== image.digest || !/^sha256:[0-9a-f]{64}$/.test(image.digest)) return;
  if (!pieces[0].split("/").every(part => /^[a-z0-9]+(?:(?:[.]|_{1,2}|-+)[a-z0-9]+)*$/.test(part))) return;
  // JS end anchors can accept a final newline. Clipboard commands must be one shell argument.
  if (/[^a-z0-9:@/._-]/.test(reference)) return;
  return reference;
}

function probeRegistry(endpoint) {
  const registryUrl = serveUrl(endpoint);
  return new Promise((resolve, reject) => {
    const request = http.get(`${registryUrl}/v2/_catalog`, { timeout: 1000 }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
        if (body.length > 1048576) response.destroy(new Error("registry catalog exceeds size limit"));
      });
      response.on("error", reject);
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

function probeFileserver(endpoint) {
  const fileserverUrl = serveUrl(endpoint);
  // Any HTTP response proves the JAT fileserver is accepting connections.
  return new Promise((resolve, reject) => {
    const request = http.get(`${fileserverUrl}/`, { timeout: 1000 }, (response) => {
      response.resume();
      resolve({ fileserver: true });
    });
    request.on("timeout", () => request.destroy(new Error("fileserver probe timed out")));
    request.on("error", reject);
  });
}

async function waitForFileserver(
  probe = probeFileserver,
  { timeoutMs = 120000, intervalMs = 500 } = {},
) {
  const deadline = Date.now() + timeoutMs;
  let lastError = new Error("fileserver did not become ready");
  do {
    try {
      return await probe();
    } catch (error) {
      lastError = error;
    }
    if (Date.now() >= deadline) break;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  } while (true);
  throw new Error(`JAT fileserver did not become ready: ${lastError.message}`);
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
  if (normalized.includes("listening on")) return "Serve listener is ready";
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

module.exports = {
  serveUrl,
  readServeJson,
  validateServeStartup,
  waitForServeStartup,
  verifiedPull,
  cleanLogLine,
  followLogFile,
  probeFileserver,
  probeRegistry,
  stageForLog,
  waitForFileserver,
  waitForRegistry,
};

const PROVIDER_LABELS = {
  r2: "Cloudflare R2",
  minio: "MinIO",
  s3: "Amazon S3 / S3-compatible",
  local: "Local Object Store",
};
const { connectionRecords, dimensionConnection } = require("./provider");

function providerKey(value) {
  const normalized = String(value || "r2").trim().toLowerCase();
  if (normalized.includes("cloudflare")) return "r2";
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

function encryptionAction(state) {
  if (state === "uninitialized" || state === "insecure") {
    return { command: "joshRoom.initializeEncryption", title: "Initialize Encryption" };
  }
  if (state === "legacy") {
    return { command: "joshRoom.migrateEncryption", title: "Migrate Encryption" };
  }
  if (state === "resumable") {
    return { command: "joshRoom.resumeEncryption", title: "Resume Encryption Migration" };
  }
  if (state === "failed") {
    return { command: "joshRoom.refresh", title: "Retry Encryption Status" };
  }
  return undefined;
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

function catalogNodes(dimension) {
  const parents = records(dimension.catalogs, "parent_room_id").flatMap(catalog => {
    if (!/^[a-f0-9]{32}$/.test(catalog.namespace) || !catalog.parent_room_id) return [];
    const points = records(catalog.recovery_points, "snapshot_id").filter(point => /^jat-[a-z0-9-]+$/.test(point.snapshot_id)
      && Number.isFinite(Date.parse(point.created_at)))
      .sort((left, right) => Date.parse(right.created_at) - Date.parse(left.created_at));
    const retirement = Array.isArray(catalog.child_rooms);
    if (!points.length && !retirement) return [];
    const parent = {
      kind: "catalog-parent", id: `${catalog.namespace}:${catalog.parent_room_id}`,
      label: catalog.display_name || "Catalog Room",
      description: points.length ? new Date(points[0].created_at).toLocaleString() : "No recovery points",
      catalog, dimension, retirement,
    };
    parent.children = points.map(point => ({
      kind: "catalog-point", id: `${parent.id}:${point.snapshot_id}`,
      label: new Date(point.created_at).toLocaleString(),
      description: `${point.child_count} child ${point.child_count === 1 ? "Room" : "Rooms"}`,
      catalog, point, dimension, retirement,
    }));
    const pinnedByPath = new Map();
    for (const point of points) {
      for (const child of Array.isArray(point.children) ? point.children : []) {
        if (typeof child === "string" && !pinnedByPath.has(child)) pinnedByPath.set(child, point);
      }
    }
    const childRows = records(catalog.child_rooms, "room_id").map(room => {
      const point = pinnedByPath.get(room.path);
      pinnedByPath.delete(room.path);
      const count = room.recovery_points.length;
      return {
        kind: "catalog-child-room", id: `${parent.id}:room:${room.room_id}`, label: room.path,
        description: `${count} recovery ${count === 1 ? "point" : "points"}`,
        catalog, dimension, room, point, child: room.path,
        children: room.recovery_points.map(point => ({
          kind: "catalog-child-point", id: `${parent.id}:${room.room_id}:${point.snapshot_id}`,
          label: new Date(point.created_at).toLocaleString(), description: point.pinned ? "Pinned" : "",
          catalog, dimension, room, point,
        })),
      };
    });
    for (const [child, point] of pinnedByPath) {
      childRows.push({ kind: "catalog-child", id: `${parent.id}:child:${child}`,
        label: child, catalog, point, dimension, child });
    }
    if (childRows.length) parent.children.push({
      kind: "catalog-child-history", id: `${parent.id}:history`, label: "Child Rooms",
      description: String(childRows.length), children: childRows,
    });
    return [parent];
  });
  if (dimension.catalog_discovery_partial) parents.push({
    kind: "catalog-partial", id: "catalog-partial", label: "Catalog discovery incomplete", dimension,
    description: "Some Catalogs could not be checked",
  });
  return parents;
}

function buildProviderTree(catalog = {}) {
  const dimensions = records(catalog.dimensions);
  const connections = connectionRecords(catalog);
  const fallbackProjects = records(catalog.projects, "id");
  const sourceDimensions = dimensions.length ? dimensions : connections.length || catalog.offer_synthetic_r2 === false ? [] : [{
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
      label: project.display_name || project.name || project.id || project.project_id,
      project,
      dimension,
      children: snapshotRecords(project).sort((left, right) => {
        if (left.id === project.latest) return right.id === project.latest ? 0 : -1;
        if (right.id === project.latest) return 1;
        const leftDate = Date.parse(left.created_at) || 0;
        const rightDate = Date.parse(right.created_at) || 0;
        return rightDate - leftDate || String(left.id).localeCompare(String(right.id));
      }).map((snapshot) => ({
        kind: "jat",
        id: snapshot.id,
        label: snapshot.created_at && !Number.isNaN(Date.parse(snapshot.created_at))
          ? new Date(snapshot.created_at).toLocaleString() : snapshot.display_name && snapshot.display_name !== snapshot.id ? snapshot.display_name : "Recovery point",
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
    const declaredEncryptionState = dimension.encryption_state || dimension.encryptionState || dimension.state;
    const encryptionState = ["ready", "uninitialized", "legacy", "resumable", "insecure", "failed"].includes(declaredEncryptionState)
      ? declaredEncryptionState : undefined;
    dimension.connection = connection;
    const loadError = dimension.load_error || dimension.error;
    const loadErrorState = ["ready", "uninitialized", "legacy", "resumable", "insecure", "failed"].includes(loadError?.state)
      ? loadError.state
      : loadError?.action === "migrate" ? "legacy" : loadError?.action === "resume" ? "resumable" : undefined;
    const effectiveEncryptionState = loadErrorState || encryptionState;
    const dimensionNode = {
      kind: "dimension",
      id: dimension.id || dimension.dimension_id,
      label: displayName,
      description: loadError?.description || "",
      provider: providerId,
      state: loadError ? effectiveEncryptionState || "error" : effectiveEncryptionState || connectionState,
      encryption_state: effectiveEncryptionState,
      action: encryptionAction(loadError ? loadErrorState || "failed" : encryptionState),
      dimension,
      children: loadError ? [{
        kind: "dimension-error",
        id: "error",
        label: loadError.label || "Could not load bucket — Retry",
        description: loadError.description || "Refresh this storage or edit its provider connection.",
        error: loadError,
        dimension,
        connection,
      }] : connectionState === "connected" ? [...catalogNodes(dimension), ...roomNodes] : [],
    };
    let connectionNode = connectionNodes.get(`${providerId}:${connectionId}`);
    if (!connectionNode) {
      connectionNode = {
        kind: "connection",
        id: connectionId,
        label: connection.display_name || connection.name || connection.label || connection.endpoint || (providerId === "r2" ? "Cloudflare" : connectionId),
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
  for (const connection of connections) {
    const connectionId = connection.id || connection.connection_id;
    const providerId = providerKey(connection.provider);
    if (!connectionId || connectionNodes.has(`${providerId}:${connectionId}`)) continue;
    if (!grouped.has(providerId)) {
      grouped.set(providerId, {
        kind: "provider",
        id: providerId,
        label: providerLabel(connection.provider || providerId),
        provider: providerId,
        children: [],
      });
    }
    const connectionState = connection.connection_state
      || (connection.auth_state === "disconnected" ? "disconnected" : connection.auth_state === "expired" ? "expired" : "connected");
    grouped.get(providerId).children.push({
      kind: "connection",
      id: connectionId,
      label: connection.display_name || connection.name || connection.label || connection.endpoint || (providerId === "r2" ? "Cloudflare" : connectionId),
      description: connectionState === "expired" || connectionState === "disconnected"
        ? "Reconnect" : connectionState === "connected" ? "Connected" : "Connect",
      state: connectionState,
      provider: providerId,
      connection,
      children: [],
    });
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
  encryptionAction,
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
