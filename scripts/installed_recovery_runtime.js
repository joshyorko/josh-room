"use strict";
const fs = require("node:fs");
const path = require("node:path");

function applyInstalledRuntime(extensionPath, runtime, { cache = require.cache,
  realpath = fs.realpathSync.native, platform = process.platform } = {}) {
  const canonical = filename => {
    const actual = realpath(filename).replaceAll("\\", "/");
    return platform === "win32" ? actual.toLowerCase() : actual;
  };
  const expected = canonical(path.join(extensionPath, "extension.js"));
  const matched = [];
  for (const loaded of Object.values(cache)) {
    if (!loaded?.filename || loaded.filename.split(/[\\/]/).pop().toLowerCase() !== "extension.js") continue;
    let actual;
    try { actual = canonical(loaded.filename); } catch { continue; }
    if (actual !== expected || typeof loaded.exports?.__test__?.setRuntimeForTests !== "function") continue;
    loaded.exports.__test__.setRuntimeForTests(runtime);
    matched.push(loaded.exports.__test__);
  }
  if (!matched.length) throw new Error("Activated installed runtime hook was not found");
  return matched;
}
module.exports = { applyInstalledRuntime };
