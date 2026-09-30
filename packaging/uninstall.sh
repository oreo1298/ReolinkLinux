#!/usr/bin/env bash
# Remove an installation made by packaging/install.sh (settings in ~/.config/reolinklinux are kept).
set -euo pipefail
PREFIX="${PREFIX:-$HOME/.local}"
APP_ID="io.github.oreo1298.ReolinkLinux"
rm -rf "$PREFIX/lib/reolinklinux"
rm -f "$PREFIX/bin/reolinklinux" "$PREFIX/bin/reolinkctl" \
      "$PREFIX/share/applications/$APP_ID.desktop" \
      "$PREFIX/share/metainfo/$APP_ID.metainfo.xml" \
      "$PREFIX/share/icons/hicolor/scalable/apps/$APP_ID.svg"
echo "ReolinkLinux removed from $PREFIX (your settings in ~/.config/reolinklinux were kept)."
