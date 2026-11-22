"use strict";
const path = require("node:path");
const { execFile } = require("node:child_process");

// Only metadata crosses this process boundary. Identity bytes are written by
// the caller after both native ACLs are protected; the controller verifies them.
const ACL_SCRIPT = `
$ErrorActionPreference = 'Stop'
$stage = 'metadata'
try {
$root = $env:JOSH_ROOM_HANDOFF_ROOT
$item = Get-Item -LiteralPath $root -Force
if (!$item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Unsafe handoff root' }
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User
$system = [Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$stage = 'directory-policy'
$directory = [Security.AccessControl.DirectorySecurity]::new()
$directory.SetOwner($user)
$directory.SetAccessRuleProtection($true, $false)
foreach ($sid in @($user, $system)) {
  $directory.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
$stage = 'directory-apply'
$item.SetAccessControl($directory)
$stage = 'file-create'
$file = Join-Path $root 'material.identity'
$handle = [IO.File]::Open($file, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
$handle.Dispose()
$stage = 'file-policy'
$acl = [Security.AccessControl.FileSecurity]::new()
$acl.SetOwner($user)
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($user, $system)) {
  $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'Allow'))
}
$stage = 'file-apply'
[IO.FileInfo]::new($file).SetAccessControl($acl)
} catch {
  $failure = $_.Exception.GetBaseException()
  [Console]::Error.WriteLine(('JR_HANDOFF:{0}:{1}:{2}' -f $stage, $failure.GetType().Name, $failure.HResult))
  exit 1
}
`;

function prepareWindowsHandoff(directory, { execute = execFile, systemRoot = process.env.SystemRoot } = {}) {
  if (!systemRoot || !path.win32.isAbsolute(systemRoot)) return Promise.reject(new Error("Windows ACL helper unavailable"));
  const executable = path.win32.join(systemRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
  return new Promise((resolve, reject) => {
    execute(executable, ["-NoProfile", "-NonInteractive", "-OutputFormat", "Text", "-Command", ACL_SCRIPT], {
      env: { ...process.env, JOSH_ROOM_HANDOFF_ROOT: directory },
      windowsHide: true, timeout: 10000, maxBuffer: 65536,
    }, (error, _stdout, stderr) => {
      if (!error) return resolve();
      const boundary = /JR_HANDOFF:([a-z-]+):([A-Za-z0-9]+):(-?\d+)/.exec(String(stderr));
      const detail = boundary ? boundary.slice(1).join("/") : "launch-or-parse";
      reject(new Error(`Native private handoff preparation failed (${detail})`));
    });
  });
}
module.exports = { prepareWindowsHandoff };
