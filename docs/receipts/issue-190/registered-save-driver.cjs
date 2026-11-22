const assert=require('node:assert/strict'); const fs=require('node:fs'); const path=require('node:path'); const vscode=require('vscode');
const base=process.env.ROOM190_ACCEPTANCE_ROOT; assert.ok(base);
const python=process.env.ROOM190_ACCEPTANCE_PYTHON; assert.ok(python);
async function session(){
 const tabs=await(await fetch('http://127.0.0.1:19390/json/list')).json(); const tab=tabs.find(t=>t.type==='page'&&t.url.includes('workbench')); assert.ok(tab);
 const socket=new WebSocket(tab.webSocketDebuggerUrl); await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
 let id=0;const pending=new Map();socket.addEventListener('message',event=>{const result=JSON.parse(event.data);if(result.id){const p=pending.get(result.id);pending.delete(result.id);result.error?p.reject(result.error):p.resolve(result.result);}});
 return{call(method,params={}){return new Promise((resolve,reject)=>{const seq=++id;pending.set(seq,{resolve,reject});socket.send(JSON.stringify({id:seq,method,params}));});},close(){socket.close();}};
}
exports.run=async()=>{
 let cdp; const receipt={runtime:'explicit local-build-fallback Python override; NOT managed RCC artifact', provider:'verified real loopback MinIO',secretService:'isolated task-only production Secret Service',stages:[]};
 try{
  const installed=vscode.extensions.getExtension('joshyorko.josh-room');assert.ok(installed);
  const extension=require(path.join(installed.extensionPath,'extension.js'));
  extension.__test__.setRuntimeForTests({jatRoot:base+'/no-jat-runtime',command:python,args:args=>['-m','josh_room',...args,'--json'],env:{JOSH_ROOM_JAT_ROOT:base+'/no-jat-runtime',PYTHONPATH:path.join(installed.extensionPath,'runtime/controller'),DBUS_SESSION_BUS_ADDRESS:'unix:path='+base+'/keyring-runtime/bus',JOSH_ROOM_CONFIG_DIR:base+'/provider-config',JOSH_ROOM_INSTANCE:base+'/provider-instance',XDG_STATE_HOME:base+'/provider-state',XDG_CACHE_HOME:base+'/provider-cache',XDG_RUNTIME_DIR:base+'/provider-runtime',PATH:base+'/josh-room-runtime/restic/0.19.1/linux-x64:'+process.env.PATH},mode:'local-build-fallback'});
  await installed.activate();cdp=await session();
  const evaluate=async(expression,awaitPromise=false)=>{const r=await cdp.call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise});if(r.exceptionDetails)throw new Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value;};
  const wait=async condition=>evaluate(`new Promise((resolve,reject)=>{const check=()=>{if(${condition}){observer.disconnect();clearTimeout(timer);resolve(true);}};const observer=new MutationObserver(check);observer.observe(document.body,{childList:true,subtree:true,characterData:true});const timer=setTimeout(()=>{observer.disconnect();reject(new Error('native UI condition timed out'));},45000);check();})`,true);
  const accept=()=>vscode.commands.executeCommand('workbench.action.acceptSelectedQuickOpenItem');
  const marker=()=>JSON.parse(fs.readFileSync(base+'/provider-workspace/.josh-room.json','utf8'));
  async function save(expect,decision='Save'){
   const resultPromise=vscode.commands.executeCommand('joshRoom.save');
   await wait(`document.querySelector('.quick-input-title')?.textContent.includes('What should this recovery point preserve?')`);await accept();
   await wait(`document.querySelector('.quick-input-title')?.textContent==='Homebrew recovery export'`);await accept();
   await wait(`document.querySelector('.quick-input-title')?.textContent==='OCI / Hauler content'`);await accept();
   await wait(`[...document.querySelectorAll('.notification-list-item')].some(n=>n.textContent.includes('Save Preview')&&n.textContent.includes('component'))`);
   const preview=await evaluate(`[...document.querySelectorAll('.notification-list-item')].find(n=>n.textContent.includes('Save Preview'))?.innerText`);
   assert.match(preview,/rcc_environment/);assert.match(preview,/homebrew_recovery/);assert.match(preview,/hauler_content/);
   await vscode.commands.executeCommand('workbench.action.closePanel');
   await evaluate(`(()=>{const n=[...document.querySelectorAll('.notification-list-item')].find(n=>n.textContent.includes('Save Preview'));n?.querySelector('.codicon-chevron-down')?.click();})()`);
   await new Promise(resolve=>setTimeout(resolve,500));
   const screen=await cdp.call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(base+'/installed-save-preview.png',Buffer.from(screen.data,'base64'));
   assert.equal(await evaluate(`(()=>{const n=[...document.querySelectorAll('.notification-list-item')].find(n=>n.textContent.includes('Save Preview'));const b=[...n.querySelectorAll('.monaco-button')].find(b=>b.textContent.trim()===${JSON.stringify(decision)});if(!b)return false;b.click();return true;})()`),true);
   if(decision==='Save'){
    await wait(`[...document.querySelectorAll('.notification-list-item')].some(n=>n.textContent.includes('Saved “')||n.textContent.includes('Already saved'))`);
    await evaluate(`(()=>{const n=[...document.querySelectorAll('.notification-list-item')].find(n=>n.textContent.includes('Saved “')||n.textContent.includes('Already saved'));const b=[...n.querySelectorAll('.monaco-button')].find(b=>b.textContent.trim()==='Show Details');b?.click();})()`);
   }
   const result=await resultPromise;assert.equal(result,expect);receipt.stages.push({registeredCommand:'joshRoom.save',result,preview,markerSnapshot:marker().snapshot_id});
  }
  const before=marker().snapshot_id;fs.writeFileSync(base+'/provider-workspace/changed.txt','registered Save fourth\n');
  await save('saved');const saved=marker().snapshot_id;assert.notEqual(saved,before);
  await save('already-saved');assert.equal(marker().snapshot_id,saved);
  fs.writeFileSync(base+'/provider-workspace/changed.txt','cancelled fifth\n');await save('cancelled','Cancel');assert.equal(marker().snapshot_id,saved);
  receipt.ok=true;receipt.sourceVersion=installed.packageJSON.version;receipt.initialSnapshot=before;receipt.savedSnapshot=saved;
 }catch(error){receipt.ok=false;receipt.error=String(error?.message||error);if(cdp){const screen=await cdp.call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(base+'/installed-save-failure.png',Buffer.from(screen.data,'base64'));}throw error;}
 finally{fs.writeFileSync(base+'/installed-save-receipt.json',JSON.stringify(receipt,null,2));cdp?.close();}
};
