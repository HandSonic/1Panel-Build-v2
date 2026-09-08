#!/usr/bin/env bash
# One build/export/tag implementation used by both CI providers.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

case "${1:-}" in
    prepare)
        python3 scripts/release_build.py prepare
        ;;
    build)
        : "${VERSION:?Missing VERSION}" "${ARCH_LIST:?Missing ARCH_LIST}"
        : "${BUILD_FINGERPRINT:?Missing BUILD_FINGERPRINT}" "${GO_VERSION:?Missing GO_VERSION}"
        IMAGE_TAG="${IMAGE_TAG:-1panel-v2-builder:${GITHUB_SHA:-${CNB_PIPELINE_ID:-local}}}"
        docker build --file Dockerfile \
            --build-arg "VERSION=$VERSION" \
            --build-arg "TARGET_ARCHES=$ARCH_LIST" \
            --build-arg "GO_VERSION=$GO_VERSION" \
            --build-arg "NODE_VERSION=${NODE_VERSION:-20}" \
            --build-arg "INSTALLER_REF=${INSTALLER_REF:-v2}" \
            --build-arg "CHANNEL=${CHANNEL:?Missing CHANNEL}" \
            --build-arg "BUILD_FINGERPRINT=$BUILD_FINGERPRINT" \
            --tag "$IMAGE_TAG" .
        mkdir -p .ci
        printf 'export IMAGE_TAG=%q\n' "$IMAGE_TAG" > .ci/image.env
        if [[ "${CI_PROVIDER:-}" == cnb ]]; then
            echo "##[set-output IMAGE_TAG=$IMAGE_TAG]"
        elif [[ -n "${GITHUB_ENV:-}" ]]; then
            echo "IMAGE_TAG=$IMAGE_TAG" >> "$GITHUB_ENV"
        fi
        ;;
    export)
        if [[ -z "${IMAGE_TAG:-}" && -f .ci/image.env ]]; then source .ci/image.env; fi
        : "${IMAGE_TAG:?Missing IMAGE_TAG}"
        mkdir -p .ci dist
        output_dir="$(mktemp -d "$ROOT/.ci/export.XXXXXX")"
        trap 'rm -rf "$output_dir"' EXIT
        docker run --rm -v "$output_dir:/dist" "$IMAGE_TAG"
        python3 scripts/release_build.py verify --dist "$output_dir"
        # Remove earlier pipeline output only after the new output is verified.
        find dist -maxdepth 1 -type f \( -name '*.tar.gz' -o -name '*.sha256' -o -name 'build-manifest.json' \) -delete
        cp "$output_dir"/* dist/
        ;;
    ensure-tag)
        : "${VERSION:?Missing VERSION}"
        if [[ ! "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
            echo "Invalid release tag" >&2
            exit 1
        fi
        remote_tags="$(git ls-remote --tags origin "refs/tags/$VERSION")"
        if [[ -z "$remote_tags" ]]; then
            git config user.name "${CI_BOT_NAME:-Build Bot}"
            git config user.email "${CI_BOT_EMAIL:-build@users.noreply.github.com}"
            if ! git show-ref --verify --quiet "refs/tags/$VERSION"; then
                git tag -a "$VERSION" -m "Release $VERSION"
            fi
            # Another provider may create the tag concurrently. Never move it.
            if ! git push origin "refs/tags/$VERSION"; then
                remote_tags="$(git ls-remote --tags origin "refs/tags/$VERSION")"
                [[ -n "$remote_tags" ]] || exit 1
            fi
        fi
        ;;
    *)
        echo "Usage: $0 {prepare|build|export|ensure-tag}" >&2
        exit 2
        ;;
esac
