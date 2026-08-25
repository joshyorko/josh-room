#!/usr/bin/env bash
set -euo pipefail

robot=${1:?robot.yaml path is required}
: "${JAT_ENVIRONMENT_ARTIFACT_REFERENCE:?}"
: "${JAT_ENVIRONMENT_ARTIFACT_MANIFEST_DIGEST:?}"
: "${JAT_ENVIRONMENT_ARTIFACT_ARCHIVE_SHA256:?}"
: "${JAT_ENVIRONMENT_ARTIFACT_ARCHIVE_SIZE:?}"
: "${JAT_ENVIRONMENT_ARTIFACT_DIGEST:?}"
: "${JAT_ENVIRONMENT_ARTIFACT_SPECIFICATION_DIGEST:?}"
: "${JAT_ENVIRONMENT_ARTIFACT_LEGACY_BLUEPRINT_KEY:?}"
: "${JAT_ENVIRONMENT_ARTIFACT_RCC_VERSION:?}"
: "${JAT_ENVIRONMENT_ARTIFACT_PLATFORM:?}"
: "${JAT_GIT_SHA:?}"

stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
archive="$stage/jat-runtime.rcca"
receipt="$stage/jat-runtime.json"
if [[ "$JAT_ENVIRONMENT_ARTIFACT_REFERENCE" == *@"$JAT_ENVIRONMENT_ARTIFACT_MANIFEST_DIGEST" ]] \
    && oras pull "$JAT_ENVIRONMENT_ARTIFACT_REFERENCE" --output "$stage" >/dev/null 2>&1 \
    && [[ -f $archive && -f $receipt ]] \
    && jq -e --arg jat "$JAT_GIT_SHA" --arg rcc "$JAT_ENVIRONMENT_ARTIFACT_RCC_VERSION" \
      --arg platform "$JAT_ENVIRONMENT_ARTIFACT_PLATFORM" --arg manifest "$JAT_ENVIRONMENT_ARTIFACT_MANIFEST_DIGEST" \
      --arg artifact "$JAT_ENVIRONMENT_ARTIFACT_DIGEST" --arg spec "$JAT_ENVIRONMENT_ARTIFACT_SPECIFICATION_DIGEST" \
      --arg legacy "$JAT_ENVIRONMENT_ARTIFACT_LEGACY_BLUEPRINT_KEY" --argjson size "$JAT_ENVIRONMENT_ARTIFACT_ARCHIVE_SIZE" \
      '.success == true and .jat_git_sha == $jat and .rcc_version == $rcc and .platform == $platform and
       .artifact_digest == $artifact and .specification_digest == $spec and .legacy_blueprint_key == $legacy and
       .archive.sha256 == "'"$JAT_ENVIRONMENT_ARTIFACT_ARCHIVE_SHA256"'" and .archive.size == $size' "$receipt" >/dev/null \
    && [[ $(sha256sum "$archive" | cut -d' ' -f1) == "$JAT_ENVIRONMENT_ARTIFACT_ARCHIVE_SHA256" ]] \
    && [[ $(stat --printf='%s' "$archive") == "$JAT_ENVIRONMENT_ARTIFACT_ARCHIVE_SIZE" ]] \
    && acquired=$(rcc env acquire --archive "$archive" --permissive-local --json) \
    && jq -e --arg digest "$JAT_ENVIRONMENT_ARTIFACT_DIGEST" '.artifactDigest == $digest and .verification.valid == true' <<<"$acquired" >/dev/null \
    && rcc --no-build ht vars --robot "$robot" --json >/dev/null; then
    printf 'Acquired verified JAT Environment Artifact.\n'
    exit 0
fi
printf 'Falling back to normal RCC environment build.\n' >&2
rcc ht vars --robot "$robot" --json >/dev/null
