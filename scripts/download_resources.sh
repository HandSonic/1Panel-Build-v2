#!/usr/bin/env bash
# Discover complete installer resources instead of maintaining a language list.
set -euo pipefail
INSTALLER_REF="${INSTALLER_REF:-v2}"
INSTALLER_REPOSITORY="${INSTALLER_REPOSITORY:-1Panel-dev/installer}"
INSTALLER_GIT_URL="${INSTALLER_GIT_URL:-https://github.com/${INSTALLER_REPOSITORY}.git}"
INSTALLER_CACHE_TTL_SECONDS="${INSTALLER_CACHE_TTL_SECONDS:-3600}"
GEOIP_URLS="${GEOIP_URLS:-https://resource.fit2cloud.com/1panel/package/v2/geo/GeoIP.mmdb https://resource.1panel.pro/1panel/package/v2/geo/GeoIP.mmdb}"
resource_tmp="$(mktemp -d "${TMPDIR:-/tmp}/1panel-resources.XXXXXX")"
trap 'rm -rf "$resource_tmp"' EXIT
manifest=.installer-resource-manifest
log() { printf '[resources] %s\n' "$*" >&2; }

# Validate cache entries exactly like downloads, including MMDB's metadata marker.
cat > "$resource_tmp/check.py" <<'PY'
import pathlib, re, shlex, subprocess, sys, tarfile

def valid(path):
    p = pathlib.Path(path)
    if not p.is_file() or p.is_symlink() or not p.stat().st_size:
        return False
    with p.open('rb') as f:
        head = f.read(4096)
        if p.name == 'GeoIP.mmdb':
            f.seek(max(0, p.stat().st_size - 131072))
            return b'\xab\xcd\xefMaxMind.com' in f.read()
    if re.search(br'<(?:!doctype\s+html|html|head|body)\b', head, re.I):
        return False
    if head.lstrip().startswith((b'{', b'[')) and not p.name.endswith('.service'):
        return False
    if re.match(br'\s*(404\b|Not Found\b|Bad Gateway\b|Forbidden\b)', head, re.I):
        return False
    if p.name.endswith('.service'):
        return b'[Service]' in head and bool(re.search(br'^ExecStart\s*=', head, re.M))
    if p.name.endswith('.sh') or p.name == '1pctl':
        return subprocess.run(['bash', '-n', str(p)], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    return True

def inventory(root):
    root = pathlib.Path(root)
    files = ['1pctl', 'install.sh']
    for directory in ('initscript', 'lang'):
        files.extend(str(p.relative_to(root)) for p in sorted((root / directory).rglob('*')) if p.is_file())
    return files

def complete(root):
    root = pathlib.Path(root)
    required = ['1pctl', 'install.sh', 'initscript/1panel-core.service', 'initscript/1panel-agent.service']
    languages = list((root / 'lang').rglob('*.sh'))
    if not languages or not all(valid(root / p) for p in required):
        return False
    # Derive language requirements from the installer, not a maintained list.
    # A recognized literal menu must never advertise an absent translation.
    text = (root / 'install.sh').read_text(errors='replace')
    menu = re.search(r'\bAVAILABLE_LANGS\s*=\s*\(([^)]*)\)', text, re.S)
    required_langs = set(re.findall(r'(?:\$LANG_DIR|\$\{LANG_DIR\}|lang)/([-\w.@]+)\.sh', text))
    try:
        choices = shlex.split(menu.group(1), comments=True) if menu else []
    except ValueError:
        choices = []
    literal_menu = bool(choices) and all(re.fullmatch(r'[-\w.@]+', item) for item in choices)
    if literal_menu:
        required_langs.update(choices)
    elif not all(valid(p) for p in languages):
        # Unknown menu logic: do not guess which of its translations are unused.
        return False
    return any(valid(p) for p in languages) and all(valid(root / 'lang' / (name + '.sh')) for name in required_langs)

mode, *args = sys.argv[1:]
if mode == 'valid':
    sys.exit(0 if valid(args[0]) else 1)
elif mode == 'complete':
    sys.exit(0 if complete(args[0]) else 1)
elif mode == 'inventory':
    print('\n'.join(inventory(args[0])))
elif mode == 'extract':
    archive, dest = args
    # Copy only regular resources; never follow links or traversal paths.
    with tarfile.open(archive) as tar:
        for member in tar:
            parts = pathlib.PurePosixPath(member.name).parts
            if len(parts) < 2 or member.name.startswith('/') or '..' in parts:
                continue
            rel = pathlib.PurePosixPath(*parts[1:])
            if not (str(rel) in ('1pctl', 'install.sh') or rel.parts[0] in ('lang', 'initscript')):
                continue
            if member.isfile():
                target = pathlib.Path(dest) / str(rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read())
elif mode == 'tree':
    import json
    data = json.load(open(args[0]))
    if data.get('truncated') or not isinstance(data.get('tree'), list):
        sys.exit(1)
    for entry in data['tree']:
        path = entry.get('path', '')
        if entry.get('type') == 'blob' and not path.startswith('/') and '..' not in pathlib.PurePosixPath(path).parts and (
            path in ('1pctl', 'install.sh') or path.startswith(('lang/', 'initscript/'))
        ):
            print(path)
PY
valid_file() { python3 "$resource_tmp/check.py" valid "$1"; }
complete_resources() { python3 "$resource_tmp/check.py" complete "$1"; }

# A failure stays a failure, allowing alternate transports and validated caches.
download() {
    local output="$1" url
    shift
    for url in "$@"; do
        if curl --fail --location --silent --show-error --connect-timeout 15 \
            --max-time 180 --retry 3 --retry-delay 2 --retry-max-time 240 --retry-all-errors \
            "$url" --output "$output.part"; then
            mv -f "$output.part" "$output"
            return 0
        fi
        rm -f "$output.part"
    done
    return 1
}
cache_complete() {
    local cached_ref path
    [ -s "$manifest" ] || return 1
    IFS= read -r cached_ref < "$manifest"
    [ "$cached_ref" = "$INSTALLER_REPOSITORY@$INSTALLER_REF" ] || return 1
    # Moving refs (normally v2) eventually refresh, while temporary outages can
    # still use the validated old snapshot via the fallback below.
    python3 - "$manifest" "$INSTALLER_CACHE_TTL_SECONDS" <<'PY' || return 1
import os, sys, time
try:
    ttl = max(0, int(sys.argv[2]))
except ValueError:
    ttl = 3600
sys.exit(0 if time.time() - os.stat(sys.argv[1]).st_mtime < ttl else 1)
PY
    complete_resources . || return 1
    tail -n +2 "$manifest" > "$resource_tmp/cache.paths"
    while IFS= read -r path; do
        [ -z "$path" ] || valid_file "$path" || return 1
    done < "$resource_tmp/cache.paths"
}
cache_ref_compatible() {
    local cached_ref
    [ -s "$manifest" ] || return 0
    IFS= read -r cached_ref < "$manifest"
    [ "$cached_ref" = "$INSTALLER_REPOSITORY@$INSTALLER_REF" ]
}
fetch_snapshot() {
    local encoded_ref url path
    encoded_ref="$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$INSTALLER_REF")"
    for url in \
        "https://codeload.github.com/${INSTALLER_REPOSITORY}/tar.gz/${encoded_ref}" \
        "https://github.com/${INSTALLER_REPOSITORY}/archive/${encoded_ref}.tar.gz"; do
        rm -rf "$resource_tmp/snapshot"
        mkdir -p "$resource_tmp/snapshot"
        if download "$resource_tmp/installer.tar.gz" "$url" && \
            python3 "$resource_tmp/check.py" extract "$resource_tmp/installer.tar.gz" "$resource_tmp/snapshot" && \
            complete_resources "$resource_tmp/snapshot"; then
            return 0
        fi
        log 'Archive unavailable or incomplete; trying another installer source.'
    done
    # Fetch handles branch names, tags and commit IDs.
    if git init --quiet "$resource_tmp/git" && \
        git -C "$resource_tmp/git" -c http.lowSpeedLimit=1024 -c http.lowSpeedTime=30 \
            fetch --quiet --depth=1 "$INSTALLER_GIT_URL" "$INSTALLER_REF" && \
        git -C "$resource_tmp/git" checkout --quiet FETCH_HEAD && \
        complete_resources "$resource_tmp/git"; then
        rm -rf "$resource_tmp/snapshot"
        mv "$resource_tmp/git" "$resource_tmp/snapshot"
        return 0
    fi
    log 'Trying the installer tree and raw-file endpoints.'
    download "$resource_tmp/tree.json" \
        "https://api.github.com/repos/${INSTALLER_REPOSITORY}/git/trees/${encoded_ref}?recursive=1" || return 1
    python3 "$resource_tmp/check.py" tree "$resource_tmp/tree.json" > "$resource_tmp/tree.paths" || return 1
    rm -rf "$resource_tmp/snapshot"
    mkdir -p "$resource_tmp/snapshot"
    while IFS= read -r path; do
        mkdir -p "$resource_tmp/snapshot/$(dirname "$path")"
        local downloaded=false
        for url in \
            "https://raw.githubusercontent.com/${INSTALLER_REPOSITORY}/${encoded_ref}/$path" \
            "https://github.com/${INSTALLER_REPOSITORY}/raw/${encoded_ref}/$path"; do
            if download "$resource_tmp/snapshot/$path" "$url" && valid_file "$resource_tmp/snapshot/$path"; then
                downloaded=true
                break
            fi
            rm -f "$resource_tmp/snapshot/$path"
        done
        if [ "$downloaded" != true ]; then
            log "Could not retrieve $path; checking the remaining snapshot."
        fi
    done < "$resource_tmp/tree.paths"
    complete_resources "$resource_tmp/snapshot"
}
install_snapshot() {
    local path temporary
    python3 "$resource_tmp/check.py" inventory "$resource_tmp/snapshot" > "$resource_tmp/snapshot.paths"
    # Replace directory contents as a cohort so removed/renamed upstream files
    # cannot leave stale translations or init scripts in the next package.
    for path in lang initscript; do
        [ ! -e "$path" ] || mv "$path" "$resource_tmp/previous-$path"
    done
    while IFS= read -r path; do
        if ! valid_file "$resource_tmp/snapshot/$path"; then
            log "Skipping invalid optional installer resource: $path"
            continue
        fi
        mkdir -p "$(dirname "$path")"
        temporary="$(mktemp "$(dirname "$path")/.resource.XXXXXX")"
        cp "$resource_tmp/snapshot/$path" "$temporary"
        chmod 644 "$temporary"
        mv -f "$temporary" "$path"
    done < "$resource_tmp/snapshot.paths"
    {
        printf '%s@%s\n' "$INSTALLER_REPOSITORY" "$INSTALLER_REF"
        while IFS= read -r path; do
            if valid_file "$path"; then printf '%s\n' "$path"; fi
        done < "$resource_tmp/snapshot.paths"
    } > "$manifest.tmp"
    mv -f "$manifest.tmp" "$manifest"
}

log "Using installer ref: $INSTALLER_REF"
if cache_complete; then
    log 'Using validated installer cache.'
elif fetch_snapshot; then
    install_snapshot
elif cache_ref_compatible && complete_resources .; then
    # Legacy caches have no inventory. Retry discovery on the next invocation.
    log 'Remote snapshot unavailable; using validated local installer resources.'
else
    log 'ERROR: Need valid 1pctl, install.sh, languages and both systemd services.'
    exit 1
fi
# Other init systems are optional; new upstream files are discovered automatically.
for service in 1panel-core.service 1panel-agent.service; do
    cp "initscript/$service" ".$service.tmp"
    mv -f ".$service.tmp" "$service"
done
# The upstream installer unconditionally copies this database.
if ! valid_file GeoIP.mmdb; then
    geoip_ok=false
    read -r -a geoip_urls <<< "$GEOIP_URLS"
    for url in "${geoip_urls[@]}"; do
        if download "$resource_tmp/GeoIP.mmdb" "$url" && valid_file "$resource_tmp/GeoIP.mmdb"; then
            cp "$resource_tmp/GeoIP.mmdb" .GeoIP.mmdb.tmp
            mv -f .GeoIP.mmdb.tmp GeoIP.mmdb
            geoip_ok=true
            break
        fi
    done
    if [ "$geoip_ok" != true ]; then
        log 'ERROR: No valid GeoIP.mmdb in cache or configured sources.'
        exit 1
    fi
fi
chmod 755 1pctl install.sh
log 'Resources ready.'
