"""GUI entry point."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .. import APP_ID, __app_name__, __version__

ICON_PATH = Path(__file__).resolve().parent.parent / "data" / "reolinklinux.svg"


def _prepare_environment() -> None:
    # EGL on X11 lets mpv use zero-copy VA-API decoding; Wayland uses EGL anyway.
    if os.environ.get("XDG_SESSION_TYPE") != "wayland" and "QT_XCB_GL_INTEGRATION" not in os.environ:
        os.environ["QT_XCB_GL_INTEGRATION"] = "xcb_egl"


def _app_icon():
    from PySide6.QtGui import QIcon

    themed = QIcon.fromTheme(APP_ID)
    if not themed.isNull():
        return themed
    if ICON_PATH.exists():
        return QIcon(str(ICON_PATH))
    return QIcon()


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reolinklinux", description=f"{__app_name__}: Reolink camera client")
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    parser.add_argument("--demo", action="store_true", help="add the simulated demo cameras")
    parser.add_argument("--theme", choices=("system", "dark", "light"), help="override the colour theme")
    parser.add_argument("--config", help="use another configuration file")
    args, qt_args = parser.parse_known_args(argv)

    _prepare_environment()
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QGuiApplication, QSurfaceFormat
    from PySide6.QtWidgets import QApplication

    from ..core import demo
    from ..core.config import Config
    from . import demo_scenes
    from .main_window import MainWindow
    from .theme import theme

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    fmt = QSurfaceFormat.defaultFormat()
    fmt.setSwapInterval(1)
    QSurfaceFormat.setDefaultFormat(fmt)
    QApplication.setApplicationName("reolinklinux")
    QApplication.setApplicationDisplayName(__app_name__)
    QApplication.setApplicationVersion(__version__)
    QApplication.setOrganizationName("reolinklinux")
    QGuiApplication.setDesktopFileName(APP_ID)

    app = QApplication.instance() or QApplication([sys.argv[0], *qt_args])
    icon = _app_icon()
    app.setWindowIcon(icon)

    config = Config(Path(args.config)) if args.config else Config()
    mode = args.theme or os.environ.get("REOLINKLINUX_THEME") or config.settings.theme
    theme.apply(app, mode)
    hints = QGuiApplication.styleHints()
    if hasattr(hints, "colorSchemeChanged"):
        hints.colorSchemeChanged.connect(lambda _s: theme.apply(app) if theme.mode == "system" else None)

    demo.scene_provider = demo_scenes.scene_path
    win = MainWindow(config, icon)
    if args.demo:
        win.add_demo_cameras()
    win.show()
    return app.exec()


def main() -> int:
    return run(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
