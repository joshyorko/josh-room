const fs = require('node:fs');
const crypto = require('node:crypto');

const admissionMessages = {
  'native-room-authority-unavailable': 'Host recovery admission is missing or unreadable. Existing shared-home history is not enrolled; dedicated-profile setup needs a host grant (#188).',
  'native-room-profile-unavailable': 'No granted recovery profile matches this Room and folder. Existing shared-home adoption needs #188.',
  'native-save-disabled': 'Agent-history Save is disabled in this controller.',
  'native-save-unsupported-platform': 'Agent-history Save requires Linux.',
  'native-save-unsupported-provider': 'Agent-history Save currently requires MinIO.',
  'native-save-work-scope-unqualified': 'Work-scope history cannot be uploaded through this Save flow.',
  'native-save-room-reference-not-authorized': 'The profile has no Room-history capture grant.',
  'native-save-profile-changed': 'The recovery grant changed. Revalidate the dedicated profile before capture.',
  'native-save-profile-admission-failed': 'The recovery profile failed current admission. Check its host grant and custody before capture.',
};
const profileDenialReasons = new Set(['native-save-profile-changed', 'native-save-profile-admission-failed']);
const noAdmittedProfile = 'No currently admitted recovery profile; existing shared-home adoption needs #188.';

function sessionSummary(inventory, tool) {
  const row = inventory?.contract_version === 1 ? inventory.tools?.[tool] : undefined;
  if (!row || !['complete', 'partial', 'unavailable', 'unsupported'].includes(row.state)
    || ![row.exact, row.descendant].every(n => Number.isInteger(n) && n >= 0 && n <= 4096)
    || row.exact + row.descendant > 4096
    || (row.state === 'complete' && row.reason !== undefined)
    || (['unavailable', 'unsupported'].includes(row.state) && (row.exact !== 0 || row.descendant !== 0))) return 'Session detection unavailable; session absence is not established.';
  const counts = row.exact + row.descendant
    ? `${row.exact} session(s) in this folder; ${row.descendant} in child folders. Detection only; not included.`
    : row.state === 'complete' ? 'No matching sessions found in scanned metadata.' : 'No matching sessions confirmed; discovery is incomplete.';
  const status = {
    complete: '', partial: ' Scan incomplete (limit, unsupported metadata, or unsafe entry).',
    unavailable: ' Session discovery failed; no absence claim.', unsupported: ' Session metadata format or platform is unsupported.',
  }[row.state];
  return counts + status;
}

function validAgentProfile(profile) {
  return profile?.admitted === true
    && ['codex', 'claude'].includes(profile.tool)
    && /^[0-9a-f]{64}$/.test(profile.profile_ref)
    && ['personal', 'oss', 'homelab'].includes(profile.scope);
}

// This selection is a request, never storage or source authorization.
async function pickComponents(vscode, source, previous, discoverInstalled, discoverAgents, discoverSessions, catalogSources) {
  let agentProfiles = [];
  let agentReason = 'Recovery admission could not be checked. No history capture is authorized.';
  let agentDenials;
  if (discoverAgents) {
    try {
      const candidate = (await discoverAgents())?.agent_save;
      const validStructure = candidate?.contract_version === 1
        && candidate.mode === 'history-only'
        && typeof candidate.available === 'boolean'
        && Array.isArray(candidate.profiles)
        && candidate.profiles.length <= 128
        && (candidate.available
          ? candidate.profiles.length > 0 && candidate.profiles.every(validAgentProfile)
          : candidate.profiles.length === 0);
      if (validStructure) {
        agentDenials = candidate.denials;
        const reason = candidate.reason;
        agentReason = typeof reason === 'string' && Object.hasOwn(admissionMessages, reason)
          && !(agentDenials && profileDenialReasons.has(reason)) ? admissionMessages[reason] : noAdmittedProfile;
        if (candidate.available) agentProfiles = candidate.profiles;
      }
    } catch (_) { /* Unavailable admission never authorizes capture. */ }
  }
  let inventory;
  if (discoverSessions) {
    try { inventory = (await discoverSessions())?.session_inventory; }
    catch (_) { /* Closed diagnostics below never expose paths or raw errors. */ }
  }
  let installed = { available: false };
  if (discoverInstalled) {
    try {
      const result = await discoverInstalled();
      const candidate = result?.homebrew_installed;
      if (candidate?.available === true && candidate.format === "josh-room-homebrew-installed-v1" && candidate.prefix === "/home/linuxbrew/.linuxbrew") installed = candidate;
    } catch (_error) { /* Unavailable discovery never authorizes capture. */ }
  }
  const hasRobot = folder => fs.existsSync(require('node:path').join(folder, 'robot.yaml'));
  const robot = catalogSources ? catalogSources.every(hasRobot) : hasRobot(source);
  const defaults = previous?.request?.components || {
    rcc_environment: robot ? 'refresh' : 'keep', homebrew_recovery: 'keep', hauler_content: 'keep',
  };
  const reasonFor = tool => {
    const denial = agentDenials?.[tool];
    return profileDenialReasons.has(denial) ? admissionMessages[denial] : agentReason;
  };
  const unavailable = [
    ...(!installed.available ? [{ label: 'Homebrew installed prefix / binaries', detail: 'Unavailable: requires qualified Debian 12 x64, owner-controlled exact prefix, supported Hello or declared jq runtime closure.' }] : []),
    ...(!agentProfiles.some(p => p.tool === 'codex') ? [{ label: 'Codex HISTORY', detail: `${sessionSummary(inventory, 'codex')} ${reasonFor('codex')} Authentication is never copied.` }] : []),
    ...(!agentProfiles.some(p => p.tool === 'claude') ? [{ label: 'Claude HISTORY', detail: `${reasonFor('claude')} Authentication is never copied.` }] : []),
    { label: 'T3 Code sessions', detail: `${sessionSummary(inventory, 't3')} T3 recovery remains blocked by #188. Keep the existing home until supported recovery is available; no shared database capture.` },
  ];
  const rows = [
    { label: 'Workspace source + Git state — always included', mandatory: true, kind: vscode.QuickPickItemKind?.Separator ?? -1, detail: 'Uses the reviewed workspace capture policy; source-only omits Git history.' },
    { id: 'rcc_environment', label: 'RCC exact environment', picked: defaults.rcc_environment !== 'omit', unavailable: Boolean(catalogSources && !robot), detail: catalogSources && !robot ? 'Unavailable: every selected folder needs a root robot.yaml. Choose a smaller group to capture individual environments.' : robot ? 'Refresh/reuse robot.yaml artifact; platform bound.' : 'Keep previous artifact if present; no root robot.yaml to capture.' },
    ...(installed.available ? [{ id: 'homebrew_installed', label: 'Installed Homebrew package/runtime (Debian 12 x64, exact prefix)', detail: 'Conditional source admission: qualified Hello build or jq/oniguruma + installed Homebrew glibc. Includes exact loader/libc bytes and a recipe fallback; replaces this point’s Homebrew recovery component.', picked: false }] : []),
    { id: 'homebrew_recovery', label: 'Homebrew recovery export', picked: defaults.homebrew_recovery !== 'omit', detail: 'Keep previous or select a prepared recovery archive. A Brewfile is a reinstall recipe, not installed binaries.' },
    { id: 'hauler_content', label: 'OCI images / additional Hauler content', picked: defaults.hauler_content !== 'omit', detail: 'Keep previous or explicitly refresh selected content.' },
    ...['codex', 'claude'].filter(tool => agentProfiles.some(p => p.tool === tool)).map(tool => ({ id: `agent_${tool}`, tool, label: `${tool === 'codex' ? 'Codex' : 'Claude'} HISTORY`, picked: false, detail: `Capture only the explicitly chosen granted profile, not ordinary or child-folder sessions. No auth or native continuation.${tool === 'codex' ? ' ' + sessionSummary(inventory, tool) : ''}` })),
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
  const inputs = {};
  const installedSelected = chosen.some(row => row.id === 'homebrew_installed');
  if (installedSelected) {
    if (!installed.available) throw new Error('Installed Homebrew capture is unavailable.');
    const approved = await vscode.window.showQuickPick(['Use /home/linuxbrew/.linuxbrew'], { title: 'Confirm qualified installed Homebrew prefix', placeHolder: 'Capture only the selected package/runtime closure from this exact prefix', ignoreFocusOut: true });
    if (!approved) return undefined;
    const formula = await vscode.window.showInputBox({ title: 'Installed Homebrew formula', prompt: 'Supported: hello (qualified build) or jq (declared oniguruma + installed Homebrew glibc); validated before Save', ignoreFocusOut: true, validateInput: value => ['hello', 'jq'].includes(value.trim()) ? undefined : 'Enter hello or jq; other packages remain unqualified.' });
    if (!formula?.trim()) return undefined;
    components.homebrew_recovery = 'refresh';
    inputs.homebrew_installed = { prefix: installed.prefix, formula: formula.trim() };
  }
  for (const [id, title, actions] of [
    ['homebrew_recovery', 'Homebrew recovery export', ['Keep previous', ...(process.platform === 'linux' ? ['Select Brewfile-only recipe directory'] : []), 'Select prepared archive']],
    ['hauler_content', 'OCI / Hauler content', ['Keep previous', 'All tagged local OCI images', 'Select OCI image references', 'Select Hauler manifest', 'Select additional Hauler file']],
  ]) {
    if (components[id] === 'omit' || (id === 'homebrew_recovery' && installedSelected)) continue;
    const action = await vscode.window.showQuickPick(actions, { title, ignoreFocusOut: true });
    if (!action) return undefined;
    if (action === 'Keep previous') continue;
    components[id] = 'refresh';
    if (id === 'hauler_content') inputs.hauler_selection = { images: [], all_images: false, manifests: [], files: [] };
    if (action === 'All tagged local OCI images') inputs.hauler_selection.all_images = true;
    else if (action === 'Select OCI image references') {
      const text = await vscode.window.showInputBox({ title: action, prompt: 'Local image references separated by whitespace; no implicit pull', ignoreFocusOut: true });
      if (!text?.trim()) return undefined;
      inputs.hauler_selection.images = text.trim().split(/\s+/);
    } else {
      const files = await vscode.window.showOpenDialog({ title: action, canSelectFiles: action !== 'Select Brewfile-only recipe directory', canSelectFolders: action === 'Select Brewfile-only recipe directory', canSelectMany: false });
      if (!files?.length) return undefined;
      if (action === 'Select additional Hauler file') {
        const name = await vscode.window.showInputBox({ title: 'Hauler file name', prompt: 'Name this explicitly selected content', ignoreFocusOut: true });
        if (!name?.trim()) return undefined;
        inputs.hauler_selection.files.push([files[0].fsPath, name.trim()]);
        continue;
      }
      if (id === 'hauler_content') inputs.hauler_selection.manifests.push(files[0].fsPath);
      else inputs[action === 'Select Brewfile-only recipe directory' ? 'homebrew_directory' : 'homebrew_archive'] = files[0].fsPath;
    }
  }
  const agentEntries = [];
  for (const row of chosen.filter(row => row.id?.startsWith('agent_'))) {
    const profiles = agentProfiles.filter(p => p.tool === row.tool);
    const selected = await vscode.window.showQuickPick(profiles.map(p => ({ label: `${p.scope} · ${p.profile_ref.slice(0, 12)}`, profile: p })), { title: `${row.tool} HISTORY: choose granted profile`, ignoreFocusOut: true });
    if (!selected) return undefined;
    agentEntries.push({ tool: row.tool, profile_ref: selected.profile.profile_ref });
  }
  const args = [];
  if (inputs.homebrew_installed) args.push('--brew-installed-prefix', inputs.homebrew_installed.prefix, '--brew-formula', inputs.homebrew_installed.formula);
  if (inputs.homebrew_archive) args.push('--brew-archive', inputs.homebrew_archive);
  if (inputs.homebrew_directory) args.push('--brew-directory', inputs.homebrew_directory);
  const hauler = inputs.hauler_selection;
  if (hauler?.all_images) args.push('--all-images');
  for (const reference of hauler?.images || []) args.push('--image', reference);
  for (const manifest of hauler?.manifests || []) args.push('--hauler-manifest', manifest);
  for (const file of hauler?.files || []) args.push('--hauler-file', ...file);
  if (agentEntries.length) args.push('--agent-selection', JSON.stringify({ format: 'josh-room.save-agent-selection.v1', entries: agentEntries }));
  const request = { format_version: 1, components };
  return { request, componentOptions: { component_selection: request, ...inputs }, args: ['--component-selection', JSON.stringify(request), ...args], allImages: args.includes('--all-images') };
}

function planText(preview) {
  const agents = (preview.agent_plan?.entries || []).map(row => `${row.tool} HISTORY · profile ${row.profile_ref} · content ${row.content_sha256} · new point capture ${row.new_point_capture_id}${row.existing_point ? ` · existing point ${row.existing_point.outcome} ${row.existing_point.capture_id}` : ""}`).join("\n");
  const components = Object.entries(preview.component_plan || {}).map(([name, row]) => `${name}: ${row.mode} — ${row.state}${row.restore_mode ? ` · ${row.restore_mode} · ${row.platform?.ID} ${row.platform?.VERSION_ID} ${row.platform?.arch} · ${row.prefix} · ${row.estimated_logical_bytes} logical bytes${row.packages ? ' · ' + row.packages.map(item => `${item.name}@${item.version} (${item.role})`).join(', ') : ''}` : ''}`).join('\n');
  return [components, agents].filter(Boolean).join('\n');
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
