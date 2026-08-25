#!/usr/bin/env bash
set -euo pipefail

robot=${1:?robot.yaml path is required}
: "${JAT_HOLOLIB_REFERENCE:?}"
: "${JAT_HOLOLIB_ZIP_SHA256:?}"
: "${JAT_HOLOLIB_ZIP_SIZE:?}"
: "${JAT_HOLOLIB_ENVIRONMENT_HASH:?}"
: "${JAT_GIT_SHA:?}"
: "${EXPECTED_RCC_VERSION:?}"
: "${JAT_HOLOLIB_RCC_VERSION:?}"

stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
zip="$stage/hololib-d1e1c3e.zip"
receipt="$stage/hololib-d1e1c3e.json"

if oras pull "$JAT_HOLOLIB_REFERENCE" --output "$stage" >/dev/null 2>&1 \
    && [[ -f $zip && -f $receipt ]] \
    && jq -e \
        --arg environment "$JAT_HOLOLIB_ENVIRONMENT_HASH" \
        --arg jat "$JAT_GIT_SHA" \
        --arg rcc "$JAT_HOLOLIB_RCC_VERSION" \
        --arg digest "$JAT_HOLOLIB_ZIP_SHA256" \
        --argjson size "$JAT_HOLOLIB_ZIP_SIZE" \
        '.success == true and .verified_no_build == true and
         .environment_hash == $environment and .jat_git_sha == $jat and
         .rcc_version == $rcc and .platform == "linux_amd64" and
         .zip.sha256 == $digest and .zip.size == $size' "$receipt" >/dev/null \
    && [[ $(sha256sum "$zip" | cut -d' ' -f1) == "$JAT_HOLOLIB_ZIP_SHA256" ]] \
    && [[ $(stat --printf='%s' "$zip") == "$JAT_HOLOLIB_ZIP_SIZE" ]] \
    && rcc ht import "$zip" >/dev/null \
    && rcc --no-build ht vars --robot "$robot" --json >/dev/null; then
    printf 'Imported verified JAT RCC hololib artifact.\n'
    exit 0
fi

printf 'Falling back to normal RCC environment build.\n' >&2
rcc ht vars --robot "$robot" --json >/dev/null
