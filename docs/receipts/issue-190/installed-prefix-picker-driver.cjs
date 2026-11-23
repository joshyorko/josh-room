const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vscode = require('vscode');
const taskRoot = process.env.ROOM190_ACCEPTANCE_ROOT;
assert.ok(taskRoot, 'Set ROOM190_ACCEPTANCE_ROOT to a disposable synthetic task directory');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function cdpSession() {
  const tabs = await (await fetch('http://127.0.0.1:19592/json/list')).json();
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
 const receipt={runtime:'installed VSIX native picker; capability from actual installed Debian fixture CLI; Save not invoked'};
 try {
  const installed=vscode.extensions.getExtension('joshyorko.josh-room');assert.ok(installed);await installed.activate();
  const picker=require(path.join(installed.extensionPath,'component-picker.js'));
  cdp=await cdpSession();
  const capability=JSON.parse(require('node:child_process').execFileSync('docker',['exec','-e','PYTHONUSERBASE=/task/provider/cli-user','codex190-prefix-source-tools-20261122','python','-m','josh_room','snapshot','homebrew-capabilities','--json'],{encoding:'utf8'}));
  assert.equal(capability.homebrew_installed.available,true);
  const promise=picker.pickComponents(vscode,path.join(taskRoot,'ui-workspace'),undefined,async()=>capability);
  await pause(700);
  const rendered=await cdp.call('Runtime.evaluate',{expression:'document.querySelector(".quick-input-widget")?.innerText',returnByValue:true});
  assert.match(rendered.result.value,/Installed Homebrew Hello/);
  assert.match(rendered.result.value,/Codex sessions/);
  const screen=await cdp.call('Page.captureScreenshot',{format:'png'});
  fs.writeFileSync(path.join(taskRoot,'prefix-installed-picker.png'),Buffer.from(screen.data,'base64'));
  await vscode.commands.executeCommand('workbench.action.closeQuickOpen');assert.equal(await promise,undefined);
  receipt.ok=true;receipt.positiveRowRendered=true;receipt.cancellationPassed=true;receipt.capability=capability;receipt.installedVersion=installed.packageJSON.version;
 } finally { fs.writeFileSync(path.join(taskRoot,'prefix-installed-picker.json'),JSON.stringify(receipt,null,2));cdp?.close(); }
};
