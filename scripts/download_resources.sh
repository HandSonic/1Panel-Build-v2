#!/usr/bin/env bash
# Downloads are atomically promoted only after every pinned input is verified.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/download_resources.py" "${VERSION:?VERSION must be an explicitly locked version}" "${RESOURCE_DEST:-.}"
