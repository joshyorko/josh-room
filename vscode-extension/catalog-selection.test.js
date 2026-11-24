"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const { selectCatalogChildren } = require("./catalog-selection");

async function sourceTree(setup) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "josh-room-catalog-"));
  await setup(root);
  return { root, cleanup: () => fs.rm(root, { recursive: true, force: true }) };
}

test("native checkbox picker lists only immediate directories and returns checked names", async () => {
  const tree = await sourceTree(async root => {
    await fs.mkdir(path.join(root, "service", "nested"), { recursive: true });
    await fs.mkdir(path.join(root, "notes"));
    await fs.writeFile(path.join(root, "root.txt"), "root");
  });
  try {
    let shown;
    const vscode = { window: {
      showQuickPick: async (items, options) => {
        shown = { items, options };
        return [items.find(item => item.label === "service")];
      },
    } };
    assert.deepEqual(await selectCatalogChildren(vscode, tree.root), ["service"]);
    assert.deepEqual(shown.items.map(item => item.label), ["notes", "service"]);
    assert.equal(shown.options.canPickMany, true);
    assert.equal(shown.items.some(item => Object.hasOwn(item, "picked")), false);
    assert.equal(shown.items.some(item => item.label.includes("nested")), false);
  } finally {
    await tree.cleanup();
  }
});

test("empty directory list reports no selectable children without opening a picker", async () => {
  const tree = await sourceTree(async root => fs.writeFile(path.join(root, "root.txt"), "root"));
  try {
    let message;
    const vscode = { window: {
      showQuickPick: async () => assert.fail("empty source should not open a picker"),
      showInformationMessage: async value => { message = value; },
    } };
    assert.equal(await selectCatalogChildren(vscode, tree.root), undefined);
    assert.match(message, /no immediate folders/i);
  } finally {
    await tree.cleanup();
  }
});

test("picker cancellation returns undefined", async () => {
  const tree = await sourceTree(root => fs.mkdir(path.join(root, "service")));
  try {
    const vscode = { window: { showQuickPick: async () => undefined } };
    assert.equal(await selectCatalogChildren(vscode, tree.root), undefined);
  } finally {
    await tree.cleanup();
  }
});

test("more than 32 checked folders is rejected without truncating the selection", async () => {
  const tree = await sourceTree(async root => {
    await Promise.all(Array.from({ length: 33 }, (_, index) => fs.mkdir(path.join(root, `child-${index}`))));
  });
  try {
    let warning;
    const vscode = { window: {
      showQuickPick: async items => items,
      showWarningMessage: async value => { warning = value; },
    } };
    assert.equal(await selectCatalogChildren(vscode, tree.root), undefined);
    assert.match(warning, /select at most 32/i);
  } finally {
    await tree.cleanup();
  }
});

test("32 of 33 immediate folders can be checked", async () => {
  const tree = await sourceTree(async root => {
    await Promise.all(Array.from({ length: 33 }, (_, index) => fs.mkdir(path.join(root, `child-${index}`))));
  });
  try {
    const vscode = { window: { showQuickPick: async items => items.slice(0, 32) } };
    assert.equal((await selectCatalogChildren(vscode, tree.root)).length, 32);
  } finally {
    await tree.cleanup();
  }
});
