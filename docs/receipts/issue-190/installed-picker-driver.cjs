const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vscode = require('vscode');
const taskRoot = process.env.ROOM190_ACCEPTANCE_ROOT;
assert.ok(taskRoot, 'Set ROOM190_ACCEPTANCE_ROOT to a disposable synthetic task directory');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function cdpSession() {
  const tabs = await (await fetch('http://127.0.0.1:19390/json/list')).json();
  const tab = tabs.find(tab => tab.type === 'page' && tab.url.includes('workbench'));
  assert.ok(tab, 'actual VS Code workbench renderer must exist');
  const socket = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  let id = 0;
  const pending = new Map();
  socket.addEventListener('message', event => { const value = JSON.parse(event.data); if (value.id) { const p = pending.get(value.id); pending.delete(value.id); value.error ? p.reject(value.error) : p.resolve(value.result); } });
  return { call(method, params = {}) { return new Promise((resolve, reject) => { const seq = ++id; pending.set(seq, {resolve, reject}); socket.send(JSON.stringify({ id: seq, method, params })); }); }, close() { socket.close(); } };
}
exports.run = async () => {
  let cdp;
  try {
    const installed = vscode.extensions.getExtension('joshyorko.josh-room');
    assert.ok(installed, 'VSIX must be installed');
    await installed.activate();
    const commands = await vscode.commands.getCommands();
    for (const name of ['joshRoom.new', 'joshRoom.save', 'joshRoom.saveFolderAs']) assert.ok(commands.includes(name));
    const picker = require(path.join(installed.extensionPath, 'component-picker.js'));
    cdp = await cdpSession();
    const promise = picker.pickComponents(vscode, path.join(taskRoot, 'synthetic-workspace'));
    await pause(700);
    const rendered = await cdp.call('Runtime.evaluate', { expression: 'document.querySelector(".quick-input-widget")?.innerText', returnByValue: true });
    assert.match(rendered.result.value, /What should this recovery point preserve/);
    assert.match(rendered.result.value, /Homebrew installed prefix/);
    assert.match(rendered.result.value, /Codex sessions/);
    const screen = await cdp.call('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(taskRoot, 'installed-picker.png'), Buffer.from(screen.data, 'base64'));
    for (let index = 0; index < 3; index += 1) {
      await vscode.commands.executeCommand('workbench.action.acceptSelectedQuickOpenItem');
      await pause(300);
    }
    const selection = await Promise.race([promise, pause(5000).then(() => { throw new Error('native chooser did not finish'); })]);
    assert.deepEqual(selection.request.components, { rcc_environment: 'keep', homebrew_recovery: 'keep', hauler_content: 'keep' });
    const cancelled = picker.pickComponents(vscode, path.join(taskRoot, 'synthetic-workspace'));
    await pause(300);
    await vscode.commands.executeCommand('workbench.action.closeQuickOpen');
    assert.equal(await cancelled, undefined);
    const receipt = { installed: true, source: installed.packageJSON.version, platform: process.platform, architecture: process.arch, renderedNativePicker: true, unavailableRowsRendered: true, defaultKeepSelection: selection.request, cancellationPassed: true, fullSave: 'NOT RUN: no qualified synthetic provider/managed runtime in isolated profile', nativeBinaryRestore: 'NOT RUN' };
    fs.writeFileSync(path.join(taskRoot, 'installed-receipt.json'), JSON.stringify(receipt, null, 2));
  } finally { cdp?.close(); }
};
