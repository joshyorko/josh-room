"use strict";

// Keep the original authenticated point through navigation, paging and cancellation.
async function browseRecovery(vscode, context, run, workspace) {
  const pin = Object.freeze({ ...context });
  if (!pin.project_id || !pin.dimension_id || !pin.snapshot_id || pin.snapshot_id === "latest") {
    throw new Error("Select an exact historical recovery point.");
  }
  const invoke = async (action, path, extra = []) => {
    const result = await run(["recovery", action, pin.project_id, "--snapshot", pin.snapshot_id,
      "--path", path, ...extra]);
    if (!result || result.snapshot_id !== pin.snapshot_id || result.dimension_id !== pin.dimension_id
      || result.room_id !== pin.project_id) throw new Error("Historical recovery identity changed.");
    return result;
  };
  const show = async (result) => {
    const document = await vscode.workspace.openTextDocument({ language: "json", content: JSON.stringify(result, null, 2) });
    await vscode.window.showTextDocument(document, { preview: true });
  };
  let path = ".";
  let offset = 0;
  while (true) {
    const result = await invoke("tree", path, ["--offset", String(offset), "--limit", "100"]);
    const choices = result.entries.map((entry) => ({ label: entry.name, description: entry.type,
      entry, path: path === "." ? entry.name : `${path}/${entry.name}` }));
    if (path !== ".") choices.unshift({ label: "..", action: "parent" });
    if (result.next_offset !== null) choices.push({ label: "Next page…", action: "next" });
    if (offset > 0) choices.unshift({ label: "First page…", action: "first" });
    const chosen = await vscode.window.showQuickPick(choices, {
      title: `Recovery ${pin.snapshot_id} · ${path}`, placeHolder: "Browse metadata without hydrating the Room",
    });
    if (!chosen) return "cancelled";
    if (chosen.action === "next") { offset = result.next_offset; continue; }
    if (chosen.action === "first") { offset = 0; continue; }
    if (chosen.action === "parent") { path = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "."; offset = 0; continue; }
    const directory = chosen.entry.type === "dir";
    const actions = directory ? ["Browse", "Recover selection…", "Compare with recovery point…"]
      : ["Inspect checksum", "Recover selection…", "Compare with current workspace", "Compare with recovery point…"];
    const action = await vscode.window.showQuickPick(actions, { title: chosen.path });
    if (!action) continue;
    if (action === "Browse") { path = chosen.path; offset = 0; continue; }
    if (action === "Inspect checksum") { await show(await invoke("inspect", chosen.path)); continue; }
    if (!vscode.workspace.isTrusted) throw new Error("Trust this workspace before recovery or comparison.");
    if (action === "Recover selection…") {
      const destination = await vscode.window.showSaveDialog({ title: "Recover to a new, absent destination", saveLabel: "Recover selection" });
      if (!destination) continue;
      if (destination.scheme !== "file") throw new Error("Recovery requires a local filesystem destination.");
      await show(await invoke("extract", chosen.path, ["--destination", destination.fsPath]));
    } else if (action === "Compare with current workspace") {
      if (!workspace) throw new Error("Open an explicit workspace to compare.");
      await show(await invoke("diff", chosen.path, ["--workspace", workspace]));
    } else {
      const other = await vscode.window.showInputBox({ title: "Exact recovery point ID to compare", ignoreFocusOut: true });
      if (!other) continue;
      if (other === "latest") throw new Error("Comparison requires an exact recovery point.");
      await show(await invoke("diff", chosen.path, ["--compare-snapshot", other]));
    }
  }
}
module.exports = { browseRecovery };
