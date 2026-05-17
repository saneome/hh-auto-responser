#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="${VENV:-.venv}"
PYTHON="$VENV/bin/python3"
PI="$VENV/bin/pyinstaller"

if [ ! -x "$PYTHON" ]; then
    echo "venv not found at $VENV — run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

if ! "$PYTHON" -c "import PySide6" 2>/dev/null; then
    echo "PySide6 not installed — run: pip install -r requirements.txt"
    exit 1
fi

if ! command -v "$PI" &>/dev/null; then
    echo "pyinstaller not found — installing..."
    "$VENV/bin/pip" install pyinstaller
fi

echo "=== building hh-auto-response ==="
echo "platform: $(uname -s) $(uname -m)"

"$PI" \
    --clean \
    --noconfirm \
    hh-auto-response.spec

echo ""
echo "=== done ==="
echo "output: dist/hh-auto-response/"

if [ "$(uname -s)" = "Linux" ]; then
    echo ""
    echo "Create desktop entry:"
    echo "  cat > ~/.local/share/applications/hh-auto-response.desktop << 'DESK'"
    echo "  [Desktop Entry]"
    echo "  Name=HH Auto Response"
    echo "  Exec=$SCRIPT_DIR/dist/hh-auto-response/hh-auto-response"
    echo "  Type=Application"
    echo "  Categories=Network;Qt;"
    echo "  DESK"
fi
