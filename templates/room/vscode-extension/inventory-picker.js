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
        if (token.isCancellationRequested && !["complete", "partial", "stale"].includes(progress.status)) { await request({ action: "cancel" }); return; }
      } while (!["complete", "partial", "stale"].includes(progress.status));
    });
    if (["complete", "partial", "stale"].includes(progress.status)) break;
    const resume = await vscode.window.showQuickPick([{ label: "Resume discovery" }], { title: "Discovery paused — closing discards the temporary index" });
    if (!resume) return "cancelled";
  }
  if (progress.status === "stale") throw new Error("Directory source or policy changed. Restart discovery.");
  let after = "";
  let query = "";
  const selected = new Map();
  let suggested = false;
  let reviewing = false;
  let reviewOffset = 0;
  async function validate(choice) {
    try {
      if (!choice.id && Buffer.byteLength(choice.path, "utf8") > 16000) {
        await vscode.window.showWarningMessage("Manual path exceeds 16,000 bytes. Browse or search for its directory instead.");
        return false;
      }
      const result = await request(choice.id ? { action: "lookup", id: choice.id } : { action: "lookup", path: choice.path });
      choice.id = result.id || choice.id || choice.path;
      return true;
    } catch (error) {
      if (!error.recoverable) throw error;
      await vscode.window.showWarningMessage("Directory unavailable or stale. Correct the path, or restart discovery if the source changed.");
      return false;
    }
  }
  for (;;) {
    if (reviewing) {
      const entries = [...selected.values()].slice(reviewOffset, reviewOffset + 50);
      const rows = entries.map((row) => ({ label: row.path, description: "Explicit choice — select to remove", choice: row }));
      if (reviewOffset + 50 < selected.size) rows.push({ label: "Next selected page", action: "next" });
      if (reviewOffset) rows.push({ label: "First selected page", action: "first" });
      rows.push({ label: "Return to browsing", action: "back" }, { label: "Finish read-only review", action: "finish" });
      const review = await vscode.window.showQuickPick(rows, { title: `${selected.size} explicit choices — preview only`, ignoreFocusOut: true });
      if (!review) return "cancelled";
      if (review.choice) selected.delete(review.choice.id || review.choice.path);
      else if (review.action === "next") reviewOffset += 50;
      else if (review.action === "first") reviewOffset = 0;
      else if (review.action === "back") reviewing = false;
      else if (review.action === "finish") {
        let valid = true;
        for (const choice of selected.values()) {
          if (!await validate(choice)) { valid = false; break; }
        }
        if (!valid) continue;
        await vscode.window.showInformationMessage(`${selected.size} explicit directory choices reviewed. Preview only; no capture, exclusions or provider operations.`, { modal: true, detail: "Existing full-workspace Save is unchanged. Scalable composition storage, tri-state assignment, publication and hydration remain future work. Git history isolation is unproven." });
        return "preview-only";
      }
      continue;
    }
    const page = await request({ action: "page", after, query, suggested });
    if (page.status === "stale") throw new Error("Source changed. Restart discovery.");
    const choices = page.candidates.map((row) => ({ label: `${selected.has(row.id || row.path) ? "✓ " : ""}${row.path}`, description: row.evidence_code, detail: row.reason, path: row.path, id: row.id }));
    choices.push({ label: "Search all indexed directories", action: "search" });
    choices.push({ label: suggested ? "Browse all directories" : "Suggested repository boundaries", action: "suggestions" });
    choices.push({ label: "Enter exact relative path", action: "manual" });
    if (page.next) choices.push({ label: "Next page", action: "next" });
    if (after) choices.push({ label: "First page", action: "first" });
    choices.push({ label: `Review ${selected.size} explicit choices (preview only)`, action: "review" });
    const picked = await vscode.window.showQuickPick(choices, { title: `Split preview: ${page.status} coverage; ${page.directories_indexed} indexed`, placeHolder: `${page.workspace_recommendation || "Ownership unknown"} — select to toggle; never capture authorization`, ignoreFocusOut: true });
    if (!picked) return "cancelled";
    if (picked.path) {
      if (await validate(picked)) {
        const key = picked.id || picked.path;
        if (selected.has(key)) selected.delete(key); else selected.set(key, picked);
      }
    } else if (picked.action === "next") after = page.next;
    else if (picked.action === "first") after = "";
    else if (picked.action === "suggestions") { suggested = !suggested; after = ""; }
    else if (picked.action === "search") {
      const value = await vscode.window.showInputBox({ title: "Search every indexed directory", value: query });
      if (value !== undefined) {
        if (Buffer.byteLength(value, "utf8") > 16000) await vscode.window.showWarningMessage("Search exceeds 16,000 bytes. Use a shorter search.");
        else { query = value; after = ""; suggested = false; }
      }
    } else if (picked.action === "manual") {
      const value = await vscode.window.showInputBox({ title: "Exact workspace-relative directory (preview only)" });
      if (value) {
        const choice = { path: value };
        if (await validate(choice)) selected.set(choice.id, choice);
      }
    } else if (picked.action === "review") {
      reviewing = true;
      reviewOffset = 0;
    }
  }
}

module.exports = { pickInventory };
