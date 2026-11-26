"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const { contributes } = require("./package.json");

const obsolete = /\b(?:Dimension|Snapshot|Logical JAT|Portable JAT|namespace Room|Composition|Room Store|Haul)\b/i;

test("the palette offers only the Room entry points and all seven JAT Tools", () => {
  const hidden = new Set(contributes.menus.commandPalette.filter(row => row.when === "false").map(row => row.command));
  const palette = contributes.commands.filter(row => !hidden.has(row.command));
  assert.equal(palette.length, 22);
  assert.equal(palette.filter(row => row.category === "JAT").length, 7);
  assert.ok(hidden.has("joshRoom.showOperation"));
  for (const row of contributes.commands) {
    assert.equal(row.category, row.command.startsWith("joshRoom.jat") ? "JAT" : "Josh Room");
    assert.doesNotMatch(row.title, /^(?:Josh(?: Room)?|JAT):/);
    if (row.category !== "JAT") assert.doesNotMatch(row.title, obsolete, row.command);
  }
});

test("Storage welcome and native walkthrough use the same vocabulary", () => {
  const states = contributes.viewsWelcome.map(row => row.when.split(" == ")[1]);
  assert.deepEqual(states, ["noStorage", "preparing", "failed", "cancelled", "untrusted", "ready"]);
  for (const row of contributes.viewsWelcome) assert.doesNotMatch(row.contents.replace(/\(command:[^)]+\)/g, ""), obsolete);
  const [walkthrough] = contributes.walkthroughs;
  assert.equal(walkthrough.steps.length, 5);
  for (const step of walkthrough.steps) {
    const markdown = fs.readFileSync(path.join(__dirname, step.media.markdown), "utf8");
    const [completion] = step.completionEvents;
    assert.ok(markdown.includes("(command:" + completion.slice("onCommand:".length) + ")"));
    assert.doesNotMatch(markdown.replace(/\(command:[^)]+\)/g, ""), obsolete);
  }
});

test("native Room entry titles avoid legacy command prefixes", () => {
  const sources = ["extension.js", "catalog-actions.js", "recovery-actions.js", "room-operations.js", "messaging.js",
    ...fs.readdirSync(__dirname).filter(name => name.endsWith("-picker.js"))];
  for (const name of sources) {
    const source = fs.readFileSync(path.join(__dirname, name), "utf8");
    assert.doesNotMatch(source, /\btitle:\s*["'`](?:Josh(?: Room)?|JAT):/, name);
  }
});
