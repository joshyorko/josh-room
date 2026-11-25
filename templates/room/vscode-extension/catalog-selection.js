"use strict";

const fs = require("node:fs/promises");

const MAX_CHILDREN = 32;

async function selectCatalogChildren(vscode, root, { selected: existing } = {}) {
  const preserving = Array.isArray(existing);
  const entries = await fs.readdir(root, { withFileTypes: true });
  const folders = entries.filter(entry => entry.isDirectory() && (!preserving || entry.name.toLowerCase() !== ".git"))
    .map(entry => entry.name).sort((left, right) => left.localeCompare(right));
  if (!folders.length) {
    await vscode.window.showInformationMessage("This source has no immediate folders to save as child Rooms.");
    return undefined;
  }

  const choices = folders.map(label => ({ label, ...(existing?.includes(label) ? { picked: true } : {}) }));
  const selected = await vscode.window.showQuickPick(choices, {
    canPickMany: true,
    title: preserving ? "Split folders into child Rooms" : "Choose Catalog child Rooms",
    placeHolder: preserving ? "Checked folders get their own recovery points. Other eligible folders stay in the Catalog."
      : "Checked folders become whole Rooms, including their nested content. Unchecked folders stay local.",
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
