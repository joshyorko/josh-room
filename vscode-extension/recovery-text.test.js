"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { decodePreview, showTextPreview, documents, metadataOnly } = require("./recovery-text");
const { browseRecovery } = require("./recovery-actions");
function mock() {
  let provider, closed;
  const calls = [];
  const vscode = {
    Uri: { parse: value => ({ toString: () => value }) },
    workspace: { isTrusted: true,
      registerTextDocumentContentProvider: (_scheme, value) => { provider = value; return { dispose() {} }; },
      onDidCloseTextDocument: callback => { closed = callback; return { dispose() {} }; },
    },
    commands: { executeCommand: async (...args) => calls.push(args) },
  };
  return { vscode, calls, text: uri => provider.provideTextDocumentContent(uri), close: uri => closed({ uri }) };
}
function envelope(text) {
  const bytes = Buffer.from(text, "utf8");
  return { state: "available", encoding: "base64-utf8", size: bytes.length,
    sha256: crypto.createHash("sha256").update(bytes).digest("hex"), data: bytes.toString("base64") };
}

test("preview to actual CLI serialization to native diff preserves whitespace and Unicode", async () => {
  const original = 'def greeting():\r\n\treturn "你好  world"\n\n';
  const root = path.resolve(__dirname, "..");
  const executable = path.join(root, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const child = spawnSync(fs.existsSync(executable) ? executable : "python3", ["-c", [
    "import json,sys,base64,hashlib",
    "from josh_room.recovery_text import text_preview",
    "from josh_room.cli import _bounded_json_result",
    "data=json.load(sys.stdin).encode('utf-8')",
    "preview=text_preview('code.py',data)",
    "print(json.dumps(_bounded_json_result({'text_preview':preview,'sha256':hashlib.sha256(data).hexdigest()})))",
  ].join("\n")], { cwd: root, env: { ...process.env, PYTHONPATH: path.join(root, "src") }, input: JSON.stringify(original), encoding: "utf8" });
  assert.equal(child.status, 0, child.stderr);
  const serialized = JSON.parse(child.stdout);
  assert.equal(decodePreview(serialized.text_preview, serialized.sha256), original);
  const fixture = mock();
  const result = { text_diff: "available", before: { sha256: serialized.sha256 }, after: { sha256: serialized.sha256 },
    before_text_preview: serialized.text_preview, after_text_preview: serialized.text_preview };
  assert.equal(await showTextPreview(fixture.vscode, result, "private-name.py", "synthetic point"), true);
  const [command, left, right] = fixture.calls[0];
  assert.equal(command, "vscode.diff");
  assert.equal(fixture.text(left), original);
  assert.equal(fixture.text(right), original);
  assert.ok(!left.toString().includes("private-name"));
  assert.ok(!left.toString().includes(original));
  fixture.close(left);
  assert.throws(() => fixture.text(left), /expired/);
  documents(fixture.vscode).dispose();
});

test("omitted oversized tampered and sensitive envelopes fall back without rendering", async () => {
  const fixture = mock();
  for (const preview of [undefined, { state: "sensitive-content" }, envelope("a".repeat(2049)),
    { ...envelope("safe"), sha256: "0".repeat(64) }, { ...envelope("safe"), data: "truncated" }]) {
    assert.equal(await showTextPreview(fixture.vscode, { text_preview: preview, sha256: envelope("safe").sha256 }, "code.txt", "point"), false);
  }
  assert.equal(fixture.calls.length, 0);
  assert.ok(!JSON.stringify(metadataOnly({ before_text_preview: { data: "malformed payload" } })).includes("malformed payload"));
});

test("digest fallback strips admitted peer payload and trust denial makes no preview call", async () => {
  const pin = { project_id: "room", dimension_id: "archive", snapshot_id: "old" };
  const preview = envelope("safe synthetic");
  const picks = [{ entry: { type: "file" }, path: "code.txt" }, "Compare with current workspace", undefined];
  let shown = "";
  const vscode = { workspace: { isTrusted: true, openTextDocument: async value => { shown = value.content; return value; } },
    window: { showQuickPick: async () => picks.shift(), showTextDocument: async () => {} } };
  await browseRecovery(vscode, pin, async args => ({ ok: true, room_id: "room", dimension_id: "archive", snapshot_id: "old",
    entries: [], next_offset: null, ...(args[1] === "diff" ? { text_diff: "digest-only", before_text_preview: preview, after_text_preview: { state: "sensitive-content" } } : {}) }), "/synthetic/workspace");
  assert.ok(!shown.includes(preview.data));
  const calls = [];
  const deniedPicks = [{ entry: { type: "file" }, path: "code.txt" }, "Preview text"];
  vscode.workspace.isTrusted = false;
  vscode.window.showQuickPick = async () => deniedPicks.shift();
  await assert.rejects(browseRecovery(vscode, pin, async args => { calls.push(args); return { room_id: "room", dimension_id: "archive", snapshot_id: "old", entries: [], next_offset: null }; }), /Trust/);
  assert.ok(calls.every(args => !args.includes("--include-text")));
});
