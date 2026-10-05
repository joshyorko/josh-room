"use strict";

const crypto = require("crypto");
const path = require("path");

function jatRestoredWorkspace(result, requestedDestination, platform = process.platform) {
  if (!result || result.operation !== "restore" || result.success !== true || result.exit_status !== 0
    || typeof result.payload_path !== "string") return null;
  const windows = platform === "win32" || platform === "win32-x64";
  const pathApi = windows ? path.win32 : path.posix;
  const payloadPath = pathApi.resolve(result.payload_path);
  const expectedPath = pathApi.resolve(requestedDestination);
  const samePath = windows
    ? payloadPath.toLowerCase() === expectedPath.toLowerCase()
    : payloadPath === expectedPath;
  return samePath ? pathApi.join(payloadPath, "workspace") : null;
}

function compareRestoredFile({ sourceBytes, restoredBytes, sourceMode, restoredMode, restoredExists = true, platform = process.platform }) {
  const source = Buffer.from(sourceBytes);
  const restored = Buffer.from(restoredBytes);
  const windows = platform === "win32" || platform === "win32-x64";
  const modeSignature = (mode) => windows
    ? ((mode & 0o222) === 0 ? "read-only" : "writable")
    : (mode & 0o777).toString(8).padStart(4, "0");
  const sourceModeSignature = modeSignature(sourceMode);
  const restoredModeSignature = modeSignature(restoredMode);
  return {
    restored_exists: restoredExists,
    bytes_match: restoredExists && source.equals(restored),
    source_size: source.length,
    restored_size: restored.length,
    source_sha256: crypto.createHash("sha256").update(source).digest("hex"),
    restored_sha256: crypto.createHash("sha256").update(restored).digest("hex"),
    source_mode: sourceModeSignature,
    restored_mode: restoredModeSignature,
    mode_semantics: windows ? "windows-readonly-bit" : "posix-permission-bits",
    mode_match: restoredExists && sourceModeSignature === restoredModeSignature,
  };
}

module.exports = { compareRestoredFile, jatRestoredWorkspace };
