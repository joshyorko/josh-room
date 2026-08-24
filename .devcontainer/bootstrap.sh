#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

readonly JOSH_ROOM_GIT_SHA="785182c0064dd6a5c8b0e517b0ca93e97d237553"
readonly JAT_GIT_SHA="d1e1c3eb107cebe7273d3792aeaffdef4488ce44"
readonly EXPECTED_RCC_VERSION="v18.18.1"
readonly JAT_HOLOLIB_REFERENCE="ghcr.io/joshyorko/josh-all-the-things-hololib@sha256:f41f5113bdd23e8985462b43ad3ff2d5b9e7921050e0ce9859fe312b23946ae2"
readonly JAT_HOLOLIB_ZIP_SHA256="1f99c793de54db6f80d8bdda86859ac38089d49e4b75d691b0595ff78b265bc1"
readonly JAT_HOLOLIB_ZIP_SIZE="226010832"
readonly JAT_HOLOLIB_ENVIRONMENT_HASH="c831b0c6ab000a0c"

trust_tap() {
    local tap="$1"
    if brew help trust >/dev/null 2>&1; then
        brew trust --tap "$tap"
    fi
}

brew tap joshyorko/tools
trust_tap joshyorko/tools
brew install age uv libsecret jq oras
brew install --cask joshyorko/tools/rcc@18.18.1 joshyorko/tools/action-server
hash -r
test "$(rcc version | head -n 1)" = "$EXPECTED_RCC_VERSION"

sudo "$(command -v rcc)" ht shared --enable --once
rcc ht init

uv tool install --force "git+https://github.com/joshyorko/josh-room.git@${JOSH_ROOM_GIT_SHA}"

extension_source="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/vscode-extension"
extension_target="$HOME/.vscode-server-insiders/extensions/joshyorko.josh-room-0.1.0"
test -f "$extension_source/extension.js"
mkdir -p "$(dirname "$extension_target")"
rm -rf -- "$extension_target"
cp -a -- "$extension_source" "$extension_target"

jat_root="${JOSH_ROOM_JAT_ROOT:-$HOME/.local/share/josh-room/josh-all-the-things}"
if [[ ! -f "$jat_root/robot.yaml" ]]; then
    mkdir -p "$(dirname "$jat_root")"
    if [[ -e "$jat_root" ]]; then
        printf 'Refusing unexplained partial JAT path: %s\n' "$jat_root" >&2
        exit 1
    fi
    git init -q "$jat_root"
    git -C "$jat_root" remote add origin https://github.com/joshyorko/josh-all-the-things
    git -C "$jat_root" fetch -q --depth 1 origin "$JAT_GIT_SHA"
    git -C "$jat_root" checkout -q --detach FETCH_HEAD
fi
test "$(git -C "$jat_root" rev-parse HEAD)" = "$JAT_GIT_SHA"

CONDA_PREFIX="${CONDA_PREFIX:-$HOME/.local/share/josh-room/jat-runtime}" \
    bash "$jat_root/scripts/install_dependencies.sh" 1
export JAT_GIT_SHA EXPECTED_RCC_VERSION JAT_HOLOLIB_REFERENCE JAT_HOLOLIB_ZIP_SHA256 JAT_HOLOLIB_ZIP_SIZE JAT_HOLOLIB_ENVIRONMENT_HASH
bash "$(dirname "${BASH_SOURCE[0]}")/bootstrap-jat-hololib.sh" "$jat_root/robot.yaml"
test -f "$jat_root/tasks.py"
for task in Build Restore Serve JAT; do
    grep -q "^  ${task}:" "$jat_root/robot.yaml"
done
grep -q 'python -m jat.cli' "$jat_root/robot.yaml"

printf 'Josh Room bootstrap complete; no JAT workload was run.\n'
