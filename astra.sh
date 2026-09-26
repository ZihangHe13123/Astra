#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT="$SCRIPT_DIR"
# Select a bootstrap interpreter, without changing the caller's workspace.
supports_python() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' >/dev/null 2>&1
}
if [[ -n "${PYTHON:-}" ]]; then
    ASTRA_PYTHON="$PYTHON"
else
    ASTRA_PYTHON=""
    for candidate in python3.11 python3 "$ROOT/.venv/bin/python"; do
        if command -v "$candidate" >/dev/null 2>&1 && supports_python "$candidate"; then
            ASTRA_PYTHON="$(command -v "$candidate")"
            break
        fi
    done
fi
if [[ -z "$ASTRA_PYTHON" ]] || ! supports_python "$ASTRA_PYTHON"; then
    printf 'Astra: Python 3.11 or newer is required (selected: %s).\n' "$ASTRA_PYTHON" >&2
    exit 1
fi
exec "$ASTRA_PYTHON" "$ROOT/astra.py" "$@"
