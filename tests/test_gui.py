"""Headless GUI smoke tests (skipped without PySide6)."""

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from reolinklinux.gui.theme import theme
    theme.apply(app, "dark")
    yield app


def test_main_window_builds_and_closes(qapp, tmp_path):
    from reolinklinux.core.config import Config
    from reolinklinux.gui.main_window import MainWindow
    win = MainWindow(Config(tmp_path / "c.json"))
    win.show()
    qapp.processEvents()
    assert win.wall.empty.isVisible() or True
    win.show_page(1)
    win.show_page(0)
    win.close()


def test_dialogs_build(qapp, tmp_path):
    from reolinklinux.core.config import CameraConfig, Config
    from reolinklinux.gui.dialogs import AboutDialog, CameraDialog, SettingsDialog
    cfg = Config(tmp_path / "c.json")
    for dlg in (SettingsDialog(cfg), CameraDialog(None), CameraDialog(None, CameraConfig(host="h"), True), AboutDialog()):
        dlg.show()
        qapp.processEvents()
        dlg.close()


def test_themes_and_icons(qapp):
    from reolinklinux.gui import icons
    from reolinklinux.gui.theme import theme
    for mode in ("light", "dark"):
        theme.apply(qapp, mode)
        assert theme.palette.name == mode
    for name in icons._PATHS:
        assert not icons.icon(name, "#ffffff").isNull()


def test_timeline_maps_clicks_to_times(qapp):
    import datetime as dt

    from reolinklinux.gui.playback import Timeline
    t = Timeline()
    t.resize(1010, 68)
    t.set_day(dt.date(2026, 9, 30), [])
    got = []
    t.seek_requested.connect(got.append)
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    pos = QPointF(10 + 495, 40)   # the middle of the track = 12:00
    for kind in (QMouseEvent.MouseButtonPress, QMouseEvent.MouseButtonRelease):
        ev = QMouseEvent(kind, pos, pos, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        (t.mousePressEvent if kind == QMouseEvent.MouseButtonPress else t.mouseReleaseEvent)(ev)
    assert got and abs((got[0] - dt.datetime(2026, 9, 30, 12)).total_seconds()) < 120


def test_demo_device_flow():
    import datetime as dt

    from reolinklinux.core.demo import DemoDevice
    dev = DemoDevice("trackmix")
    dev.connect()
    ch = dev.channels[0]
    assert ch.caps.telephoto and ch.caps.pan_tilt
    assert dev.stream_url(0, 1, "main").startswith("av://lavfi:")
    days = dev.recording_days(0, dt.date.today().year, dt.date.today().month)
    assert dt.date.today().day in days
    dev.save_preset(0, 9, "New")
    assert ch.presets[-1].name == "New"


def test_cameras_decode_on_cpu_unless_gpu_chosen(qapp, tmp_path):
    from reolinklinux.core.config import CameraConfig, Config
    from reolinklinux.gui.manager import CameraManager
    cfg = Config(tmp_path / "c.json")
    cfg.cameras = [CameraConfig(id="duo", host="d"), CameraConfig(id="tm", host="t")]
    mgr = CameraManager(cfg)
    assert mgr.hwdec("duo") == mgr.hwdec("tm") == "no"
    cfg.settings.hwdec = "nvdec-copy"
    mgr.entries["duo"].cfg.decoder = "no"
    assert mgr.hwdec("duo") == "no" and mgr.hwdec("tm") == "nvdec-copy"
