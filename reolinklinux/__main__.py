"""``python -m reolinklinux`` launches the GUI (``python -m reolinklinux.cli`` for the CLI)."""

import sys

from .gui.app import main

sys.exit(main())
