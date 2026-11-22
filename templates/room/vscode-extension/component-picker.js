const fs = require('node:fs');
const crypto = require('node:crypto');

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
    { label: 'Workspace source + Git state — always included', mandatory: true, kind: vscode.QuickPickItemKind?.Separator ?? -1, detail: 'Uses the reviewed workspace capture policy; source-only omits Git history.' },
    { id: 'rcc_environment', label: 'RCC exact environment', picked: defaults.rcc_environment !== 'omit', detail: robot ? 'Refresh/reuse robot.yaml artifact; platform bound.' : 'Keep previous artifact if present; no root robot.yaml to capture.' },
    { id: 'homebrew_recovery', label: 'Homebrew recovery export', picked: defaults.homebrew_recovery !== 'omit', detail: 'Keep previous or select a prepared recovery archive. A Brewfile is a reinstall recipe, not installed binaries.' },
    { id: 'hauler_content', label: 'OCI images / additional Hauler content', picked: defaults.hauler_content !== 'omit', detail: 'Keep previous or explicitly refresh selected content.' },
    ...unavailable.map(row => ({ ...row, label: `Unavailable — ${row.label}`, unavailable: true, picked: false })),
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
    ['homebrew_recovery', 'Homebrew recovery export', ['Keep previous', ...(process.platform === 'linux' ? ['Select Brewfile-only recipe directory'] : []), 'Select prepared archive']],
    ['hauler_content', 'OCI / Hauler content', ['Keep previous', 'All tagged local OCI images', 'Select OCI image references', 'Select Hauler manifest', 'Select additional Hauler file']],
  ]) {
    if (components[id] === 'omit') continue;
    const action = await vscode.window.showQuickPick(actions, { title, ignoreFocusOut: true });
    if (!action) return undefined;
    if (action === 'Keep previous') continue;
    components[id] = 'refresh';
    if (action === 'All tagged local OCI images') args.push('--all-images');
    else if (action === 'Select OCI image references') {
      const text = await vscode.window.showInputBox({ title: action, prompt: 'Local image references separated by whitespace; no implicit pull', ignoreFocusOut: true });
      if (!text?.trim()) return undefined;
      for (const reference of text.trim().split(/\s+/)) args.push('--image', reference);
    } else {
      const files = await vscode.window.showOpenDialog({ title: action, canSelectFiles: action !== 'Select Brewfile-only recipe directory', canSelectFolders: action === 'Select Brewfile-only recipe directory', canSelectMany: false });
      if (!files?.length) return undefined;
      if (action === 'Select additional Hauler file') {
        const name = await vscode.window.showInputBox({ title: 'Hauler file name', prompt: 'Name this explicitly selected content', ignoreFocusOut: true });
        if (!name?.trim()) return undefined;
        args.push('--hauler-file', files[0].fsPath, name.trim());
        continue;
      }
      args.push(id === 'homebrew_recovery' ? (action === 'Select Brewfile-only recipe directory' ? '--brew-directory' : '--brew-archive') : '--hauler-manifest', files[0].fsPath);
    }
  }
  const request = { format_version: 1, components };
  return { request, args: ['--component-selection', JSON.stringify(request), ...args], allImages: args.includes('--all-images') };
}

function planText(preview) {
  return Object.entries(preview.component_plan || {}).map(([name, row]) => `${name}: ${row.mode} — ${row.state}`).join('\n');
}
function storageKey(binding) {
  return `save-components.v1.${crypto.createHash('sha256').update(binding).digest('hex')}`;
}
function storedChoice(state, binding) {
  const value = state?.get(storageKey(binding));
  const modes = value?.request?.components;
  if (value?.request?.format_version !== 1 || !modes || Object.keys(modes).sort().join(',') !== 'hauler_content,homebrew_recovery,rcc_environment'
    || !Object.values(modes).every(mode => ['keep', 'refresh', 'omit'].includes(mode))) return undefined;
  return { request: { format_version: 1, components: { ...modes } } };
}
async function rememberChoice(state, binding, choice) {
  // Persist modes only. File paths, credentials and capture authority never persist.
  await state?.update(storageKey(binding), { request: choice.request });
}
module.exports = { pickComponents, planText, storedChoice, rememberChoice };
