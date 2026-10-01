"""Dialogs: add/edit camera (with network discovery), settings, about."""

from __future__ import annotations

import platform

from PySide6 import __version__ as pyside_version
from PySide6.QtCore import QSize, Qt, QUrl, qVersion
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__, __version__
from ..core import discovery, recorder
from ..core.config import CameraConfig, Config, keyring
from ..core.device import Device
from ..core.errors import AuthError
from . import mpv, worker
from .widgets import bind_icon, scaled_font

ENABLE_PORTS_HELP = ("The camera must have its HTTP or HTTPS port and its RTSP port turned on. In the Reolink app "
                     "open the camera's Settings → Network → Advanced → Server settings (called Port settings on "
                     "some models) and switch on HTTPS (or HTTP) and RTSP.")


def _muted(text: str, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setObjectName("Muted")
    label.setWordWrap(wrap)
    return label


class CameraDialog(QDialog):
    """Add or edit a camera. ``result_config()`` / ``result_password()`` hold the answer."""

    def __init__(self, parent=None, cfg: CameraConfig | None = None, has_password: bool = False):
        super().__init__(parent)
        self.editing = cfg is not None
        self.cfg = cfg or CameraConfig()
        self.setWindowTitle("Edit camera" if self.editing else "Add camera")
        self.setMinimumWidth(560)
        self._testing = False

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        title = QLabel("Edit camera" if self.editing else "Add a Reolink camera, NVR or Home Hub")
        title.setFont(scaled_font(title, 1.2, bold=True))
        lay.addWidget(title)

        # discovery
        if not self.editing:
            search_row = QHBoxLayout()
            self.search_btn = QPushButton("Search the network")
            self.search_btn._binder = bind_icon(self.search_btn, "scan")
            self.search_btn.clicked.connect(self._search)
            search_row.addWidget(self.search_btn)
            self.search_state = _muted("Finds cameras on this network (ONVIF and Reolink ports).", False)
            search_row.addWidget(self.search_state, 1)
            lay.addLayout(search_row)
            self.found = QListWidget()
            self.found.setMaximumHeight(130)
            self.found.hide()
            self.found.itemClicked.connect(self._pick_found)
            lay.addWidget(self.found)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
        self.host = QLineEdit(self.cfg.host)
        self.host.setPlaceholderText("192.168.1.50 or camera.local")
        self.name = QLineEdit(self.cfg.name)
        self.name.setPlaceholderText("Taken from the camera if left empty")
        self.user = QLineEdit(self.cfg.username or "admin")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        if self.editing and has_password:
            self.password.setPlaceholderText("Unchanged")
        show = QPushButton()
        show.setObjectName("Flat")
        show.setCheckable(True)
        show.setToolTip("Show password")
        show._binder = bind_icon(show, "eye", "text_muted")
        show.setFixedWidth(36)
        show.toggled.connect(lambda on: self.password.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password))
        pw_row = QHBoxLayout()
        pw_row.addWidget(self.password, 1)
        pw_row.addWidget(show)
        form.addRow("Address", self.host)
        form.addRow("Name", self.name)
        form.addRow("User name", self.user)
        form.addRow("Password", pw_row)
        self.scheme = QComboBox()
        self.scheme.addItem("Automatic (HTTPS, then HTTP)", None)
        self.scheme.addItem("HTTPS", True)
        self.scheme.addItem("HTTP", False)
        self.scheme.setCurrentIndex(max(0, self.scheme.findData(self.cfg.https)))
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("Default (443 / 80)")
        self.port.setValue(self.cfg.port or 0)
        form.addRow("Protocol", self.scheme)
        form.addRow("Web port", self.port)
        lay.addLayout(form)

        self.continuous = QCheckBox("Record continuously to this PC while ReolinkLinux is running")
        self.continuous.setChecked(self.cfg.continuous_record)
        lay.addWidget(self.continuous)
        self.software = QCheckBox("Software decoding (use if the picture shows coloured dots or lines)")
        self.software.setChecked(self.cfg.software_decode)
        lay.addWidget(self.software)

        help_box = QLabel(ENABLE_PORTS_HELP)
        help_box.setObjectName("Banner")
        help_box.setWordWrap(True)
        lay.addWidget(help_box)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.hide()
        lay.addWidget(self.result)

        buttons = QDialogButtonBox()
        if not self.editing:
            demo = buttons.addButton("Try demo cameras", QDialogButtonBox.HelpRole)
            demo.setProperty("variant", "ghost")
            demo.setToolTip("Add simulated cameras to explore the app without hardware")
            demo.clicked.connect(self._demo)
        self.test_btn = buttons.addButton("Test connection", QDialogButtonBox.ActionRole)
        self.test_btn.clicked.connect(self._test)
        self.ok_btn = buttons.addButton("Save" if self.editing else "Add camera", QDialogButtonBox.AcceptRole)
        self.ok_btn.setProperty("variant", "primary")
        buttons.addButton(QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.host.setFocus()

    # -- discovery
    def _search(self) -> None:
        self.search_btn.setEnabled(False)
        self.search_state.setText("Searching… (a few seconds)")
        worker.run(lambda: discovery.discover(), self._found, self._search_failed)

    def _found(self, items: list) -> None:
        self.search_btn.setEnabled(True)
        self.found.clear()
        if not items:
            self.search_state.setText("Nothing found. Enter the address by hand (see your router's device list).")
            self.found.hide()
            return
        self.search_state.setText(f"Found {len(items)} device{'s' if len(items) != 1 else ''}; click one to use it.")
        for it in items:
            note = "" if it.api else "  (HTTP/HTTPS API is off)"
            item = QListWidgetItem(f"{it.host}   {it.description}{note}")
            item.setData(Qt.UserRole, it)
            self.found.addItem(item)
        self.found.show()

    def _search_failed(self, exc: Exception) -> None:
        self.search_btn.setEnabled(True)
        self.search_state.setText(f"Search failed: {exc}")

    def _pick_found(self, item: QListWidgetItem) -> None:
        it = item.data(Qt.UserRole)
        self.host.setText(it.host)
        if it.name and not self.name.text():
            self.name.setText(it.name)
        if it.https is not None:
            self.scheme.setCurrentIndex(self.scheme.findData(it.https))
        self.password.setFocus()

    # -- test / accept
    def _values(self) -> tuple[CameraConfig, str | None]:
        cfg = CameraConfig(**{**self.cfg.__dict__})
        cfg.host = self.host.text().strip()
        cfg.name = self.name.text().strip()
        cfg.username = self.user.text().strip() or "admin"
        cfg.https = self.scheme.currentData()
        cfg.port = self.port.value() or None
        cfg.continuous_record = self.continuous.isChecked()
        cfg.software_decode = self.software.isChecked()
        password = self.password.text()
        if self.editing and not password and self.password.placeholderText() == "Unchanged":
            return cfg, None
        return cfg, password

    def _show_result(self, text: str, ok: bool) -> None:
        self.result.setText(text)
        self.result.setStyleSheet("color: #3ddc97;" if ok else "color: #ff6b6b;")
        self.result.show()

    def _test(self) -> None:
        cfg, password = self._values()
        if not cfg.host:
            self._show_result("Enter the camera's address first.", False)
            return
        if password is None:
            parent = self.parent()
            password = parent.config.password(self.cfg) if parent is not None and hasattr(parent, "config") else ""
        self.test_btn.setEnabled(False)
        self._show_result("Connecting…", True)
        dev = Device(cfg.host, cfg.username, password, cfg.port, cfg.https)

        def work():
            dev.connect(probe_streams=False)
            ch = dev.channels[0] if dev.channels else None
            if ch:
                dev.probe_stream(ch.index, 0, "main")
            dev.disconnect()
            return dev

        worker.run(work, self._tested, self._test_failed)

    def _tested(self, dev: Device) -> None:
        self.test_btn.setEnabled(True)
        kind = "NVR" if dev.is_nvr else "camera"
        chans = f", {len(dev.channels)} channels" if dev.is_nvr else ""
        ch = dev.channels[0] if dev.channels else None
        extra = ""
        if ch:
            feats = [n for f, n in ((ch.caps.pan_tilt, "PTZ"), (ch.caps.optical_zoom, "zoom"),
                                    (ch.caps.telephoto, "dual lens"), (ch.caps.spotlight, "spotlight"))
                     if f]
            extra = f" · {ch.main.resolution}" if ch.main.resolution else ""
            extra += f" · {', '.join(feats)}" if feats else ""
        rtsp = "" if dev.ports.get("rtsp_enabled", True) else "  RTSP is switched off on the camera!"
        self._show_result(f"Connected: {dev.info.model or kind} “{dev.info.name}”{chans}{extra}.{rtsp}", not rtsp)
        if not self.name.text() and dev.info.name:
            self.name.setPlaceholderText(dev.info.name)

    def _test_failed(self, exc: Exception) -> None:
        self.test_btn.setEnabled(True)
        hint = "" if isinstance(exc, AuthError) else "\n" + ENABLE_PORTS_HELP
        self._show_result(f"{exc}{hint}", False)

    def _demo(self) -> None:
        self.wants_demo = True
        self.accept()

    def _accept(self) -> None:
        cfg, password = self._values()
        if not cfg.host:
            self._show_result("Enter the camera's address.", False)
            return
        self.cfg_result, self.password_result = cfg, password
        self.accept()


class SettingsDialog(QDialog):
    def __init__(self, config: Config, parent=None):
        super().__init__(parent)
        self.config = config
        s = config.settings
        self.setWindowTitle("Settings")
        self.setMinimumSize(QSize(600, 520))
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        # General
        general = QWidget()
        g = QFormLayout(general)
        g.setVerticalSpacing(10)
        self.theme = QComboBox()
        for label, value in (("Follow the system", "system"), ("Dark", "dark"), ("Light", "light")):
            self.theme.addItem(label, value)
        self.theme.setCurrentIndex(max(0, self.theme.findData(s.theme)))
        g.addRow("Theme", self.theme)
        self.show_detection = QCheckBox("Show person / vehicle / animal detections on the video")
        self.show_detection.setChecked(s.show_detection)
        g.addRow("", self.show_detection)
        self.remember_layout = QCheckBox("Remember the grid layout")
        self.remember_layout.setChecked(s.remember_layout)
        g.addRow("", self.remember_layout)
        self.use_keyring = QCheckBox("Store passwords in the desktop keyring")
        self.use_keyring.setChecked(s.use_keyring and keyring.available)
        self.use_keyring.setEnabled(keyring.available)
        g.addRow("", self.use_keyring)
        g.addRow("", _muted("Keyring: " + ("available (Secret Service / KWallet)" if keyring.available else
                                           "not available (install python-keyring); passwords are kept in "
                                           "~/.config/reolinklinux/config.json, readable only by you")))
        tabs.addTab(general, "General")

        # Video
        video = QWidget()
        v = QFormLayout(video)
        v.setVerticalSpacing(10)
        self.grid_quality = QComboBox()
        self.grid_quality.addItem("Automatic (Clear for up to 4 videos, else Fluent)", "auto")
        self.grid_quality.addItem("Clear (main stream, full quality)", "main")
        self.grid_quality.addItem("Fluent (sub stream, low bandwidth)", "sub")
        self.grid_quality.setCurrentIndex(max(0, self.grid_quality.findData(s.grid_quality)))
        v.addRow("Grid of cameras", self.grid_quality)
        self.focus_quality = QComboBox()
        self.focus_quality.addItem("Clear (main stream, full quality)", "main")
        self.focus_quality.addItem("Fluent (sub stream, low bandwidth)", "sub")
        self.focus_quality.setCurrentIndex(max(0, self.focus_quality.findData(s.focus_quality)))
        v.addRow("Enlarged camera", self.focus_quality)
        self.protocol = QComboBox()
        self.protocol.addItem("RTSP (recommended; H.264 and H.265)", "rtsp")
        self.protocol.addItem("FLV over HTTP(S) (H.264 streams only)", "flv")
        self.protocol.setCurrentIndex(max(0, self.protocol.findData(s.protocol)))
        v.addRow("Stream protocol", self.protocol)
        self.hwdec = QComboBox()
        for label, value in (("Off: decode on the CPU (recommended; clean picture from every camera)", "no"),
                             ("Automatic (less CPU; can draw coloured dots over Reolink H.265)", "auto-copy-safe"),
                             ("Automatic, zero-copy (least CPU)", "auto-safe"),
                             ("VA-API, copy back (Intel / AMD)", "vaapi-copy"), ("NVDEC, copy back (NVIDIA)", "nvdec-copy"),
                             ("VA-API, zero-copy", "vaapi"), ("NVDEC, zero-copy", "nvdec")):
            self.hwdec.addItem(label, value)
        idx = self.hwdec.findData(s.hwdec)
        if idx < 0:
            self.hwdec.addItem(s.hwdec, s.hwdec)
            idx = self.hwdec.count() - 1
        self.hwdec.setCurrentIndex(idx)
        v.addRow("Hardware decoding", self.hwdec)
        self.low_latency = QCheckBox("Low latency (smallest delay; turn off if video stutters)")
        self.low_latency.setChecked(s.low_latency)
        v.addRow("", self.low_latency)
        self.grid_audio = QCheckBox("Play sound in the grid (otherwise only for the enlarged camera)")
        self.grid_audio.setChecked(s.grid_audio)
        v.addRow("", self.grid_audio)
        self.fill = QCheckBox("Fill tiles (crop the picture instead of showing black bars)")
        self.fill.setChecked(s.fill_tiles)
        v.addRow("", self.fill)
        self.both_lenses = QCheckBox("Show both lenses of TrackMix cameras in the grid")
        self.both_lenses.setChecked(s.both_lenses)
        v.addRow("", self.both_lenses)
        v.addRow("", _muted("If a camera's picture is covered in coloured dots or lines, right-click it and turn on "
                            "Software decoding for that camera, or set Hardware decoding to Off."))
        v.addRow("", _muted("A 4K H.265 main stream needs H.265 support in FFmpeg/mpv. Fedora and openSUSE ship "
                            "without it by default; see the README for the one-line fix."))
        tabs.addTab(video, "Video")

        # Recording
        rec = QWidget()
        r = QFormLayout(rec)
        r.setVerticalSpacing(10)
        self.video_dir = QLineEdit(str(s.videos()))
        r.addRow("Videos folder", self._browse_row(self.video_dir))
        self.picture_dir = QLineEdit(str(s.pictures()))
        r.addRow("Snapshots folder", self._browse_row(self.picture_dir))
        self.fmt = QComboBox()
        self.fmt.addItem("MP4 (plays everywhere)", "mp4")
        self.fmt.addItem("Matroska / MKV", "mkv")
        self.fmt.setCurrentIndex(max(0, self.fmt.findData(s.record_format)))
        r.addRow("File format", self.fmt)
        self.segment = QSpinBox()
        self.segment.setRange(1, 240)
        self.segment.setSuffix(" minutes")
        self.segment.setValue(s.segment_minutes)
        r.addRow("Continuous file length", self.segment)
        self.retention = QSpinBox()
        self.retention.setRange(0, 3650)
        self.retention.setSpecialValueText("Keep forever")
        self.retention.setSuffix(" days")
        self.retention.setValue(s.retention_days)
        r.addRow("Delete continuous recordings after", self.retention)
        r.addRow("", _muted("Continuous recording is switched on per camera (Edit camera). Manual recordings and "
                            "downloads are never deleted automatically."))
        ff = recorder.ffmpeg_path()
        r.addRow("FFmpeg", _muted(f"found: {ff}" if ff else "not found: install the ffmpeg package to record"))
        tabs.addTab(rec, "Recording")

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setProperty("variant", "primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _browse_row(self, edit: QLineEdit) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(lambda: self._browse(edit))
        h.addWidget(b)
        return w

    def _browse(self, edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder", edit.text())
        if path:
            edit.setText(path)

    def _save(self) -> None:
        s = self.config.settings
        self.video_changed = (s.hwdec != self.hwdec.currentData() or s.low_latency != self.low_latency.isChecked()
                              or s.protocol != self.protocol.currentData())
        s.theme = self.theme.currentData()
        s.show_detection = self.show_detection.isChecked()
        s.remember_layout = self.remember_layout.isChecked()
        s.use_keyring = self.use_keyring.isChecked()
        s.grid_quality = self.grid_quality.currentData()
        s.focus_quality = self.focus_quality.currentData()
        s.protocol = self.protocol.currentData()
        s.hwdec = self.hwdec.currentData()
        s.low_latency = self.low_latency.isChecked()
        s.grid_audio = self.grid_audio.isChecked()
        s.fill_tiles = self.fill.isChecked()
        s.both_lenses = self.both_lenses.isChecked()
        from ..core.config import default_picture_dir, default_video_dir
        vd, pd = self.video_dir.text().strip(), self.picture_dir.text().strip()
        s.video_dir = "" if vd == str(default_video_dir()) else vd
        s.picture_dir = "" if pd == str(default_picture_dir()) else pd
        s.record_format = self.fmt.currentData()
        s.segment_minutes = self.segment.value()
        s.retention_days = self.retention.value()
        self.config.save()
        self.accept()


class AboutDialog(QDialog):
    def __init__(self, parent=None, icon=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {__app_name__}")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        lay.setSpacing(8)
        head = QHBoxLayout()
        if icon is not None:
            logo = QLabel()
            logo.setPixmap(icon.pixmap(64, 64))
            head.addWidget(logo)
        titles = QVBoxLayout()
        name = QLabel(__app_name__)
        name.setFont(scaled_font(name, 1.6, bold=True))
        titles.addWidget(name)
        titles.addWidget(_muted(f"Version {__version__} · Reolink camera client for Linux", False))
        head.addLayout(titles, 1)
        lay.addLayout(head)
        lay.addWidget(_muted("Live view in full quality, PTZ control, SD-card playback and downloads, and local "
                             "recording for Reolink PoE cameras, NVRs and Home Hubs. Built with Qt, mpv and FFmpeg."))
        line = QFrame()
        line.setObjectName("Divider")
        lay.addWidget(line)
        try:
            major, minor = mpv.api_version()
            mpv_text = f"libmpv client API {major}.{minor}"
        except OSError:
            mpv_text = "libmpv not found"
        ff = recorder.ffmpeg_path()
        details = [f"Qt {qVersion()} · PySide6 {pyside_version}", mpv_text,
                   f"FFmpeg {recorder.ffmpeg_major()}" if ff else "FFmpeg not installed",
                   f"Python {platform.python_version()} on {platform.system()} {platform.release()}"]
        for d in details:
            lay.addWidget(_muted(d, False))
        lay.addWidget(_muted("ReolinkLinux is an independent project and is not affiliated with or endorsed by "
                             "Reolink. Reolink, Duo and TrackMix are trademarks of their owner."))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        home = buttons.addButton("Project page", QDialogButtonBox.ActionRole)
        home.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://github.com/oreo1298/ReolinkLinux")))
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        lay.addWidget(buttons)


class DiagnosticsDialog(QDialog):
    """Collects the diagnostics report for one camera, with Copy / Save buttons."""

    def __init__(self, parent, title: str, collect, local_lines: list[str]):
        super().__init__(parent)
        from PySide6.QtWidgets import QApplication, QPlainTextEdit

        from .theme import mono_font
        self.setWindowTitle(f"Diagnostics: {title}")
        self.resize(900, 620)
        lay = QVBoxLayout(self)
        lay.addWidget(_muted("What the camera reports and how each stream was chosen. Passwords are removed, so "
                             "this is safe to share when reporting a problem."))
        self.text = QPlainTextEdit()
        self.text.setObjectName("Log")
        self.text.setReadOnly(True)
        self.text.setFont(mono_font(9.5))
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.text.setPlainText("Collecting… opening each stream can take up to a minute.")
        lay.addWidget(self.text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.copy = buttons.addButton("Copy", QDialogButtonBox.ActionRole)
        self.copy.setProperty("variant", "primary")
        self.copy.setEnabled(False)
        self.copy.clicked.connect(lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        self.save = buttons.addButton("Save…", QDialogButtonBox.ActionRole)
        self.save.setEnabled(False)
        self.save.clicked.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._local = local_lines
        worker.run(collect, self._done, self._failed)

    def _done(self, text: str) -> None:
        self.text.setPlainText(text + ("\n\n== this PC's players\n" + "\n".join(self._local) if self._local else ""))
        self.copy.setEnabled(True)
        self.save.setEnabled(True)

    def _failed(self, exc: Exception) -> None:
        self.text.setPlainText(f"Could not collect the report: {exc}\n\n" + "\n".join(self._local))
        self.copy.setEnabled(True)

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save diagnostics", "reolinklinux-diagnostics.txt")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.text.toPlainText())
