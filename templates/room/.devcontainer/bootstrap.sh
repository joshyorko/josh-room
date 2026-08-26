#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

readonly JOSH_ROOM_GIT_SHA="ea5bcc753247a33e18875030f9178fec33b78942"
readonly JAT_GIT_SHA="0d08869f1e0d267bed72c2a76ff32b376b8e10a1"
readonly EXPECTED_RCC_VERSION="v18.19.2"
readonly JAT_ENVIRONMENT_ARTIFACT_REFERENCE="ghcr.io/joshyorko/josh-all-the-things-jat-runtime@sha256:173cd4fe996af650257651152dac76abd96e9d90ea36b68d0cf7b17b09e02d50"
readonly JAT_ENVIRONMENT_ARTIFACT_MANIFEST_DIGEST="sha256:173cd4fe996af650257651152dac76abd96e9d90ea36b68d0cf7b17b09e02d50"
readonly JAT_ENVIRONMENT_ARTIFACT_ARCHIVE_SHA256="1e9837d5a1cf154a87aece9cbb607a98a468a1445715072559b15b6fde10b94b"
readonly JAT_ENVIRONMENT_ARTIFACT_ARCHIVE_SIZE="230662657"
readonly JAT_ENVIRONMENT_ARTIFACT_DIGEST="sha256:36fa516220d023201cbc989624cf3e9c914ab36f5d206daee95303a876ae25ca"
readonly JAT_ENVIRONMENT_ARTIFACT_SPECIFICATION_DIGEST="sha256:adedfa16d05276fc99b8f7260118180a9e6140931296e87fa70b04454d762cb7"
readonly JAT_ENVIRONMENT_ARTIFACT_LEGACY_BLUEPRINT_KEY="c831b0c6ab000a0c"
readonly JAT_ENVIRONMENT_ARTIFACT_RCC_VERSION="v18.19.2"
readonly JAT_ENVIRONMENT_ARTIFACT_PLATFORM="linux_amd64"

trust_tap() {
    local tap="$1"
    if brew help trust >/dev/null 2>&1; then
        brew trust --tap "$tap"
    fi
}

brew tap joshyorko/tools
trust_tap joshyorko/tools
brew install age uv libsecret jq oras
brew install --cask joshyorko/tools/rcc joshyorko/tools/action-server
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
export JAT_GIT_SHA EXPECTED_RCC_VERSION JAT_ENVIRONMENT_ARTIFACT_REFERENCE JAT_ENVIRONMENT_ARTIFACT_MANIFEST_DIGEST JAT_ENVIRONMENT_ARTIFACT_ARCHIVE_SHA256 JAT_ENVIRONMENT_ARTIFACT_ARCHIVE_SIZE JAT_ENVIRONMENT_ARTIFACT_DIGEST JAT_ENVIRONMENT_ARTIFACT_SPECIFICATION_DIGEST JAT_ENVIRONMENT_ARTIFACT_LEGACY_BLUEPRINT_KEY JAT_ENVIRONMENT_ARTIFACT_RCC_VERSION JAT_ENVIRONMENT_ARTIFACT_PLATFORM
bash "$(dirname "${BASH_SOURCE[0]}")/bootstrap-jat-environment.sh" "$jat_root/robot.yaml"
test -f "$jat_root/tasks.py"
for task in Build Restore Serve JAT; do
    grep -q "^  ${task}:" "$jat_root/robot.yaml"
done
grep -q 'python -m jat.cli' "$jat_root/robot.yaml"

printf 'Josh Room bootstrap complete; no JAT workload was run.\n'
