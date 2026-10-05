"use strict";

const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const {
  compareRestoredFile,
  jatRestoredWorkspace,
} = require("./managed_runtime_acceptance_helpers");

test("JAT Restore workspace includes the Build source basename beneath payload/workspace", () => {
  const destination = path.resolve("synthetic-clean-room");
  const result = {
    operation: "restore",
    success: true,
    exit_status: 0,
    payload_path: destination,
  };

  assert.equal(
    jatRestoredWorkspace(result, destination, "workspace"),
    path.join(destination, "workspace", "workspace"),
  );
  assert.equal(jatRestoredWorkspace(result, destination, "../outside"), null);
  assert.equal(jatRestoredWorkspace({ ...result, payload_path: `${destination}-other` }, destination, "workspace"), null);
  assert.equal(jatRestoredWorkspace({ ...result, success: false }, destination, "workspace"), null);
  const windowsPayload = "C:\\Temp\\Clean-Restore";
  assert.equal(
    jatRestoredWorkspace({ ...result, payload_path: windowsPayload }, "c:\\temp\\clean-restore", "workspace", "win32"),
    path.win32.join(windowsPayload, "workspace", "workspace"),
  );
});

test("JAT restore file check compares bytes and OS-native mode semantics", () => {
  const bytes = Buffer.from("synthetic readonly file\n");
  const unchanged = compareRestoredFile({
    sourceBytes: bytes,
    restoredBytes: Buffer.from(bytes),
    sourceMode: 0o444,
    restoredMode: 0o444,
    platform: "linux-x64",
  });
  assert.equal(unchanged.bytes_match, true);
  assert.equal(unchanged.mode_match, true);

  const windowsReadonly = compareRestoredFile({
    sourceBytes: bytes,
    restoredBytes: Buffer.from(bytes),
    sourceMode: 0o444,
    restoredMode: 0o555,
    platform: "win32-x64",
  });
  assert.equal(windowsReadonly.bytes_match, true);
  assert.equal(windowsReadonly.mode_match, true);
  assert.equal(windowsReadonly.source_mode, "read-only");
  assert.equal(windowsReadonly.restored_mode, "read-only");

  const writableRestore = compareRestoredFile({
    sourceBytes: bytes,
    restoredBytes: Buffer.from(bytes),
    sourceMode: 0o444,
    restoredMode: 0o666,
    platform: "win32-x64",
  });
  assert.equal(writableRestore.mode_match, false);

  const changedBytes = compareRestoredFile({
    sourceBytes: bytes,
    restoredBytes: Buffer.from("changed"),
    sourceMode: 0o444,
    restoredMode: 0o444,
    platform: "linux-x64",
  });
  assert.equal(changedBytes.bytes_match, false);
  assert.equal(changedBytes.mode_match, true);

  const missingFile = compareRestoredFile({
    sourceBytes: bytes,
    restoredBytes: Buffer.alloc(0),
    sourceMode: 0o444,
    restoredMode: 0,
    restoredExists: false,
    platform: "win32-x64",
  });
  assert.equal(missingFile.bytes_match, false);
  assert.equal(missingFile.mode_match, false);
});
