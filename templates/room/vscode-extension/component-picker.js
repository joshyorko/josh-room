const fs = require('node:fs');

// This selection is a request, never storage or source authorization.
async function pickComponents(vscode, source, previous) {
  const robot = fs.existsSync(require('node:path').join(source, 'robot.yaml'));
  const defaults = previous?.request?.components || {
    rcc_environment: robot ? 'refresh' : 'keep', homebrew_recovery: 'keep', hauler_content: 'keep',
  };
  const unavailable = [
    { label: 'Homebrew installed prefix / binaries', detail: 'Unavailable: fresh same-platform, same-prefix restore and binary execution are not qualified.' },
    { label: 'Codex sessions (history-only)', detail: 'Unavailable in this controller: #188 admission and history capture must qualify; auth is never copied.' },
    { label: 'Claude sessions', detail: 'Blocked: no qualified #188 capability.' },
    { label: 'T3 Code sessions', detail: 'Blocked: shared-server isolation and #188 admission are not qualified.' },
  ];
  const rows = [
    { label: 'Workspace source + Git state — always included', mandatory: true, picked: true, detail: 'Uses the reviewed workspace capture policy; source-only omits Git history.' },
    { id: 'rcc_environment', label: 'RCC exact environment', picked: defaults.rcc_environment !== 'omit', detail: robot ? 'Refresh/reuse robot.yaml artifact; platform bound.' : 'Keep previous artifact if present; no root robot.yaml to capture.' },
    { id: 'homebrew_recovery', label: 'Homebrew recovery export', picked: defaults.homebrew_recovery !== 'omit', detail: 'Keep previous or select a prepared recovery archive. A Brewfile is a reinstall recipe, not installed binaries.' },
    { id: 'hauler_content', label: 'OCI images / additional Hauler content', picked: defaults.hauler_content !== 'omit', detail: 'Keep previous or explicitly refresh selected content.' },
    ...unavailable.map(row => ({ ...row, unavailable: true })),
  ];
  let chosen;
  while (true) {
    chosen = await vscode.window.showQuickPick(rows, { title: 'What should this recovery point preserve?', placeHolder: 'Customize components • workspace is always included; unchecked optional rows omit only from the new recovery point', canPickMany: true, ignoreFocusOut: true });
    if (!chosen) return undefined;
    if (chosen.some(row => row.unavailable)) {
      await vscode.window.showWarningMessage('That component is unavailable. No capture was authorized. Deselect it to continue.');
      continue;
    }
    break;
  }
  const components = Object.fromEntries(['rcc_environment', 'homebrew_recovery', 'hauler_content'].map(id => [id, chosen.some(row => row.id === id) ? 'keep' : 'omit']));
  if (components.rcc_environment !== 'omit' && robot) components.rcc_environment = 'refresh';
  const args = [];
  for (const [id, title, actions] of [
    ['homebrew_recovery', 'Homebrew recovery export', ['Keep previous', 'Select Brewfile-only recipe directory', 'Select prepared archive']],
    ['hauler_content', 'OCI / Hauler content', ['Keep previous', 'All tagged local OCI images', 'Select Hauler manifest']],
  ]) {
    if (components[id] === 'omit') continue;
    const action = await vscode.window.showQuickPick(actions, { title, ignoreFocusOut: true });
    if (!action) return undefined;
    if (action === 'Keep previous') continue;
    components[id] = 'refresh';
    if (action === 'All tagged local OCI images') args.push('--all-images');
    else {
      const files = await vscode.window.showOpenDialog({ title: action, canSelectFiles: action !== 'Select Brewfile-only recipe directory', canSelectFolders: action === 'Select Brewfile-only recipe directory', canSelectMany: false });
      if (!files?.length) return undefined;
      args.push(id === 'homebrew_recovery' ? (action === 'Select Brewfile-only recipe directory' ? '--brew-directory' : '--brew-archive') : '--hauler-manifest', files[0].fsPath);
    }
  }
  const request = { format_version: 1, components };
  return { request, args: ['--component-selection', JSON.stringify(request), ...args], allImages: args.includes('--all-images') };
}

function planText(preview) {
  return Object.entries(preview.component_plan || {}).map(([name, row]) => `${name}: ${row.mode} — ${row.state}`).join('\n');
}
module.exports = { pickComponents, planText };
