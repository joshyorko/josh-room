"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { isRoomMarker } = require("./dirty");

// Only open folders with a corroborated Room binding are eligible. Never use cwd.
function roomViewTargets(folders = []) {
  return folders.flatMap(folder => {
    if (folder.uri.scheme !== "file") return [];
    const openedRoot = path.resolve(folder.uri.fsPath);
    let fd;
    try {
      const metadata = fs.lstatSync(openedRoot);
      if (!metadata.isDirectory() || metadata.isSymbolicLink()) return [];
      const root = fs.realpathSync(openedRoot);
      const identity = fs.statSync(root, { bigint: true });
      fd = fs.openSync(path.join(root, ".josh-room.json"), fs.constants.O_RDONLY
        | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
      const info = fs.fstatSync(fd);
      if (!info.isFile() || info.nlink !== 1 || info.size > 65536) return [];
      const bytes = Buffer.alloc(info.size + 1);
      let offset = 0;
      while (offset < bytes.length) {
        const count = fs.readSync(fd, bytes, offset, bytes.length - offset);
        if (!count) break;
        offset += count;
      }
      if (offset !== info.size) return [];
      const marker = JSON.parse(bytes.subarray(0, info.size).toString("utf8"));
      const digest = crypto.createHash("sha256").update(root).digest("hex");
      if (!isRoomMarker(marker) || marker.format_version === 1 || marker.workspace_path_sha256 !== digest) return [];
      const binding = Object.freeze({ room_id: marker.project_id, dimension_id: marker.dimension_id,
        encryption_domain_id: marker.encryption_domain_id || null,
        composition_namespace: marker.composition_namespace || null,
        device: String(identity.dev), inode: String(identity.ino) });
      return [{ root, marker, binding, label: marker.display_name, description: `${marker.dimension_id} · ${marker.project_id}` }];
    } catch { return []; }
    finally { if (fd !== undefined) fs.closeSync(fd); }
  });
}

function matchingRoomViewTargets(targets, item) {
  if (!item) return targets;
  if (item.kind && !["room", "current-room"].includes(item.kind)) return [];
  const project = item.project || item;
  const dimension = item.dimension || project.dimension;
  const dimensionId = project.dimension_id || dimension?.id || dimension?.dimension_id;
  const id = project.project_id || project.id;
  if (!id || !dimensionId) return [];
  return targets.filter(({ marker }) => marker.project_id === id && marker.dimension_id === dimensionId);
}

module.exports = { roomViewTargets, matchingRoomViewTargets };
