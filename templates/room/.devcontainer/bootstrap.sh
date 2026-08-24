#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

readonly JOSH_ROOM_GIT_SHA="86c289beae380a959262ec3ddd9c57fedcbac418"
readonly JAT_GIT_SHA="67c3b78c550874a443f56d291e5bfec66dc136b0"
readonly EXPECTED_RCC_VERSION="v18.18.1"

trust_tap() {
    local tap="$1"
    if brew help trust >/dev/null 2>&1; then
        brew trust --tap "$tap"
    fi
}

brew tap joshyorko/tools
trust_tap joshyorko/tools
brew install age uv libsecret jq
brew install --cask joshyorko/tools/rcc joshyorko/tools/action-server

mv "$zshenv_temp" "$zshenv"
uv tool install --force "git+https://github.com/joshyorko/josh-room.git@${JOSH_ROOM_GIT_SHA}"

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
rcc ht vars --robot "$jat_root/robot.yaml" --json >/dev/null
test "$(rcc version | head -n 1)" = "$EXPECTED_RCC_VERSION"
test -f "$jat_root/tasks.py"
for task in Build Restore Serve JAT; do
    grep -q "^  ${task}:" "$jat_root/robot.yaml"
done
grep -q 'python -m jat.cli' "$jat_root/robot.yaml"

printf 'Josh Room bootstrap complete; no JAT workload was run.\n'
