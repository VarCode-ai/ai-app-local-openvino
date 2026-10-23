#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Start Lectern from a terminal without installing it. Uses the environment created by install.sh,
# or the system Python if it already has the dependencies (requirements.txt).
set -euo pipefail
SCRIPT_DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
for PYTHON in "${XDG_DATA_HOME:-$HOME/.local/share}/lectern/venv/bin/python" python3; do
    if command -v "$PYTHON" >/dev/null &&
       "$PYTHON" -c "import PySide6.QtMultimedia, openvino_genai, numpy, requests" 2>/dev/null; then
        exec "$PYTHON" "$SCRIPT_DIR/lectern.py" "$@"
    fi
done
echo "Python environment not ready: run $SCRIPT_DIR/install.sh first (or --dev to keep using this folder)." >&2
exit 1
