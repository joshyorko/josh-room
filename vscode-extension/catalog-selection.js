"use strict";

const fs = require("node:fs/promises");

const MAX_CHILDREN = 32;

async function selectCatalogChildren(vscode, root) {
  const entries = await fs.readdir(root, { withFileTypes: true });
  const folders = entries.filter(entry => entry.isDirectory()).map(entry => entry.name).sort((left, right) => left.localeCompare(right));
  if (!folders.length) {
    await vscode.window.showInformationMessage("This source has no immediate folders to save as child Rooms.");
    return undefined;
  }

  const choices = folders.map(label => ({ label }));
  const selected = await vscode.window.showQuickPick(choices, {
    canPickMany: true,
    title: "Choose Catalog child Rooms",
    placeHolder: "Checked folders become whole Rooms, including their nested content. Unchecked folders stay local.",
    ignoreFocusOut: true,
  });
  if (selected === undefined) return undefined;
  if (!Array.isArray(selected) || selected.some(item => !item || !folders.includes(item.label))) {
    throw new Error("The Catalog folder selection was invalid. Restart selection.");
  }
  if (selected.length > MAX_CHILDREN) {
    await vscode.window.showWarningMessage(`Select at most ${MAX_CHILDREN} child folders. No folders were selected.`);
    return undefined;
  }
  return selected.map(item => item.label);
}

module.exports = { selectCatalogChildren };
