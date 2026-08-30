#!/usr/bin/env bash
set -euo pipefail

room_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
extension_source="$room_root/vscode-extension"
extension_base="${VSCODE_EXTENSIONS_DIR:-$HOME/.vscode-server-insiders/extensions}"
extension_target="$extension_base/joshyorko.josh-room-0.1.14"

test -f "$extension_source/extension.js"
test -f "$extension_source/package.json"
mkdir -p "$extension_base"
if [[ -e "$extension_target" && ! -d "$extension_target" ]]; then
    printf 'Refusing non-directory extension target: %s\n' "$extension_target" >&2
    exit 1
fi
rm -rf -- "$extension_target"
cp -a -- "$extension_source" "$extension_target"

printf 'Optional golden-host extension copy complete. Josh Room acquires RCC, JAT, and Hauler on first use.\n'
