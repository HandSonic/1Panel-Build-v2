#!/usr/bin/env bash

set -euo pipefail

VERSION="${1:-}"
UPSTREAM_REPO="${UPSTREAM_REPO:-1Panel-dev/1Panel}"
DEFAULT_GO_VERSION="${DEFAULT_GO_VERSION:-1.25.7}"

if [[ -z "${VERSION}" ]]; then
    echo "Usage: $0 <version-or-branch>" >&2
    exit 1
fi

fetch_go_mod() {
    local module_dir="$1"
    local url="" relative_path="go.mod"
    if [[ "${module_dir}" == "." ]]; then
        url="https://raw.githubusercontent.com/${UPSTREAM_REPO}/${VERSION}/go.mod"
    else
        url="https://raw.githubusercontent.com/${UPSTREAM_REPO}/${VERSION}/${module_dir}/go.mod"
        relative_path="${module_dir}/go.mod"
    fi
    curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 --max-time 60 "${url}" 2>/dev/null \
        || curl -fsSL --retry 2 --connect-timeout 15 --max-time 60 \
            "https://github.com/${UPSTREAM_REPO}/raw/${VERSION}/${relative_path}" 2>/dev/null \
        || return 1
}

collect_versions_from_content() {
    local content="$1"
    while IFS= read -r version; do
        if [[ -n "${version}" ]]; then
            versions+=("${version}")
        fi
    done < <(printf '%s\n' "${content}" | extract_versions)
}

extract_versions() {
    awk '
        $1 == "toolchain" && $2 ~ /^go[0-9]+\.[0-9]+(\.[0-9]+)?$/ {
            sub(/^go/, "", $2)
            print $2
        }
        $1 == "go" && $2 ~ /^[0-9]+\.[0-9]+(\.[0-9]+)?$/ {
            print $2
        }
    '
}

declare -a versions=()

for module_dir in . core agent; do
    if content="$(fetch_go_mod "${module_dir}")"; then
        collect_versions_from_content "${content}"
    fi
done

if [[ ${#versions[@]} -eq 0 ]] && command -v git >/dev/null 2>&1; then
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "${tmp_dir}"' EXIT

    if git clone --depth=1 --branch "${VERSION}" "https://github.com/${UPSTREAM_REPO}.git" "${tmp_dir}/repo" >/dev/null 2>&1; then
        for module_file in \
            "${tmp_dir}/repo/go.mod" \
            "${tmp_dir}/repo/core/go.mod" \
            "${tmp_dir}/repo/agent/go.mod"; do
            if [[ -f "${module_file}" ]]; then
                collect_versions_from_content "$(cat "${module_file}")"
            fi
        done

        if [[ ${#versions[@]} -eq 0 ]]; then
            while IFS= read -r module_file; do
                collect_versions_from_content "$(cat "${module_file}")"
            done < <(find "${tmp_dir}/repo" -maxdepth 3 -type f -name go.mod ! -path '*/vendor/*' | sort)
        fi
    fi
fi

if [[ ${#versions[@]} -eq 0 ]]; then
    echo "[WARN] Cannot resolve upstream Go requirement; using ${DEFAULT_GO_VERSION} with GOTOOLCHAIN=auto" >&2
    echo "${DEFAULT_GO_VERSION}"
    exit 0
fi

printf '%s\n' "${versions[@]}" | sort -V | tail -n 1
