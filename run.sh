#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    printf 'Error: %s was not found. Install Python 3 and try again.\n' "$PYTHON_BIN" >&2
    exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
    printf 'Creating virtual environment in %s\n' "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR" || {
        printf 'Error: Python venv support is missing. On Debian/Ubuntu run: sudo apt install python3-venv\n' >&2
        exit 1
    }
fi

printf 'Installing/updating Python dependencies...\n'
"$VENV_PYTHON" -m pip install -r requirements.txt

exec "$VENV_PYTHON" app.py