#!/usr/bin/env bash
set -euo pipefail

readonly SECRET_NAME="josh-room-runtime-josh-room"
readonly ROLE_NAME="josh-room-runtime-josh-room"
readonly TTL_SECONDS=21600

for command in devpod jq secret-tool curl kubectl base64; do
    command -v "$command" >/dev/null 2>&1 || {
        printf 'Josh Room Kubernetes preparation requires %s on the host.\n' "$command" >&2
        exit 1
    }
done

providers=$(devpod provider list --output json)
provider_name=$(jq -r '
    to_entries
    | map(select(.value.state.initialized == true and .value.config.agent.driver == "kubernetes"))
    | if length == 1 then .[0].key
      elif (map(select(.value.default == true)) | length) == 1 then
        (map(select(.value.default == true)) | .[0].key)
      else empty end
' <<<"$providers")
if [[ -z $provider_name ]]; then
    printf 'Josh Room requires exactly one initialized Kubernetes DevPod provider.\n' >&2
    exit 1
fi

provider=$(jq -c --arg name "$provider_name" '.[$name]' <<<"$providers")
kubeconfig=$(jq -r '.state.options.KUBERNETES_CONFIG.value // empty' <<<"$provider")
context=$(jq -r '.state.options.KUBERNETES_CONTEXT.value // empty' <<<"$provider")
namespace=$(jq -r '.state.options.KUBERNETES_NAMESPACE.value // "devpod"' <<<"$provider")
kubectl_args=()
[[ -z $kubeconfig ]] || kubectl_args+=(--kubeconfig "$kubeconfig")
[[ -z $context ]] || kubectl_args+=(--context "$context")
kubectl_args+=(-n "$namespace")

config_dir="${XDG_CONFIG_HOME:-$HOME/.config}/josh-room"
config="$config_dir/config.json"
[[ -r $config ]] || {
    printf 'Run josh-room setup on the Bluefin host before devpod up.\n' >&2
    exit 1
}
r2_profile=$(jq -er '.r2.credential_profile' "$config")
age_profile=$(jq -er '.age_identity_profile' "$config")
bucket=$(jq -er '.r2.bucket' "$config")

lookup() {
    local profile=$1 field=$2
    secret-tool lookup service josh-room profile "$profile" field "$field"
}

parent_access_key=$(lookup "$r2_profile" access-key-id)
api_token=$(lookup "$r2_profile" cloudflare-api-token)
account_id=$(lookup "$r2_profile" cloudflare-account-id)
age_identity=$(lookup "$age_profile" age-identity)
[[ -n $parent_access_key && -n $api_token && -n $account_id && -n $age_identity ]] || {
    printf 'Josh Room host keyring is missing Kubernetes authority fields; rerun host setup.\n' >&2
    exit 1
}

work=$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/josh-room-k8s.XXXXXX")
chmod 700 "$work"
trap 'rm -rf -- "$work"' EXIT INT TERM
request="$work/request.json"
response="$work/response.json"
curl_config="$work/curl.conf"
manifest="$work/manifest.json"
age_file="$work/age.identity"
access_file="$work/access"
secret_file="$work/secret"
session_file="$work/session"

jq -n \
    --arg bucket "$bucket" \
    --arg parent "$parent_access_key" \
    --argjson ttl "$TTL_SECONDS" \
    '{bucket:$bucket,permission:"object-read-write",ttlSeconds:$ttl,parentAccessKeyId:$parent}' >"$request"
chmod 600 "$request"
cat >"$curl_config" <<EOF
silent
show-error
fail-with-body
request = "POST"
url = "https://api.cloudflare.com/client/v4/accounts/${account_id}/r2/temp-access-credentials"
header = "Authorization: Bearer ${api_token}"
header = "Content-Type: application/json"
data = "@${request}"
output = "${response}"
EOF
chmod 600 "$curl_config"
curl --config "$curl_config"
jq -e '.success == true and (.result.accessKeyId | length > 0) and (.result.secretAccessKey | length > 0) and (.result.sessionToken | length > 0)' "$response" >/dev/null
jq -r '.result.accessKeyId' "$response" >"$access_file"
jq -r '.result.secretAccessKey' "$response" >"$secret_file"
jq -r '.result.sessionToken' "$response" >"$session_file"
printf '%s\n' "$age_identity" >"$age_file"
chmod 600 "$access_file" "$secret_file" "$session_file" "$age_file"

expires=$(date -u -d "+${TTL_SECONDS} seconds" +%Y-%m-%dT%H:%M:%SZ)
jq -n \
    --arg namespace "$namespace" \
    --arg secret_name "$SECRET_NAME" \
    --arg role_name "$ROLE_NAME" \
    --arg expires "$expires" \
    --rawfile access "$access_file" \
    --rawfile secret "$secret_file" \
    --rawfile session "$session_file" \
    --rawfile age "$age_file" \
    '{
      apiVersion:"v1",
      kind:"List",
      items:[
        {
          apiVersion:"v1",
          kind:"Secret",
          metadata:{
            name:$secret_name,
            namespace:$namespace,
            labels:{"app.kubernetes.io/name":"josh-room","josh-room.dev/workspace":"josh-room"},
            annotations:{"josh-room.dev/expires-at":$expires}
          },
          type:"Opaque",
          stringData:{
            "access-key-id":($access|rtrimstr("\n")),
            "secret-access-key":($secret|rtrimstr("\n")),
            "session-token":($session|rtrimstr("\n")),
            "age-identity":$age
          }
        },
        {
          apiVersion:"rbac.authorization.k8s.io/v1",
          kind:"Role",
          metadata:{name:$role_name,namespace:$namespace},
          rules:[{apiGroups:[""],resources:["secrets"],resourceNames:[$secret_name],verbs:["get","delete"]}]
        },
        {
          apiVersion:"rbac.authorization.k8s.io/v1",
          kind:"RoleBinding",
          metadata:{name:$role_name,namespace:$namespace},
          subjects:[{kind:"ServiceAccount",name:"default",namespace:$namespace}],
          roleRef:{apiGroup:"rbac.authorization.k8s.io",kind:"Role",name:$role_name}
        }
      ]
    }' >"$manifest"
chmod 600 "$manifest"
kubectl "${kubectl_args[@]}" apply -f "$manifest" >/dev/null
printf 'Prepared short-lived Josh Room authority for Kubernetes workspace %s.\n' "$SECRET_NAME"

