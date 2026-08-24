#!/usr/bin/env bash
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

readonly JOSH_ROOM_GIT_SHA="9bae799f7dcfb394aba3893b29aad201e24c5716"
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

readonly RUNTIME_SECRET_NAME="josh-room-runtime-josh-room"
service_account=/var/run/secrets/kubernetes.io/serviceaccount
[[ -r "$service_account/token" && -r "$service_account/ca.crt" && -r "$service_account/namespace" ]] || {
    printf 'Kubernetes service-account authority is unavailable.\n' >&2
    exit 1
}
runtime_dir=$(mktemp -d /tmp/josh-room-runtime.XXXXXX)
chmod 700 "$runtime_dir"
runtime_response="$runtime_dir/secret-response.json"
runtime_curl="$runtime_dir/curl.conf"
namespace=$(cat "$service_account/namespace")
token=$(cat "$service_account/token")
cat >"$runtime_curl" <<EOF
silent
show-error
fail-with-body
url = "https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT_HTTPS}/api/v1/namespaces/${namespace}/secrets/${RUNTIME_SECRET_NAME}"
header = "Authorization: Bearer ${token}"
cacert = "${service_account}/ca.crt"
output = "${runtime_response}"
EOF
chmod 600 "$runtime_curl"
curl --config "$runtime_curl"
for field in access-key-id secret-access-key session-token age-identity; do
    jq -er --arg field "$field" '.data[$field]' "$runtime_response" | base64 -d >"$runtime_dir/$field"
    chmod 600 "$runtime_dir/$field"
done
jq -n \
    --rawfile access "$runtime_dir/access-key-id" \
    --rawfile secret "$runtime_dir/secret-access-key" \
    --rawfile session "$runtime_dir/session-token" \
    '{"access-key-id":($access|rtrimstr("\n")),"secret-access-key":($secret|rtrimstr("\n")),"session-token":($session|rtrimstr("\n"))}' \
    >"$runtime_dir/r2.json"
chmod 600 "$runtime_dir/r2.json"
cat >"$runtime_curl" <<EOF
silent
show-error
fail-with-body
request = "DELETE"
url = "https://${KUBERNETES_SERVICE_HOST}:${KUBERNETES_SERVICE_PORT_HTTPS}/api/v1/namespaces/${namespace}/secrets/${RUNTIME_SECRET_NAME}"
header = "Authorization: Bearer ${token}"
cacert = "${service_account}/ca.crt"
output = "/dev/null"
EOF
curl --config "$runtime_curl"
rm -f "$runtime_curl" "$runtime_response" "$runtime_dir/access-key-id" "$runtime_dir/secret-access-key" "$runtime_dir/session-token"
export JOSH_ROOM_RUNTIME_CREDENTIALS="$runtime_dir/r2.json"
export JOSH_ROOM_IDENTITY="$runtime_dir/age-identity"
zshenv="$HOME/.zshenv"
zshenv_temp=$(mktemp "$HOME/.zshenv.XXXXXX")
grep -v '^export JOSH_ROOM_\(RUNTIME_CREDENTIALS\|IDENTITY\)=' "$zshenv" >"$zshenv_temp" 2>/dev/null || true
printf 'export JOSH_ROOM_RUNTIME_CREDENTIALS=%q\n' "$JOSH_ROOM_RUNTIME_CREDENTIALS" >>"$zshenv_temp"
printf 'export JOSH_ROOM_IDENTITY=%q\n' "$JOSH_ROOM_IDENTITY" >>"$zshenv_temp"
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
