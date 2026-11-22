const vscode=require('vscode');const fs=require('node:fs');
const base=process.env.JOSH_ROOM_ACCEPTANCE_ROOT;
const target=vscode.extensions.getExtension('joshyorko.josh-room');
const installed=target.extensionPath;
exports.activate=async function(context){
 const log=entry=>fs.appendFileSync(base+'/production-ui-receipts.jsonl',JSON.stringify(entry)+'\n');
 try{
  const extension=require(installed+'/extension');
  extension.__test__.setRuntimeForTests({command:process.env.JOSH_ROOM_ACCEPTANCE_PYTHON,args:args=>['-m','josh_room',...args,'--json'],env:{PYTHONPATH:installed+'/runtime/controller',DBUS_SESSION_BUS_ADDRESS:'unix:path='+base+'/keyring-runtime/bus',JOSH_ROOM_CONFIG_DIR:base+'/provider-config',XDG_STATE_HOME:base+'/provider-state',XDG_CACHE_HOME:base+'/provider-cache',XDG_RUNTIME_DIR:base+'/provider-runtime'},mode:'local-build-fallback'});
  await vscode.extensions.getExtension('joshyorko.josh-room').activate();
  context.subscriptions.push(vscode.workspace.onDidOpenTextDocument(doc=>{try{const result=JSON.parse(doc.getText());if(result.snapshot_id)log({stage:'visible-result',result});}catch{}}));
  const pins=JSON.parse(fs.readFileSync(base+'/provider-pins.json','utf8'));
  log({stage:'registered-command',command:'joshRoom.browseRecoveryPoint',snapshot_id:pins[0],runtime:'source-controller-override-not-managed-RCC'});
  const result=await vscode.commands.executeCommand('joshRoom.browseRecoveryPoint',{id:'synthetic-room',dimension:{id:'recovery-fixture',provider:'minio'},snapshot_id:pins[0]});
  log({stage:'finished',result});
 }catch(error){log({stage:'error',message:error.message});vscode.window.showErrorMessage(error.message);}
};
