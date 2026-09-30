#!/usr/bin/env bash
# Run ReolinkLinux straight from this folder, without installing it.
# Needs Python 3.10+, PySide6 and libmpv (see the README for your distribution).
#   ./reolinklinux.sh            # the app
#   ./reolinklinux.sh --demo     # with simulated cameras
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$here${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m reolinklinux "$@"
