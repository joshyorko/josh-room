"use strict";
const crypto = require("node:crypto");
const services = new WeakMap();

function decodePreview(preview, expected) {
  if (preview?.state === "absent" && expected === null) return "";
  if (preview?.state !== "available" || preview.encoding !== "base64-utf8"
    || typeof preview.data !== "string" || preview.data.length > 2732
    || !Number.isSafeInteger(preview.size) || preview.size < 0 || preview.size > 2048
    || !/^[0-9a-f]{64}$/.test(expected || "") || preview.sha256 !== expected) return undefined;
  const bytes = Buffer.from(preview.data, "base64");
  if (bytes.length !== preview.size || bytes.toString("base64") !== preview.data
    || crypto.createHash("sha256").update(bytes).digest("hex") !== expected) return undefined;
  const text = bytes.toString("utf8");
  return Buffer.from(text, "utf8").equals(bytes) ? text : undefined;
}

function documents(vscode) {
  if (services.has(vscode)) return services.get(vscode);
  const content = new Map();
  const scheme = "josh-room-recovery";
  const provider = vscode.workspace.registerTextDocumentContentProvider(scheme, {
    provideTextDocumentContent(uri) {
      if (!vscode.workspace.isTrusted || !content.has(uri.toString())) throw new Error("Historical preview expired or workspace is untrusted.");
      return content.get(uri.toString());
    },
  });
  const close = vscode.workspace.onDidCloseTextDocument(document => content.delete(document.uri.toString()));
  const service = {
    add(text, selected, side) {
      if (!vscode.workspace.isTrusted) throw new Error("Trust this workspace before text preview.");
      const suffix = /\.([A-Za-z0-9]{1,12})$/.exec(selected)?.[1] || "txt";
      const uri = vscode.Uri.parse(`${scheme}:/${crypto.randomUUID()}/${side}.${suffix}`);
      while (content.size >= 32) content.delete(content.keys().next().value);
      content.set(uri.toString(), text);
      return uri;
    },
    dispose() { content.clear(); provider.dispose(); close.dispose(); services.delete(vscode); },
  };
  services.set(vscode, service);
  return service;
}

async function showTextPreview(vscode, result, selected, title) {
  if (!vscode.workspace.isTrusted) throw new Error("Trust this workspace before text preview.");
  if (result.text_diff === "available") {
    const before = decodePreview(result.before_text_preview, result.before?.sha256 ?? null);
    const after = decodePreview(result.after_text_preview, result.after?.sha256 ?? null);
    if (before === undefined || after === undefined) return false;
    const service = documents(vscode);
    const left = service.add(before, selected, "historical");
    const right = service.add(after, selected, "comparison");
    await vscode.commands.executeCommand("vscode.diff", left, right, title, { preview: true });
    return true;
  }
  const preview = decodePreview(result.text_preview, result.sha256);
  if (preview === undefined) return false;
  const uri = documents(vscode).add(preview, selected, "historical");
  await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(uri), { preview: true });
  return true;
}

function metadataOnly(value, context = "") {
  if (Array.isArray(value)) return value.map(entry => metadataOnly(entry, context));
  if (value && typeof value === "object") return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !((value.encoding === "base64-utf8" || context.endsWith("text_preview")) && key === "data"))
    .map(([key, entry]) => [key, metadataOnly(entry, key)]));
  return value;
}
module.exports = { decodePreview, showTextPreview, metadataOnly, documents };
