#!/usr/bin/env bash
set -euo pipefail

room_root=$1
controller_root=${JOSH_ROOM_CONTROLLER_ROOT:-/home/vscode/.local/share/josh-room/controller}

test -f "$room_root/.devcontainer/prepare-workspace.sh"
test -f "$room_root/src/josh_room/__init__.py"

mkdir -p "$(dirname "$controller_root")"
rm -rf -- "$controller_root"
cp -a -- "$room_root" "$controller_root"

find "$room_root" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
