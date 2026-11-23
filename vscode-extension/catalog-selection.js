"use strict";

const MAX_CHILDREN = 32;

function eligible(row) {
  return row.selectable === true && !row.path_truncated && typeof row.path === "string"
    && row.path.length > 0 && [...row.path].length <= 512
    && ["git-boundary", "manual-directory"].includes(row.evidence_code)
    && ["folder", "git-directory-unverified"].includes(row.kind);
}

function selectionProblem(paths) {
  if (paths.length > MAX_CHILDREN) return `This would select ${paths.length} children. Select at most ${MAX_CHILDREN}; the previous selection was kept.`;
  const names = new Set(paths);
  for (const name of names) {
    const parts = name.split("/");
    while (parts.length > 1) {
      parts.pop();
      if (names.has(parts.join("/"))) return `Select either ${name} or its enclosing folder. The previous selection was kept.`;
    }
  }
}

async function selectCatalogChildren(vscode, request) {
  const { window } = vscode;
  const discovery = await window.withProgress({
    location: vscode.ProgressLocation.Notification, title: "Finding Catalog child folders", cancellable: true,
  }, async (reporter, token) => {
    for (;;) {
      if (token.isCancellationRequested) return;
      const progress = await request({ action: "advance" });
      reporter.report({ message: `${progress.directories_indexed} folders indexed` });
      if (["complete", "partial", "stale"].includes(progress.status)) return progress;
    }
  });
  if (!discovery) return;
  if (discovery.status === "stale") throw new Error("The source changed. Restart Catalog selection.");

  let suggested = true;
  let after = "";
  let query = "";
  let page;
  const selected = new Map();
  for (;;) {
    page ||= await request({ action: "page", suggested, after, query, limit: MAX_CHILDREN });
    if (page.status === "stale") throw new Error("The source changed. Restart Catalog selection.");
    const choices = page.candidates.filter(eligible);
    const rows = [
      { label: `Select All on This Page (${choices.length})`, action: "all" },
      { label: "Clear All", description: `${selected.size} selected across all pages`, action: "clear" },
      ...choices.map(candidate => ({ label: candidate.path, description: selected.has(candidate.path) ? "Selected" : candidate.reason, candidate })),
      { label: suggested ? "Browse all folders" : "Show suggested repositories", action: "scope" },
      { label: "Search folders", action: "search" },
    ];
    if (page.next) rows.push({ label: "Next page", action: "next" });
    if (after) rows.push({ label: "First page", action: "first" });
    rows.push({ label: `Continue with ${selected.size} selected`, action: "continue" });
    const scope = suggested ? "Suggested repositories" : query ? `Folders matching “${query}”` : "All indexed folders";
    const choice = await window.showQuickPick(rows, {
      title: `Catalog children — ${selected.size} selected · ${scope}`,
      placeHolder: `${discovery.status === "partial" ? "Partial discovery. " : ""}${choices.length} eligible on this page. Select folders to toggle.`,
      ignoreFocusOut: true,
    });
    if (!choice) return;
    if (choice.action === "continue") {
      if (!selected.size) {
        await window.showWarningMessage("Select at least one child folder to continue.");
        continue;
      }
      for (const row of selected.values()) {
        const current = await request({ action: "lookup", id: row.id });
        if (current.id !== row.id || current.selected !== row.path || current.status === "stale") {
          throw new Error("A selected folder changed. Restart Catalog selection.");
        }
      }
      return [...selected.keys()];
    }
    if (choice.action === "clear") {
      selected.clear();
    } else if (choice.candidate || choice.action === "all") {
      if (choice.candidate && selected.delete(choice.candidate.path)) continue;
      const additions = choice.candidate ? [choice.candidate] : choices;
      const proposed = new Set([...selected.keys(), ...additions.map(row => row.path)]);
      const problem = selectionProblem([...proposed]);
      if (problem) await window.showWarningMessage(problem);
      else for (const row of additions) selected.set(row.path, row);
    } else {
      if (choice.action === "search") {
        const filter = await window.showInputBox({ title: "Search all indexed folders", value: query });
        if (filter === undefined) continue;
        if (Buffer.byteLength(filter, "utf8") > 16000) {
          await window.showWarningMessage("Use a search of at most 16,000 bytes.");
          continue;
        }
        query = filter;
        suggested = false;
      } else if (choice.action === "scope") {
        suggested = !suggested;
        query = "";
      }
      after = choice.action === "next" ? page.next : "";
      page = undefined;
    }
  }
}

module.exports = { selectCatalogChildren };
