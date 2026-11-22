const test = require('node:test');
const assert = require('node:assert/strict');
const { pickComponents, planText } = require('./component-picker');
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
