"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const manifest = require("./package.json");

const menus = manifest.contributes.menus["view/item/context"];
const commands = new Set(manifest.contributes.commands.map(item => item.command));
const destructive = ["remove", "deleteRecoveryPoint", "deleteRoom", "manageCatalogDeletion", "deleteCatalogItem"];

test("destructive actions appear only in the danger group", () => {
  for (const menu of menus.filter(entry => destructive.some(name => entry.command === "joshRoom." + name))) {
    assert.match(menu.group, /^9_danger/);
  }
});

test("Recovery Point deletion has a distinct command bound to both ordinary point kinds", () => {
  assert.ok(commands.has("joshRoom.deleteRecoveryPoint"));
  assert.ok(menus.some(entry => entry.command === "joshRoom.deleteRecoveryPoint" && entry.when.includes("jat|logical-jat")));
});

test("Room deletion remains a separate scoped action", () => {
  assert.ok(commands.has("joshRoom.deleteRoom"));
  assert.ok(menus.some(entry => entry.command === "joshRoom.deleteRoom" && entry.when.endsWith("viewItem == room")));
});
