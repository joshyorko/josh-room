param(
    [Parameter(Mandatory = $true)][string]$CodeRoot,
    [Parameter(Mandatory = $true)][string]$Profile,
    [Parameter(Mandatory = $true)][string]$Extensions,
    [Parameter(Mandatory = $true)][string]$Candidate
)
$ErrorActionPreference = 'Stop'
# The official wrapper resolves the app's revision directory and clears VSCODE_DEV.
& (Join-Path $CodeRoot 'bin/code.cmd') --user-data-dir $Profile --extensions-dir $Extensions --install-extension $Candidate --force
if ($LASTEXITCODE -ne 0) { throw 'Official VS Code CLI installation failed' }
