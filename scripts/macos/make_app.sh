#!/usr/bin/env bash
# Build "Job Search.app", the Dock app, and put it in ~/Applications (macOS only).
#
#   bash scripts/macos/make_app.sh             # build, replacing an older copy
#   bash scripts/macos/make_app.sh --out DIR   # put the app in DIR instead
#
# The app opens this repository's UI in its own window and runs scripts/start.sh when no
# backend is running. It is tied to this folder: rebuild it after moving the folder. It runs
# with the PATH of the shell that built it, so conda, npm, Chrome and the model CLIs are found
# as they are in your terminal. Needs the Xcode command line tools (swiftc).
# Env overrides: BACKEND_PORT (8000).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
OUT_DIR="$HOME/Applications"
APP_NAME="Job Search"
BUNDLE_ID="local.jobsearch.app"
PORT="${BACKEND_PORT:-8000}"

log() { printf '\033[1;34m[app]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[app]\033[0m %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT_DIR="$2"; shift 2 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ "$(uname)" == Darwin ]] || { warn "The Dock app is for macOS. Use scripts/start.sh."; exit 1; }
command -v swiftc >/dev/null || { warn "swiftc not found. Run: xcode-select --install"; exit 1; }

BUILD="$(mktemp -d)"
trap 'rm -r "$BUILD"' EXIT
APP="$BUILD/$APP_NAME.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$BUILD/AppIcon.iconset"

log "Compiling"
swiftc -O -swift-version 5 "$HERE/main.swift" -o "$APP/Contents/MacOS/JobSearch"
swiftc -O "$HERE/make_icon.swift" -o "$BUILD/make_icon"
"$BUILD/make_icon" "$BUILD/icon.png"
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" "$BUILD/icon.png" --out "$BUILD/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$BUILD/icon.png" \
    --out "$BUILD/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$BUILD/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"

xml() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' <<<"$1"; }
# The Dock starts apps with a bare PATH; keep the builder's, plus the usual install places.
LAUNCH_PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cat >"$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleExecutable</key><string>JobSearch</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>JobSearchRoot</key><string>$(xml "$ROOT")</string>
  <key>JobSearchPath</key><string>$(xml "$LAUNCH_PATH")</string>
  <key>JobSearchPort</key><integer>$PORT</integer>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist" >/dev/null
codesign --force --sign - "$APP" >/dev/null 2>&1 || warn "Could not sign the app locally"

TARGET="$OUT_DIR/$APP_NAME.app"
mkdir -p "$OUT_DIR"
if [[ -e "$TARGET" ]]; then
  existing="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$TARGET/Contents/Info.plist" \
    2>/dev/null || true)"
  [[ "$existing" == "$BUNDLE_ID" ]] || { warn "$TARGET exists and is not this app; left alone"; exit 1; }
  rm -r "$TARGET"
fi
mv "$APP" "$TARGET"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$TARGET" >/dev/null 2>&1 || true

log "Installed $TARGET"
if pgrep -xq JobSearch; then log "Quit Job Search and open it again to use this build."; fi
log "Open it, then right-click its Dock icon → Options → Keep in Dock."
