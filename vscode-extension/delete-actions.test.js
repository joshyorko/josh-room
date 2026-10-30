"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const manifest = require("./package.json");

const menus = manifest.contributes.menus["view/item/context"];
const commands = new Set(manifest.contributes.commands.map((item) => item.command));

function hasMenu(command, type, inline = false) {
  return menus.some((entry) => entry.command === command
    && entry.when.includes("viewItem == " + type)
    && (!inline || entry.group.startsWith("inline")));
}

test("Delete Snapshot remains discoverable inline on ordinary and current Room rows", () => {
  assert.ok(hasMenu("joshRoom.remove", "room", true), "ordinary Room should show a hover trash action");
  assert.ok(hasMenu("joshRoom.remove", "current-room", true), "current Room should show a hover trash action");
});

test("both portable and logical recovery point rows expose explicitly scoped deletion", () => {
  assert.ok(commands.has("joshRoom.deleteRecoveryPoint"), "recovery-point deletion must be a named command");
  assert.ok(hasMenu("joshRoom.deleteRecoveryPoint", "jat"), "portable JAT should offer Delete Recovery Point");
  assert.ok(hasMenu("joshRoom.deleteRecoveryPoint", "logical-jat"), "logical JAT should offer Delete Recovery Point");
});

test("Delete Room is a separate, discoverable action rather than a snapshot alias", () => {
  assert.ok(commands.has("joshRoom.deleteRoom"), "full-Room deletion must have its own command");
  assert.ok(hasMenu("joshRoom.deleteRoom", "room"), "ordinary Room should offer Delete Room");
  assert.ok(hasMenu("joshRoom.deleteRoom", "current-room"), "current Room should offer Delete Room");
});
