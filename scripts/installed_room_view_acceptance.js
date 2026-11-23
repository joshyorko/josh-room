const vscode = require('vscode');
const fs = require('node:fs');
const path = require('node:path');
const probe = require('./installed_room_view_probe');
// Run as VS Code --extensionTestsPath in a disposable development-host profile.
// The fixture must be an open, trusted synthetic-room in synthetic-dimension.
// Only browser handoff is intercepted; runtime acquisition and serving are real.
const base = process.env.JOSH_ROOM_ACCEPTANCE_ROOT;
if (!base || !path.isAbsolute(base)) throw new Error('Set an absolute task-private JOSH_ROOM_ACCEPTANCE_ROOT');
const record = value => fs.appendFileSync(path.join(base,'installed.jsonl'), JSON.stringify(value)+'\n');
exports.run = async () => {
  let url;
  let serverUrl;
  let desktopVerified = false;
  let launchedSession;
  const desktopCommand = process.env.JOSH_ROOM_DESKTOP_PROBE_COMMAND;
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
      const session = await original({...options,vscode:{workspace:options.vscode.workspace,Uri:options.vscode.Uri,UIKind:options.vscode.UIKind,env:{uiKind:options.vscode.env.uiKind,remoteName:options.vscode.env.remoteName,
        asExternalUri: options.vscode.env.asExternalUri && (async uri => {
          serverUrl = uri.toString(true);
          return options.vscode.env.asExternalUri(uri);
        }), openExternal: async uri => {
        url = uri.toString(true);
        if (options.vscode.env.remoteName && !desktopCommand) throw new Error('Remote acceptance requires a desktop-side probe command');
        const result = desktopCommand ? await vscode.commands.executeCommand(desktopCommand, { action: 'probe', url }) : await probe.probe(url);
        if (!result || result.root !== 200 || result.state !== 200 || result.hasFixture !== true || result.unauthenticated !== 403 || result.post !== 405) throw new Error('Desktop HTTP verification did not pass');
        desktopVerified = true;
        record({ stage: 'desktop-http', remote: Boolean(options.vscode.env.remoteName), ...result });
        // Test observes the browser handoff, not visible rendering.
        return true;
      }}}});
      launchedSession = session;
      return session;
    };
    await target.activate();
    const outcome = await Promise.race([vscode.commands.executeCommand('joshRoom.explore3D',{kind:'room',project:{id:'synthetic-room'},dimension:{id:'synthetic-dimension'}}),new Promise((_,reject)=>{ deadline=setTimeout(()=>reject(new Error('Installed launch deadline exceeded')),180000); })]);
    if (outcome === 'failed' || outcome === 'cancelled' || !url || !desktopVerified || !launchedSession?.active) throw new Error('Installed Explore did not complete a verified desktop handoff');
    await vscode.commands.executeCommand('joshRoom.stopRoomView');
    const stopped = await probe.isListenerStopped(serverUrl || url);
    const desktopStopped = desktopCommand ? await vscode.commands.executeCommand(desktopCommand, { action: 'stopped', url }) : stopped;
    if (!desktopStopped) throw new Error('Forwarded viewer still serves data after Stop');
    record({stage:'stopped',stopped,desktopStopped});
    if(!stopped) throw new Error('Viewer listener survived Stop');
  } catch(error) { record({stage:'FAIL',message:error.message}); throw error; }
  finally {
    clearTimeout(deadline);
    await vscode.commands.executeCommand('joshRoom.stopRoomView').catch(() => {});
    if (helper && original) helper.launchRoomView = original;
  }
};
