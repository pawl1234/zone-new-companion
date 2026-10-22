#!/usr/bin/env bash
# Builds "dist/Zone New Companion.app" with libmpv inside, plus a .dmg of it.
#
# Usage: scripts/build_macos.sh [--no-libmpv] [--no-dmg]
#   --no-libmpv  build without the built-in player; streams then play in VLC
#   --no-dmg     skip the disk image
#
# libmpv comes from Homebrew (brew install mpv); LIBMPV=/path/to/libmpv.dylib
# overrides it. Homebrew libraries only run on the build Mac's architecture
# and macOS version or newer, so build on the oldest macOS you want to support.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="Zone New Companion"
BUNDLE_ID="com.kidpoleon.zone-new-companion"
WITH_LIBMPV=1
WITH_DMG=1

for arg in "$@"; do
  case "$arg" in
    --no-libmpv) WITH_LIBMPV=0 ;;
    --no-dmg) WITH_DMG=0 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This script builds the macOS app; run it on a Mac." >&2
  exit 1
fi

cd "$ROOT"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' zone_new_companion/__init__.py)"

# The app needs Python 3.10+ (dataclass slots).
if [[ ! -x venv/bin/python ]]; then
  PYTHON="$(command -v python3 || true)"
  if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "Python 3.10 or newer not found. Install it with: brew install python" >&2
    exit 1
  fi
  echo "Creating venv with $PYTHON..."
  "$PYTHON" -m venv venv
fi
echo "Installing requirements..."
venv/bin/python -m pip install --quiet --upgrade pip
venv/bin/python -m pip install --quiet -r requirements.txt

PYINSTALLER_ARGS=(
  --noconfirm --clean --windowed
  --name "$APP_NAME"
  --osx-bundle-identifier "$BUNDLE_ID"
  --add-data "zone_new_companion/icon/icon.ico:zone_new_companion/icon"
  --add-data "media:media"
  --add-data "zone_new_companion/ui/theme:zone_new_companion/ui/theme"
  --hidden-import mpv
)

if [[ "$WITH_LIBMPV" == 1 ]]; then
  if [[ -z "${LIBMPV:-}" ]]; then
    if ! command -v brew >/dev/null; then
      echo "Homebrew not found. Install it from https://brew.sh, then: brew install mpv" >&2
      exit 1
    fi
    LIBMPV="$(brew --prefix)/lib/libmpv.dylib"
  fi
  if [[ ! -f "$LIBMPV" ]]; then
    echo "libmpv not found at $LIBMPV. Install it with: brew install mpv (or pass --no-libmpv)" >&2
    exit 1
  fi
  # python-mpv looks for "libmpv.dylib", so the bundled copy needs exactly that
  # name; PyInstaller collects the Homebrew libraries it depends on.
  STAGED="build/libmpv/libmpv.dylib"
  mkdir -p build/libmpv
  cp -fL "$LIBMPV" "$STAGED"
  PYINSTALLER_ARGS+=(--add-binary "$STAGED:.")
else
  echo "Warning: building without libmpv; the built-in player is off, streams play in VLC." >&2
fi

# The app icon, from the 256 px image in icon.ico.
ICONSET="build/icon/icon.iconset"
rm -rf build/icon
mkdir -p "$ICONSET"
sips -s format png zone_new_companion/icon/icon.ico --out build/icon/icon.png >/dev/null
for size in 16 32 128 256; do
  sips -z "$size" "$size" build/icon/icon.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) build/icon/icon.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o build/icon/icon.icns
PYINSTALLER_ARGS+=(--icon build/icon/icon.icns)

echo "Running PyInstaller..."
venv/bin/python -m PyInstaller "${PYINSTALLER_ARGS[@]}" main.py

APP="dist/$APP_NAME.app"
if [[ ! -d "$APP" ]]; then
  echo "PyInstaller finished but $APP is missing." >&2
  exit 1
fi
# PyInstaller writes version 0.0.0; editing Info.plist means signing again (ad hoc).
plutil -replace CFBundleShortVersionString -string "$VERSION" "$APP/Contents/Info.plist"
plutil -replace CFBundleVersion -string "$VERSION" "$APP/Contents/Info.plist"
codesign --force --sign - "$APP"
codesign --verify --deep --strict "$APP"

echo
echo "Built $APP ($(du -sh "$APP" | cut -f1))"

if [[ "$WITH_DMG" == 1 ]]; then
  DMG="dist/Zone-New-Companion-$VERSION.dmg"
  STAGE="build/dmg"
  rm -rf "$STAGE" "$DMG"
  mkdir -p "$STAGE"
  cp -R "$APP" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -quiet -volname "$APP_NAME" -srcfolder "$STAGE" -format UDZO -ov "$DMG"
  rm -rf "$STAGE"
  echo "Built $DMG ($(du -sh "$DMG" | cut -f1))"
fi
