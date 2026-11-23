const test = require('node:test');
const assert = require('node:assert/strict');
const { pickComponents, planText } = require('./component-picker');
const admitted = { agent_save: { contract_version: 1, mode: 'history-only', available: true, profiles: [
  { tool: 'codex', profile_ref: 'a'.repeat(64), scope: 'personal', admitted: true },
  { tool: 'claude', profile_ref: 'b'.repeat(64), scope: 'oss', admitted: true },
] } };
test('two history tools require explicit opaque profiles and never persist opt-in', async () => {
  const v = mock([rows => rows.filter(r => r.tool), rows => rows[0], rows => rows[0]]);
  const result = await pickComponents(v, '/synthetic/source', undefined, undefined, async () => admitted);
  assert.ok(v.calls[0].rows.filter(r => r.tool).every(r => !r.picked && /HISTORY/.test(r.label)));
  assert.deepEqual(JSON.parse(result.args[result.args.indexOf('--agent-selection') + 1]), {
    format: 'josh-room.save-agent-selection.v1', entries: [
      { tool: 'codex', profile_ref: 'a'.repeat(64) }, { tool: 'claude', profile_ref: 'b'.repeat(64) },
    ],
  });
  let persisted;
  await require('./component-picker').rememberChoice({ update: async (_k, value) => { persisted = value; } }, 'synthetic', result);
  assert.deepEqual(Object.keys(persisted), ['request']);
  assert.deepEqual(Object.keys(persisted.request.components).sort(), ['hauler_content', 'homebrew_recovery', 'rcc_environment']);
});
test('unknown admission version and cancelled profile selection authorize no history', async () => {
  const v = mock([[]]);
  await pickComponents(v, '/synthetic/source', undefined, undefined, async () => ({ agent_save: { ...admitted.agent_save, contract_version: 2 } }));
  assert.equal(v.calls[0].rows.some(r => r.tool), false);
  const cancelled = mock([rows => rows.filter(r => r.tool === 'codex'), undefined]);
  assert.equal(await pickComponents(cancelled, '/synthetic/source', undefined, undefined, async () => admitted), undefined);
});
function mock(answers, files = []) {
  const calls = [];
  return { calls, window: {
    showQuickPick: async (rows, options) => { calls.push({ rows, options }); const next = answers.shift(); return typeof next === 'function' ? next(rows) : next; },
    showOpenDialog: async () => files.shift(),
    showWarningMessage: async message => calls.push({ message }),
  } };
}
test('mandatory workspace cannot be omitted; unavailable capture never becomes an argument', async () => {
  const v = mock([rows => [rows.find(r => r.unavailable)], []]);
  const result = await pickComponents(v, '/synthetic/absent');
  assert.deepEqual(Object.values(result.request.components), ['omit', 'omit', 'omit']);
  assert.equal(v.calls[0].options.canPickMany, true);
  assert.match(v.calls[1].message, /unavailable/);
  assert.equal(result.args.length, 2);
});
test('one request supplies identical preview and create arguments for a selected recipe', async () => {
  const v = mock([rows => rows.filter(r => r.id === 'homebrew_recovery'), 'Select Brewfile-only recipe directory'], [[{ fsPath: '/synthetic/recipe' }]]);
  const result = await pickComponents(v, '/synthetic/absent');
  assert.equal(result.request.components.homebrew_recovery, 'refresh');
  assert.deepEqual(result.args.slice(-2), ['--brew-directory', '/synthetic/recipe']);
  assert.deepEqual(JSON.parse(result.args[1]), result.request);
});
test('cancellation does not return a partial capture request', async () => {
  const v = mock([rows => rows.filter(r => r.id === 'homebrew_recovery'), undefined]);
  assert.equal(await pickComponents(v, '/synthetic/absent'), undefined);
});
test('preview discloses keep and pending without claiming successful capture', () => {
  assert.equal(planText({ component_plan: { rcc_environment: { mode: 'refresh', state: 'pending' } } }), 'rcc_environment: refresh — pending');
});
test('host defaults persist modes only and reject malformed saved requests', async () => {
  const { storedChoice, rememberChoice } = require('./component-picker');
  const saved = new Map();
  const state = { get: key => saved.get(key), update: async (key, value) => saved.set(key, value) };
  const choice = await pickComponents(mock([[]]), '/synthetic/absent');
  choice.args.push('--brew-archive', '/synthetic/private-path');
  await rememberChoice(state, 'synthetic-binding', choice);
  assert.deepEqual(storedChoice(state, 'synthetic-binding').request, choice.request);
  assert.equal(JSON.stringify([...saved]).includes('private-path'), false);
  assert.equal(storedChoice(state, 'other-binding'), undefined);
  saved.set([...saved.keys()][0], { request: { format_version: 1, components: { auth: 'refresh' } } });
  assert.equal(storedChoice(state, 'synthetic-binding'), undefined);
});
test('qualified installed closure uses existing Homebrew slot and explicit source flags', async () => {
  const v = mock([rows => rows.filter(r => r.id === 'homebrew_installed'), 'Use /home/linuxbrew/.linuxbrew']);
  v.window.showInputBox = async () => 'hello';
  const result = await pickComponents(v,'/synthetic/source',undefined,async()=>({homebrew_installed:{available:true,format:'josh-room-homebrew-installed-v1',prefix:'/home/linuxbrew/.linuxbrew'}}));
  assert.equal(result.request.components.homebrew_recovery,'refresh');
  assert.deepEqual(result.args.slice(-4),['--brew-installed-prefix','/home/linuxbrew/.linuxbrew','--brew-formula','hello']);
  assert.equal(v.calls[0].rows.some(row=>row.id==='homebrew_installed'),true);
});
test('missing or failed installed capability stays unavailable', async () => {
  const v = mock([[]]);
  await pickComponents(v,'/synthetic/source',undefined,async()=>{throw new Error('unavailable');});
  assert.equal(v.calls[0].rows.some(row=>row.id==='homebrew_installed'),false);
});

const detected = { session_inventory: { contract_version: 1, tools: {
  codex: { state: 'complete', exact: 1, descendant: 2 },
  t3: { state: 'complete', exact: 0, descendant: 1 },
} } };
const noGrant = { agent_save: { contract_version: 1, mode: 'history-only', available: false, profiles: [], reason: 'native-room-authority-unavailable' } };

test('detected ordinary sessions disclose exact/child scope without granting capture', async () => {
  const v = mock([rows => rows.filter(r => /Codex HISTORY|T3 Code sessions/.test(r.label)), []]);
  const result = await pickComponents(v, '/synthetic/source', undefined, undefined, async () => noGrant, async () => detected);
  const rows = v.calls[0].rows;
  assert.match(rows.find(r => /Codex HISTORY/.test(r.label)).detail, /1 session\(s\) in this folder; 2 in child folders/);
  assert.match(rows.find(r => /Codex HISTORY/.test(r.label)).detail, /Host recovery admission is missing/);
  assert.match(rows.find(r => /T3 Code sessions/.test(r.label)).detail, /1 in child folders.*#188/);
  assert.ok(rows.filter(r => /Codex HISTORY|T3 Code sessions/.test(r.label)).every(r => r.unavailable && !r.picked));
  assert.equal(result.args.includes('--agent-selection'), false);
});

test('admission failure does not suppress independent session detection', async () => {
  const v = mock([[]]);
  await pickComponents(v, '/synthetic/source', undefined, undefined, async () => { throw new Error('/private/credential'); }, async () => detected);
  const detail = v.calls[0].rows.find(r => /Codex HISTORY/.test(r.label)).detail;
  assert.match(detail, /1 session.*could not be checked/);
  assert.doesNotMatch(detail, /private|credential/);
});

test('no history, unsupported format, incomplete scan and failure stay distinct', async () => {
  for (const [state, expected] of [['complete', /No matching sessions found/], ['unsupported', /format or platform is unsupported/], ['partial', /discovery is incomplete/], ['unavailable', /discovery failed/]]) {
    const v = mock([[]]);
    await pickComponents(v, '/synthetic/source', undefined, undefined, async () => noGrant, async () => ({ session_inventory: { contract_version: 1, tools: { codex: { state, exact: 0, descendant: 0, reason: '/private/error' } } } }));
    const detail = v.calls[0].rows.find(r => /Codex HISTORY/.test(r.label)).detail;
    assert.match(detail, expected);
    assert.doesNotMatch(detail, /private/);
  }
});

test('unknown inventory versions and invalid counts never become found history', async () => {
  for (const value of [{ contract_version: 2, tools: detected.session_inventory.tools }, { contract_version: 1, tools: { codex: { state: 'complete', exact: -1, descendant: 0 } } }]) {
    const v = mock([[]]);
    const result = await pickComponents(v, '/synthetic/source', undefined, undefined, async () => ({ agent_save: { ...noGrant.agent_save, reason: '/private/error' } }), async () => ({ session_inventory: value }));
    const detail = v.calls[0].rows.find(r => /Codex HISTORY/.test(r.label)).detail;
    assert.match(detail, /detection unavailable/);
    assert.doesNotMatch(detail, /private/);
    assert.equal(result.args.includes('--agent-selection'), false);
  }
});

test('admitted profile selection still excludes detected ordinary/child sessions', async () => {
  const v = mock([rows => rows.filter(r => r.tool === 'codex'), rows => rows[0]]);
  const result = await pickComponents(v, '/synthetic/source', undefined, undefined, async () => admitted, async () => detected);
  assert.match(v.calls[0].rows.find(r => r.tool === 'codex').detail, /not ordinary or child-folder sessions/);
  assert.deepEqual(JSON.parse(result.args[result.args.indexOf('--agent-selection') + 1]).entries, [{ tool: 'codex', profile_ref: 'a'.repeat(64) }]);
});

test('missing host directory traverses real Python discovery and adapter into the picker', { skip: process.platform !== 'linux' }, async t => {
  const fs = require('node:fs');
  const path = require('node:path');
  const { spawnSync } = require('node:child_process');
  const root = path.resolve(__dirname, '..');
  const python = path.join(root, '.venv/bin/python');
  if (!fs.existsSync(python)) { t.skip('repository Python environment unavailable'); return; }
  const directory = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'josh-room-picker-'));
  try {
    const code = `import json,sys; from pathlib import Path; from types import SimpleNamespace; from josh_room.native_agent_save import status; from josh_room.native_session_inventory import inventory; p=Path(sys.argv[1]); r=status(None,SimpleNamespace(provider='minio',dimension_id='synthetic'),None,'demo',p); r['session_inventory']=inventory(p); print(json.dumps(r))`;
    const run = spawnSync(python, ['-B', '-c', code, directory], { cwd: root, encoding: 'utf8', env: { ...process.env, XDG_CONFIG_HOME: directory, CODEX_HOME: path.join(directory, 'codex'), T3CODE_HOME: path.join(directory, 't3') } });
    assert.equal(run.status, 0, run.stderr);
    const actual = JSON.parse(run.stdout);
    assert.equal(actual.agent_save.reason, 'native-room-authority-unavailable');
    const v = mock([[]]);
    const result = await pickComponents(v, directory, undefined, undefined, async () => actual, async () => actual);
    assert.match(v.calls[0].rows.find(r => /Codex HISTORY/.test(r.label)).detail, /Host recovery admission is missing/);
    assert.equal(result.args.includes('--agent-selection'), false);
    assert.equal(fs.existsSync(path.join(directory, 'josh-room')), false);
  } finally { fs.rmSync(directory, { recursive: true, force: true }); }
});
