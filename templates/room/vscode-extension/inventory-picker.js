"use strict";

// Each QuickPick contains at most 50 directory rows plus navigation. Choices
// persist independently of page/filter; no recommendation is preselected.
async function pickInventory(vscode, request) {
  let progress;
  for (;;) {
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: "Discovering directories (preview only)", cancellable: true }, async (reporter, token) => {
      do {
        progress = await request({ action: "advance" });
        reporter.report({ message: `${progress.directories_indexed} directories observed; ${progress.status}` });
        if (token.isCancellationRequested) { await request({ action: "cancel" }); return; }
      } while (!["complete", "partial", "stale"].includes(progress.status));
    });
    if (["complete", "partial", "stale"].includes(progress.status)) break;
    const resume = await vscode.window.showQuickPick([{ label: "Resume discovery" }], { title: "Discovery paused — closing discards the temporary index" });
    if (!resume) return "cancelled";
  }
  if (progress.status === "stale") throw new Error("Directory source or policy changed. Restart discovery.");
  let after = "";
  let query = "";
  const selected = new Set();
  for (;;) {
    const page = await request({ action: "page", after, query });
    if (page.status === "stale") throw new Error("Source changed. Restart discovery.");
    const choices = page.candidates.map((row) => ({ label: `${selected.has(row.path) ? "✓ " : ""}${row.path}`, description: row.evidence_code, detail: row.reason, path: row.path }));
    choices.push({ label: "Search all indexed directories", action: "search" });
    choices.push({ label: "Enter exact relative path", action: "manual" });
    if (page.next) choices.push({ label: "Next page", action: "next" });
    if (after) choices.push({ label: "First page", action: "first" });
    choices.push({ label: `Review ${selected.size} explicit choices (preview only)`, action: "review" });
    const picked = await vscode.window.showQuickPick(choices, { title: `Split preview: ${page.status} coverage; ${page.directories_indexed} indexed`, placeHolder: "Select to toggle; recommendations never authorize capture", ignoreFocusOut: true });
    if (!picked) return "cancelled";
    if (picked.path) {
      await request({ action: "lookup", path: picked.path });
      if (selected.has(picked.path)) selected.delete(picked.path); else selected.add(picked.path);
    } else if (picked.action === "next") after = page.next;
    else if (picked.action === "first") after = "";
    else if (picked.action === "search") {
      const value = await vscode.window.showInputBox({ title: "Search every indexed directory", value: query });
      if (value !== undefined) { query = value; after = ""; }
    } else if (picked.action === "manual") {
      const value = await vscode.window.showInputBox({ title: "Exact workspace-relative directory (preview only)" });
      if (value) { await request({ action: "lookup", path: value }); selected.add(value); }
    } else if (picked.action === "review") {
      for (const path of selected) await request({ action: "lookup", path });
      await vscode.window.showInformationMessage(`${selected.size} explicit directory choices reviewed. Preview only; no capture, exclusions or provider operations.`, { modal: true, detail: "Existing full-workspace Save is unchanged. Scalable composition storage, tri-state assignment, publication and hydration remain future work. Git history isolation is unproven." });
      return "preview-only";
    }
  }
}

module.exports = { pickInventory };
