#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

readonly JOSH_ROOM_GIT_SHA="11b229d281398601323163e99869a6e63b44f51a"
readonly JAT_GIT_SHA="67c3b78c550874a443f56d291e5bfec66dc136b0"
readonly EXPECTED_RCC_VERSION="v18.18.1"

mapfile -d '' host_buses < <(
    find /run/josh-room/host-runtime -mindepth 2 -maxdepth 2 -type s -name bus -print0 2>/dev/null
)
if ((${#host_buses[@]} != 1)); then
    printf 'Expected exactly one host Secret Service session bus; found %s.\n' "${#host_buses[@]}" >&2
    exit 1
fi
export DBUS_SESSION_BUS_ADDRESS="unix:path=${host_buses[0]}"
zshenv="$HOME/.zshenv"
zshenv_temp=$(mktemp "$HOME/.zshenv.XXXXXX")
grep -v '^export DBUS_SESSION_BUS_ADDRESS=unix:path=/run/josh-room/host-runtime/' "$zshenv" >"$zshenv_temp" 2>/dev/null || true
printf 'export DBUS_SESSION_BUS_ADDRESS=%q\n' "$DBUS_SESSION_BUS_ADDRESS" >>"$zshenv_temp"
mv "$zshenv_temp" "$zshenv"

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
