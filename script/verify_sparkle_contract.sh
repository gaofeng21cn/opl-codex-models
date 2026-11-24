#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
EXPECTED_FEED_URL="https://github.com/gaofeng21cn/opl-codex-models/releases/latest/download/appcast.xml"

grep -Fq 'https://github.com/sparkle-project/Sparkle' "$ROOT_DIR/Package.swift"
grep -Fq '.product(name: "Sparkle", package: "Sparkle")' "$ROOT_DIR/Package.swift"
grep -Fq '@executable_path/../Frameworks' "$ROOT_DIR/Package.swift"

grep -Fq 'SPUStandardUpdaterController' "$ROOT_DIR/Sources/CodexModelManager/Services/AppUpdater.swift"
grep -Fq 'checkForUpdates()' "$ROOT_DIR/Sources/CodexModelManager/App/CodexModelManagerApp.swift"
grep -Fq 'checkForUpdates()' "$ROOT_DIR/Sources/CodexModelManager/Views/SettingsView.swift"

grep -Fq "$EXPECTED_FEED_URL" "$ROOT_DIR/script/build_app.sh"
grep -Fq 'SUPublicEDKey' "$ROOT_DIR/script/build_app.sh"
grep -Fq 'Sparkle.framework' "$ROOT_DIR/script/build_app.sh"
grep -Fq 'Installer.xpc' "$ROOT_DIR/script/build_app.sh"
grep -Fq 'Downloader.xpc' "$ROOT_DIR/script/build_app.sh"

grep -Fq 'gaofeng21cn/one-person-lab/.github/workflows/reusable-macos-sparkle-release.yml@main' \
    "$ROOT_DIR/.github/workflows/release.yml"
grep -Fq 'appcast.xml' "$ROOT_DIR/.github/workflows/release.yml"

if [[ $# -ge 1 && -d "$1" ]]; then
    APP_BUNDLE="$1"
    PLIST="$APP_BUNDLE/Contents/Info.plist"
    /usr/bin/plutil -lint "$PLIST" >/dev/null
    test "$(/usr/bin/plutil -extract SUFeedURL raw "$PLIST")" = "$EXPECTED_FEED_URL"
    test "$(/usr/bin/plutil -extract SUEnableAutomaticChecks raw "$PLIST")" = "true"
    test "$(/usr/bin/plutil -extract SUAutomaticallyUpdate raw "$PLIST")" = "false"
    PUB_KEY="$(/usr/bin/plutil -extract SUPublicEDKey raw "$PLIST")"
    /usr/bin/python3 - "$PUB_KEY" <<'PY'
import base64
import sys

raw = base64.b64decode(sys.argv[1], validate=True)
if len(raw) != 32:
    raise SystemExit(f"Expected 32-byte Ed25519 key, got {len(raw)} bytes")
PY
    SPARKLE_FW="$APP_BUNDLE/Contents/Frameworks/Sparkle.framework"
    test -d "$SPARKLE_FW"
    test -f "$SPARKLE_FW/Versions/B/Sparkle"
    for comp in \
        "$SPARKLE_FW/Versions/B/XPCServices/Installer.xpc" \
        "$SPARKLE_FW/Versions/B/XPCServices/Downloader.xpc" \
        "$SPARKLE_FW/Versions/B/Autoupdate" \
        "$SPARKLE_FW/Versions/B/Updater.app" \
        "$SPARKLE_FW"
    do
        /usr/bin/codesign --verify --strict "$comp"
    done
    /usr/bin/codesign --verify --deep --strict "$APP_BUNDLE"
fi

echo "Verified Codex Models Sparkle 2 dual-track updater contract."
