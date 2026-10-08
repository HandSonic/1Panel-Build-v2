#!/usr/bin/env bash
# Compatibility entry point: only reviewed immutable version locks are supported.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 -c 'import sys;sys.path.insert(0,sys.argv[1]);from resolve_inputs import resolve;print(resolve(sys.argv[2])["go_version"])' "$SCRIPT_DIR" "${1:?Usage: resolve_go_version.sh VERSION}"
