"use strict";
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { selectCatalogChildren } = require("./catalog-selection");

const candidate = (path, code = "git-boundary") => ({
  id: path, path, evidence_code: code,
  kind: code === "git-boundary" ? "git-directory-unverified" : "folder", selectable: true,
});

async function pick(pages, actions) {
  const warnings = [];
  const requests = [];
  const titles = [];
  const vscode = { ProgressLocation: { Notification: 1 }, window: {
    withProgress: (_, body) => body({ report() {} }, { isCancellationRequested: false }),
    showQuickPick: async (rows, options) => {
      titles.push(options.title);
      assert.ok(actions.length, "unexpected extra picker");
      const label = actions.shift();
      if (label === null) return;
      const row = rows.find(row => row.label === label);
      assert.ok(row, `missing picker row ${label}`);
      return row;
    },
    showWarningMessage: async warning => warnings.push(warning),
    showInputBox: async () => "projects",
  } };
  const result = await selectCatalogChildren(vscode, async request => {
    requests.push(request);
    if (request.action === "lookup") return { status: "complete", id: request.id, selected: request.id };
    if (request.action === "page") {
      const index = Number(request.after || 0);
      return { status: "complete", candidates: pages[index], next: index < pages.length - 1 ? String(index + 1) : null };
    }
    return { status: "complete", directories_indexed: 100 };
  });
  return { result, warnings, requests, titles };
}

test("Select All covers the visible eligible page, preserving manual overrides across pages", async () => {
  const pages = [[candidate("a"), candidate("b"), candidate("hidden", "policy-excluded"),
    candidate("repo/internal", "cohesive-repository")], [candidate("c")]];
  const { result, requests, titles } = await pick(pages, ["Select All on This Page (2)", "b", "Next page",
    "Select All on This Page (1)", "Clear All", "First page", "a", "Continue with 1 selected"]);
  assert.deepEqual(result, ["a"]);
  assert.deepEqual(requests.filter(request => request.action === "lookup"), [{ action: "lookup", id: "a" }]);
  assert.ok(titles.some(title => title.includes("2 selected")));
  assert.equal(titles.at(-2).includes("0 selected"), true);
});

test("bulk selection refuses overflow atomically and cancellation produces no selection", async () => {
  const rows = Array.from({ length: 33 }, (_, n) => candidate(`repo-${n}`));
  const { warnings, result, requests } = await pick([rows], ["repo-0", "Select All on This Page (33)", "Continue with 1 selected"]);
  assert.match(warnings[0], /33.*at most 32/);
  assert.deepEqual(result, ["repo-0"]);
  assert.equal(requests.filter(request => request.action === "lookup").length, 1);
  assert.equal((await pick([rows], ["Select All on This Page (33)", null])).result, undefined);
});

test("nested selections require an explicit choice and empty selections cannot continue", async () => {
  const { result, warnings } = await pick([[candidate("group", "manual-directory"), candidate("group/repo")]],
    ["Browse all folders", "Continue with 0 selected", "group/repo", "group", "Select All on This Page (2)", "Continue with 1 selected"]);
  assert.deepEqual(result, ["group/repo"]);
  assert.match(warnings[0], /at least one/);
  assert.match(warnings[1], /enclosing folder/);
  assert.match(warnings[2], /enclosing folder/);
});
