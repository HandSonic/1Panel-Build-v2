#!/usr/bin/env bash
# An unsupported architecture must not discard healthy packages for other systems.
set -euo pipefail

ROOT="${SOURCE_ROOT:-/opt/1Panel}"
DIST="${DIST_DIR:-${ROOT}/dist}"
VERSION="${VERSION:?VERSION is required}"
TARGET_ARCHES="${TARGET_ARCHES:-amd64 arm64 armv7 ppc64le s390x loong64 riscv64}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

build_arch() {
    local arch="$1" goarch="$1" goarm="" package="1panel-${VERSION}-linux-$1"
    [[ "$arch" != armv7 ]] || { goarch=arm; goarm=7; }
    local work="${ROOT}/build/${arch}"
    mkdir -p "${work}/${package}"
    (
        cd "${ROOT}/core"
        CGO_ENABLED=0 GOOS=linux GOARCH="$goarch" GOARM="$goarm" \
            go build -trimpath -ldflags '-s -w' -o "${work}/${package}/1panel-core" ./cmd/server
    )
    (
        cd "${ROOT}/agent"
        CGO_ENABLED=0 GOOS=linux GOARCH="$goarch" GOARM="$goarm" \
            go build -trimpath -ldflags '-s -w' -o "${work}/${package}/1panel-agent" ./cmd/server
    )
    for file in 1pctl install.sh GeoIP.mmdb; do
        test -s "${ROOT}/${file}"
        cp "${ROOT}/${file}" "${work}/${package}/"
    done
    for file in 1panel-core.service 1panel-agent.service LICENSE README.md; do
        if [[ -s "${ROOT}/${file}" ]]; then cp "${ROOT}/${file}" "${work}/${package}/"; fi
    done
    cp -R "${ROOT}/initscript" "${ROOT}/lang" "${work}/${package}/"
    tar -czf "${work}/${package}.tar.gz" -C "$work" "$package"
    mv "${work}/${package}.tar.gz" "${DIST}/${package}.tar.gz"
    (cd "$DIST" && sha256sum "${package}.tar.gz" > "${package}.tar.gz.sha256")
    rm -rf "$work"
}

if [[ "${1:-}" == --arch ]]; then
    # Run in a separate shell so errexit remains active when parent checks status.
    build_arch "$2"
    exit 0
fi

[[ "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || { echo 'Invalid VERSION' >&2; exit 1; }
mkdir -p "$DIST" "${ROOT}/build"
status_file="$(mktemp "${ROOT}/build/status.XXXXXX")"
trap 'rm -f "$status_file"' EXIT
read -r -a arches <<< "${TARGET_ARCHES//,/ }"
declare -A seen=()
built=0
for arch in "${arches[@]}"; do
    [[ "$arch" =~ ^[A-Za-z0-9_]+$ ]] || { echo "Invalid architecture: $arch" >&2; exit 1; }
    [[ -z "${seen[$arch]:-}" ]] || continue
    seen[$arch]=1
    echo "==> building ${arch}"
    # Clean only this attempt's outputs; CI merges verified previous assets separately.
    rm -f "${DIST}/1panel-${VERSION}-linux-${arch}.tar.gz" "${DIST}/1panel-${VERSION}-linux-${arch}.tar.gz.sha256"
    if SOURCE_ROOT="$ROOT" DIST_DIR="$DIST" bash "$0" --arch "$arch"; then
        printf '%s\tbuilt\n' "$arch" >> "$status_file"
        built=$((built + 1))
    else
        echo "[WARN] ${arch} build failed; publishing other usable architectures and retrying next run" >&2
        printf '%s\tskipped\n' "$arch" >> "$status_file"
        rm -f "${DIST}/1panel-${VERSION}-linux-${arch}.tar.gz" "${DIST}/1panel-${VERSION}-linux-${arch}.tar.gz.sha256"
        rm -rf "${ROOT}/build/${arch}"
    fi
done

python3 - "$status_file" "$DIST" "$VERSION" "$SCRIPT_DIR" <<'PY'
import hashlib, json, os, sys
from pathlib import Path
status_file, dist, version, scripts = sys.argv[1:]
sys.path.insert(0, scripts)
from configure_runtime import resolve_channel
dist = Path(dist)
packages = []
for line in Path(status_file).read_text().splitlines():
    arch, status = line.split('\t')
    entry = {"arch": arch, "status": status}
    if status == "built":
        filename = f"1panel-{version}-linux-{arch}.tar.gz"
        hasher = hashlib.sha256()
        with (dist / filename).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                hasher.update(chunk)
        entry.update(file=filename, sha256=hasher.hexdigest(), size=(dist / filename).stat().st_size)
    else:
        entry['reason'] = 'Compilation or packaging failed; see architecture build log'
    packages.append(entry)
manifest = {
    "schema": 1, "version": version,
    "channel": resolve_channel(version, os.environ.get('CHANNEL', '')),
    "fingerprint": os.environ.get('BUILD_FINGERPRINT', ''),
    "requested_arches": [entry['arch'] for entry in packages],
    "packages": packages,
    "complete": bool(packages) and all(entry['status'] == 'built' for entry in packages),
}
(dist / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY
if [[ "$built" == 0 ]]; then
    echo 'ERROR: no usable architecture was built' >&2
    exit 1
fi
echo "Built ${built} architecture(s); see dist/build-manifest.json for results."
