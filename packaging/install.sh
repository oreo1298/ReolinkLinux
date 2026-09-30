#!/usr/bin/env bash
# Install ReolinkLinux for your user on any Linux distribution (no root needed).
#
# Creates a virtualenv under $PREFIX/lib/reolinklinux, installs the app into it, puts
# `reolinklinux` and `reolinkctl` in $PREFIX/bin and adds a menu entry and icon.
# The venv sees your system's Python packages, so a PySide6 from your distribution
# is reused; otherwise PySide6-Essentials is downloaded from PyPI (~100 MB).
#
#   ./packaging/install.sh                 # PREFIX=~/.local
#   PREFIX=/opt/reolinklinux sudo -E ./packaging/install.sh
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local}"
LIB="$PREFIX/lib/reolinklinux"
VENV="$LIB/venv"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ID="io.github.oreo1298.ReolinkLinux"
PY="${PYTHON:-python3}"

say() { printf '\033[1;34m>>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

command -v "$PY" >/dev/null || { echo "python3 is required"; exit 1; }
"$PY" - <<'PYCHECK' || { echo "Python 3.10 or newer is required"; exit 1; }
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PYCHECK

missing=()
if ! ldconfig -p 2>/dev/null | grep -q 'libmpv\.so'; then
  missing+=("libmpv (the mpv library)")
fi
command -v ffmpeg >/dev/null || missing+=("ffmpeg")
if [ ${#missing[@]} -gt 0 ]; then
  warn "Missing: ${missing[*]}"
  echo "   Install them first, for example:"
  echo "     Arch:            sudo pacman -S --needed mpv ffmpeg"
  echo "     Debian/Ubuntu:   sudo apt install libmpv2 ffmpeg     (Ubuntu 22.04: libmpv1)"
  echo "     Fedora:          sudo dnf install mpv-libs ffmpeg-free"
  echo "     openSUSE:        sudo zypper install libmpv2 ffmpeg-7"
  echo "   (continuing; video needs libmpv, recording needs ffmpeg)"
fi

say "Creating virtualenv at $VENV"
mkdir -p "$LIB"
"$PY" -m venv --system-site-packages "$VENV" || {
  echo "Could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"; exit 1; }
"$VENV/bin/python" -m pip install --quiet --upgrade pip

extras=""
if "$VENV/bin/python" -c "import PySide6.QtOpenGLWidgets, PySide6.QtSvg" 2>/dev/null; then
  say "Using the PySide6 installed on this system"
else
  say "PySide6 not found on the system; installing PySide6-Essentials from PyPI"
  extras="gui"
fi
if ! "$VENV/bin/python" -c "import keyring" 2>/dev/null; then
  extras="${extras:+$extras,}keyring"
fi

say "Installing ReolinkLinux"
"$VENV/bin/python" -m pip install --quiet "$HERE${extras:+[$extras]}"

say "Installing launchers into $PREFIX/bin"
mkdir -p "$PREFIX/bin"
ln -sf "$VENV/bin/reolinklinux" "$PREFIX/bin/reolinklinux"
ln -sf "$VENV/bin/reolinkctl" "$PREFIX/bin/reolinkctl"

say "Installing the menu entry and icon"
install -Dm644 "$HERE/reolinklinux/data/reolinklinux.svg" "$PREFIX/share/icons/hicolor/scalable/apps/$APP_ID.svg"
mkdir -p "$PREFIX/share/applications"
sed "s#^Exec=reolinklinux#Exec=$PREFIX/bin/reolinklinux#" "$HERE/data/$APP_ID.desktop" \
  > "$PREFIX/share/applications/$APP_ID.desktop"
install -Dm644 "$HERE/data/$APP_ID.metainfo.xml" "$PREFIX/share/metainfo/$APP_ID.metainfo.xml"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$PREFIX/share/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$PREFIX/share/icons/hicolor" 2>/dev/null || true

echo
say "Done. Start ReolinkLinux from your application menu, or run: reolinklinux"
case ":$PATH:" in
  *":$PREFIX/bin:"*) : ;;
  *) warn "Add $PREFIX/bin to your PATH:  echo 'export PATH=\"$PREFIX/bin:\$PATH\"' >> ~/.profile" ;;
esac
