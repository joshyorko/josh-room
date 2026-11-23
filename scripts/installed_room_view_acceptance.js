const vscode = require('vscode');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
// Run as VS Code --extensionTestsPath in a disposable development-host profile.
// The fixture must be an open, trusted synthetic-room in synthetic-dimension.
// Only browser handoff is intercepted; runtime acquisition and serving are real.
const base = process.env.JOSH_ROOM_ACCEPTANCE_ROOT;
if (!base || !path.isAbsolute(base)) throw new Error('Set an absolute task-private JOSH_ROOM_ACCEPTANCE_ROOT');
const record = value => fs.appendFileSync(path.join(base,'installed.jsonl'), JSON.stringify(value)+'\n');
exports.run = async () => {
  let url;
  let helper;
  let original;
  let deadline;
  try {
    const target = vscode.extensions.getExtension('joshyorko.josh-room');
    if(!target) throw new Error('Install the candidate VSIX into the disposable extensions directory');
    record({stage:'installed',found:!!target});
    helper = require(path.join(target.extensionPath,'room-view.js'));
    original = helper.launchRoomView;
    helper.launchRoomView = async options => {
      if(!options.runtime.controllerArtifact) throw new Error('Managed pinned controller artifact required');
      record({stage:'managed-launch',command: path.basename(options.runtime.command),mode:options.runtime.mode,artifact:options.runtime.controllerArtifact});
      return original({...options,vscode:{workspace:options.vscode.workspace,Uri:options.vscode.Uri,UIKind:options.vscode.UIKind,env:{uiKind:options.vscode.env.uiKind,remoteName:options.vscode.env.remoteName,openExternal: async uri => {
        url = uri.toString(true);
        const status = await new Promise((resolve,reject) => http.get(url,r=>{r.resume();resolve(r.statusCode)}).on('error',reject));
        if(status !== 200) throw new Error('Viewer HTTP status '+status);
        record({stage:'http',status,loopback:new URL(url).hostname==='127.0.0.1',token:!!new URL(url).searchParams.get('token')});
        const request = (target, method = 'GET') => new Promise((resolve, reject) => {
          const req = http.request(target, { method }, response => {
            let body = ''; response.on('data', chunk => { body += chunk; });
            response.on('end', () => resolve({ status: response.statusCode, body }));
          });
          req.on('error', reject); req.end();
        });
        if ((await request(new URL('/', url))).status !== 403) throw new Error('Unauthenticated read was not denied');
        if ((await request(url, 'POST')).status !== 405) throw new Error('POST was not denied');
        const stateUrl = new URL(url); stateUrl.pathname = '/state.json';
        const state = await request(stateUrl);
        if (state.status !== 200 || !state.body.includes('example.txt')) throw new Error('Selected synthetic workspace state missing');
        record({stage:'provider-and-method-guards',state:200,unauthenticated:403,post:405});
        // Test observes the browser handoff, not visible rendering.
        return true;
      }}}});
    };
    await target.activate();
    await Promise.race([vscode.commands.executeCommand('joshRoom.explore3D',{kind:'room',project:{id:'synthetic-room'},dimension:{id:'synthetic-dimension'}}),new Promise((_,reject)=>{ deadline=setTimeout(()=>reject(new Error('Installed launch deadline exceeded')),180000); })]);
    if (!url) throw new Error('No browser URL handed off');
    await vscode.commands.executeCommand('joshRoom.stopRoomView');
    const stopped = await new Promise(resolve=>http.get(url,r=>{r.resume();resolve(false)}).on('error',()=>resolve(true)));
    record({stage:'stopped',stopped});
    if(!stopped) throw new Error('Viewer listener survived Stop');
  } catch(error) { record({stage:'FAIL',message:error.message}); throw error; }
  finally {
    clearTimeout(deadline);
    await vscode.commands.executeCommand('joshRoom.stopRoomView');
    if (helper && original) helper.launchRoomView = original;
  }
};
