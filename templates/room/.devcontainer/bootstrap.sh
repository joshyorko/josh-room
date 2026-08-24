#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

trust_tap() {
    local tap="$1"
    if brew help trust >/dev/null 2>&1; then
        brew trust --tap "$tap"
    fi
}

brew tap joshyorko/tools
trust_tap joshyorko/tools
brew install age uv libsecret
brew install --cask joshyorko/tools/rcc joshyorko/tools/action-server
uv tool install --force git+https://github.com/joshyorko/josh-room.git@main

jat_root="${JOSH_ROOM_JAT_ROOT:-$HOME/.local/share/josh-room/josh-all-the-things}"
if [[ ! -f "$jat_root/robot.yaml" ]]; then
    mkdir -p "$(dirname "$jat_root")"
    if [[ -e "$jat_root" ]]; then
        printf 'Refusing unexplained partial JAT path: %s\n' "$jat_root" >&2
        exit 1
    fi
    git clone --depth 1 https://github.com/joshyorko/josh-all-the-things "$jat_root"
fi

CONDA_PREFIX="${CONDA_PREFIX:-$HOME/.local/share/josh-room/jat-runtime}" \
    bash "$jat_root/scripts/install_dependencies.sh" 1
rcc ht vars --robot "$jat_root/robot.yaml" --json >/dev/null
test -f "$jat_root/tasks.py"
for task in Build Restore Serve 3tc; do
    grep -q "^  ${task}:" "$jat_root/robot.yaml"
done
grep -q 'python -m jat.cli' "$jat_root/robot.yaml"

printf 'Josh Room bootstrap complete; no JAT workload was run.\n'
