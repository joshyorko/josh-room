#!/usr/bin/env bash
set -euo pipefail

controller_root=$1
test -f "$controller_root/.vscode/tasks.json"
mkdir -p /workspaces/room/.vscode
cp "$controller_root/.vscode/tasks.json" /workspaces/room/.vscode/tasks.json

