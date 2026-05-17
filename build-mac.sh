#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="${VENV:-.venv}"
PYTHON="$VENV/bin/python3"
PI="$VENV/bin/pyinstaller"

if [ ! -x "$PYTHON" ]; then
    echo "venv not found at $VENV"
    echo "run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
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

echo "=== building hh-auto-response (macOS) ==="

"$PI" \
    --clean \
    --noconfirm \
    hh-auto-response.spec

APP_DIR="dist/hh-auto-response.app"
BUNDLE_DIR="dist/hh-auto-response"

if [ -d "$BUNDLE_DIR" ]; then
    echo ""
    echo "=== creating .app bundle ==="
    mkdir -p "$APP_DIR/Contents/MacOS"
    mkdir -p "$APP_DIR/Contents/Resources"
    mkdir -p "$APP_DIR/Contents/Frameworks"

    cp icon.png "$APP_DIR/Contents/Resources/icon.png"

    cat > "$APP_DIR/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>HH Auto Response</string>
    <key>CFBundleDisplayName</key>
    <string>HH Auto Response</string>
    <key>CFBundleIdentifier</key>
    <string>com.hh-auto-response.app</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleExecutable</key>
    <string>hh-auto-response</string>
    <key>CFBundleIconFile</key>
    <string>icon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>NSSupportsAutomaticTermination</key>
    <true/>
    <key>NSSupportsSuddenTermination</key>
    <true/>
</dict>
</plist>
PLIST

    cp -r "$BUNDLE_DIR/"* "$APP_DIR/Contents/MacOS/" 2>/dev/null || true
    mv "$APP_DIR/Contents/MacOS/_internal" "$APP_DIR/Contents/Frameworks/" 2>/dev/null || true

    cat > "$APP_DIR/Contents/MacOS/hh-auto-response" << LAUNCH
#!/usr/bin/env bash
DIR="\$(cd "\$(dirname "\$0")" && pwd)"
exec "\$DIR/hh-auto-response-bin" "\$@"
LAUNCH
    mv "$APP_DIR/Contents/MacOS/hh-auto-response" "$APP_DIR/Contents/MacOS/hh-auto-response-bin" 2>/dev/null || true
    chmod +x "$APP_DIR/Contents/MacOS/hh-auto-response"

    echo "macOS .app bundle created: $APP_DIR"
fi

echo ""
echo "=== done ==="
