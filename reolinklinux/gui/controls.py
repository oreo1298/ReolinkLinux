"""The control panel for the selected camera: view, PTZ, lights and device details."""

from __future__ import annotations

import datetime as dt
import webbrowser

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.models import MAIN, SUB, Channel
from .live import VideoWall
from .manager import CameraManager, RecordingManager, Source
from .theme import theme
from .widgets import (
    KeyValueGrid,
    SegmentedControl,
    StatTile,
    StatusDot,
    bind_icon,
    flat_button,
    human_size,
    scaled_font,
)


def _section(title: str) -> QLabel:
    label = QLabel(title.upper())
    label.setObjectName("CardTitle")
    label.setFont(scaled_font(label, 0.78, bold=True))
    return label


def _divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    return line


class PadButton(QToolButton):
    """A PTZ direction button: moves while held, stops on release."""

    def __init__(self, icon: str, op: str, tooltip: str, center: bool = False):
        super().__init__()
        self.op = op
        self.setObjectName("PadCenter" if center else "Pad")
        self.setToolTip(tooltip)
        self.setIconSize(QSize(20, 20))
        self.setFixedSize(QSize(44, 44))
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoRepeat(False)
        self._binder = bind_icon(self, icon)


class ControlPanel(QWidget):
    snapshot_requested = Signal()
    record_requested = Signal()
    enlarge_requested = Signal()

    def __init__(self, cameras: CameraManager, recordings: RecordingManager, wall: VideoWall):
        super().__init__()
        self.cameras = cameras
        self.recordings = recordings
        self.wall = wall
        self.cam_id: str | None = None
        self.ch_index = 0
        self._updating = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.dot = StatusDot(9)
        head.addWidget(self.dot, 0, Qt.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.name = QLabel("No camera selected")
        self.name.setObjectName("Title")
        self.name.setFont(scaled_font(self.name, 1.12, bold=True))
        self.model = QLabel("Select a camera in the list or click a video.")
        self.model.setObjectName("Muted")
        self.model.setWordWrap(True)
        titles.addWidget(self.name)
        titles.addWidget(self.model)
        head.addLayout(titles, 1)
        lay.addLayout(head)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        lay.addWidget(self.tabs, 1)
        self.tabs.addTab(self._scroll(self._build_controls()), "Controls")
        self.tabs.addTab(self._scroll(self._build_device()), "Device")

        cameras.camera_changed.connect(self._camera_changed)
        cameras.detection_changed.connect(self._poll_update)
        recordings.changed.connect(lambda _k: self._update_view_buttons())
        wall.focus_changed.connect(lambda _f: self._update_view_buttons())
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._update_stats)
        self._stats_timer.start(2000)
        self.set_camera(None, 0)

    @staticmethod
    def _scroll(widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(widget)
        return area

    # ------------------------------------------------------------------ build: controls
    def _build_controls(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 8, 8, 8)
        lay.setSpacing(10)

        # View
        self.view_box = QWidget()
        v = QVBoxLayout(self.view_box)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(_section("View"))
        self.lens_row = QWidget()
        lr = QHBoxLayout(self.lens_row)
        lr.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel("Lens")
        lbl.setObjectName("Muted")
        lbl.setFixedWidth(64)
        lr.addWidget(lbl)
        self.lens = SegmentedControl()
        self.lens.add("Wide", "wide", "Wide-angle lens")
        self.lens.add("Tele", "tele", "Telephoto lens")
        self.lens.add("Both", "both", "Both lenses side by side")
        self.lens.changed.connect(self._set_lens)
        lr.addWidget(self.lens, 1)
        v.addWidget(self.lens_row)
        qr = QHBoxLayout()
        lbl = QLabel("Quality")
        lbl.setObjectName("Muted")
        lbl.setFixedWidth(64)
        qr.addWidget(lbl)
        self.quality = SegmentedControl()
        self.quality.add("Clear", MAIN, "Main stream: full resolution")
        self.quality.add("Fluent", SUB, "Sub stream: low bandwidth")
        self.quality.changed.connect(self._set_quality)
        qr.addWidget(self.quality, 1)
        v.addLayout(qr)
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.btn_snapshot = flat_button("snapshot", "Save a full-resolution snapshot (Ctrl+S)")
        self.btn_snapshot.clicked.connect(self.snapshot_requested.emit)
        self.btn_record = flat_button("record", "Record to this PC (Ctrl+R)", checkable=True)
        self.btn_record.clicked.connect(self.record_requested.emit)
        self.btn_mute = flat_button("mute", "Sound on/off (M)", checkable=True)
        self.btn_mute.clicked.connect(self._toggle_mute)
        self.btn_enlarge = flat_button("expand", "Enlarge / back to grid (double-click a video)", checkable=True)
        self.btn_enlarge.clicked.connect(self.enlarge_requested.emit)
        self.btn_zoom_reset = flat_button("zoom_out", "Reset digital zoom (scroll on the video to zoom)")
        self.btn_zoom_reset.clicked.connect(self._reset_digital_zoom)
        for b in (self.btn_snapshot, self.btn_record, self.btn_mute, self.btn_enlarge, self.btn_zoom_reset):
            buttons.addWidget(b)
        buttons.addStretch(1)
        v.addLayout(buttons)
        lay.addWidget(self.view_box)

        # PTZ
        self.ptz_box = QWidget()
        p = QVBoxLayout(self.ptz_box)
        p.setContentsMargins(0, 0, 0, 0)
        p.setSpacing(8)
        p.addWidget(_divider())
        p.addWidget(_section("Pan · tilt · zoom"))
        pad_row = QHBoxLayout()
        pad_row.addStretch(1)
        self.pad = QWidget()
        grid = QGridLayout(self.pad)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(5)
        dirs = [("up_left", "LeftUp", 0, 0), ("up", "Up", 0, 1), ("up_right", "RightUp", 0, 2),
                ("left", "Left", 1, 0), ("right", "Right", 1, 2),
                ("down_left", "LeftDown", 2, 0), ("down", "Down", 2, 1), ("down_right", "RightDown", 2, 2)]
        self.pad_buttons: list[PadButton] = []
        for icon, op, r, c in dirs:
            b = PadButton(icon, op, f"Move {op.lower().replace('left', ' left').replace('right', ' right').strip()}")
            b.pressed.connect(lambda op=op: self._ptz_move(op))
            b.released.connect(self._ptz_stop)
            grid.addWidget(b, r, c)
            self.pad_buttons.append(b)
        self.btn_scan = PadButton("scan", "Auto", "Auto scan (pan continuously); press again to stop", center=True)
        self.btn_scan.setCheckable(True)
        self.btn_scan.clicked.connect(self._auto_scan)
        grid.addWidget(self.btn_scan, 1, 1)
        pad_row.addWidget(self.pad)
        pad_row.addStretch(1)
        p.addLayout(pad_row)

        self.speed_row = QWidget()
        sr = QHBoxLayout(self.speed_row)
        sr.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel("Speed")
        lbl.setObjectName("Muted")
        lbl.setFixedWidth(64)
        sr.addWidget(lbl)
        self.speed = QSlider(Qt.Horizontal)
        self.speed.setRange(1, 64)
        self.speed.setValue(self.cameras.config.settings.ptz_speed)
        self.speed.valueChanged.connect(self._speed_changed)
        sr.addWidget(self.speed, 1)
        self.speed_value = QLabel(str(self.speed.value()))
        self.speed_value.setFixedWidth(24)
        self.speed_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        sr.addWidget(self.speed_value)
        p.addWidget(self.speed_row)

        self.zoom_row, self.zoom_slider = self._stepper_row("Zoom", "ZoomDec", "ZoomInc", "zoom_out", "zoom_in",
                                                            self._zoom_released)
        p.addWidget(self.zoom_row)
        self.focus_row, self.focus_slider = self._stepper_row("Focus", "FocusDec", "FocusInc", "left", "right",
                                                              self._focus_released)
        p.addWidget(self.focus_row)
        self.autofocus = QCheckBox("Auto focus")
        self.autofocus.toggled.connect(self._autofocus_toggled)
        p.addWidget(self.autofocus)

        self.preset_row = QWidget()
        pr = QHBoxLayout(self.preset_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(6)
        lbl = QLabel("Preset")
        lbl.setObjectName("Muted")
        lbl.setFixedWidth(64)
        pr.addWidget(lbl)
        self.presets = QComboBox()
        self.presets.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.presets.activated.connect(lambda _i: self._goto_preset())
        pr.addWidget(self.presets, 1)
        go = QPushButton("Go")
        go.clicked.connect(self._goto_preset)
        pr.addWidget(go)
        more = QToolButton()
        more.setObjectName("Flat")
        more.setToolTip("Save or delete presets")
        more.setPopupMode(QToolButton.InstantPopup)
        more._binder = bind_icon(more, "menu", "text_muted")
        menu = QMenu(more)
        menu.addAction("Save current position as preset…", self._save_preset)
        menu.addAction("Delete selected preset", self._delete_preset)
        more.setMenu(menu)
        pr.addWidget(more)
        p.addWidget(self.preset_row)

        self.patrol_row = QWidget()
        tr = QHBoxLayout(self.patrol_row)
        tr.setContentsMargins(0, 0, 0, 0)
        tr.setSpacing(6)
        lbl = QLabel("Patrol")
        lbl.setObjectName("Muted")
        lbl.setFixedWidth(64)
        tr.addWidget(lbl)
        self.patrols = QComboBox()
        self.patrols.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        tr.addWidget(self.patrols, 1)
        self.btn_patrol = QPushButton("Start")
        self.btn_patrol.setCheckable(True)
        self.btn_patrol.clicked.connect(self._toggle_patrol)
        tr.addWidget(self.btn_patrol)
        p.addWidget(self.patrol_row)

        self.autotrack = QCheckBox("Auto tracking (follow people and vehicles)")
        self.autotrack.toggled.connect(self._autotrack_toggled)
        p.addWidget(self.autotrack)

        self.guard_row = QWidget()
        gr = QHBoxLayout(self.guard_row)
        gr.setContentsMargins(0, 0, 0, 0)
        gr.setSpacing(6)
        lbl = QLabel("Guard")
        lbl.setObjectName("Muted")
        lbl.setFixedWidth(64)
        gr.addWidget(lbl)
        set_guard = QPushButton("Set here")
        set_guard.setToolTip("Make the current position the guard (home) position the camera returns to")
        set_guard.clicked.connect(self._set_guard)
        go_guard = QPushButton("Go")
        go_guard.setToolTip("Move to the guard position")
        go_guard.clicked.connect(lambda: self._control(lambda d, i: d.goto_guard(i), "Guard position"))
        gr.addWidget(set_guard)
        gr.addWidget(go_guard)
        gr.addStretch(1)
        self.btn_calibrate = QPushButton("Calibrate")
        self.btn_calibrate.setToolTip("Run the PTZ self-check / calibration")
        self.btn_calibrate.clicked.connect(self._calibrate)
        gr.addWidget(self.btn_calibrate)
        p.addWidget(self.guard_row)
        lay.addWidget(self.ptz_box)

        # Lights and alarm
        self.lights_box = QWidget()
        li = QVBoxLayout(self.lights_box)
        li.setContentsMargins(0, 0, 0, 0)
        li.setSpacing(8)
        li.addWidget(_divider())
        li.addWidget(_section("Lights & alarm"))
        self.ir_row = QWidget()
        ir = QHBoxLayout(self.ir_row)
        ir.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel("Infrared")
        lbl.setObjectName("Muted")
        lbl.setFixedWidth(64)
        ir.addWidget(lbl)
        self.ir = SegmentedControl()
        self.ir.add("Auto", "Auto", "Infrared LEDs switch on in the dark")
        self.ir.add("On", "On", "Infrared LEDs always on")
        self.ir.add("Off", "Off", "Infrared LEDs off")
        self.ir.changed.connect(self._set_ir)
        ir.addWidget(self.ir, 1)
        li.addWidget(self.ir_row)
        self.spot_row = QWidget()
        sp = QHBoxLayout(self.spot_row)
        sp.setContentsMargins(0, 0, 0, 0)
        self.spotlight = QCheckBox("Spotlight")
        self.spotlight.setFixedWidth(96)
        self.spotlight.toggled.connect(self._spot_toggled)
        sp.addWidget(self.spotlight)
        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(0, 100)
        self.brightness.setToolTip("Spotlight brightness")
        self.brightness.sliderReleased.connect(self._brightness_released)
        sp.addWidget(self.brightness, 1)
        li.addWidget(self.spot_row)
        self.siren_row = QWidget()
        sr2 = QHBoxLayout(self.siren_row)
        sr2.setContentsMargins(0, 0, 0, 0)
        self.btn_siren = QPushButton("Sound siren")
        self.btn_siren.setProperty("variant", "danger")
        self.btn_siren._binder = bind_icon(self.btn_siren, "siren", "danger")
        self.btn_siren.clicked.connect(self._siren)
        sr2.addWidget(self.btn_siren)
        stop = QPushButton("Stop")
        stop.clicked.connect(lambda: self._control(lambda d, i: d.siren(i, False), "Siren"))
        sr2.addWidget(stop)
        sr2.addStretch(1)
        li.addWidget(self.siren_row)
        lay.addWidget(self.lights_box)
        lay.addStretch(1)
        return page

    def _stepper_row(self, label: str, dec_op: str, inc_op: str, dec_icon: str, inc_icon: str, released):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        lbl = QLabel(label)
        lbl.setObjectName("Muted")
        lbl.setFixedWidth(64)
        h.addWidget(lbl)
        dec = flat_button(dec_icon, f"{label} out" if label == "Zoom" else f"{label} near")
        dec.pressed.connect(lambda: self._ptz_move(dec_op))
        dec.released.connect(self._ptz_stop_and_refresh)
        slider = QSlider(Qt.Horizontal)
        slider.sliderReleased.connect(released)
        inc = flat_button(inc_icon, f"{label} in" if label == "Zoom" else f"{label} far")
        inc.pressed.connect(lambda: self._ptz_move(inc_op))
        inc.released.connect(self._ptz_stop_and_refresh)
        h.addWidget(dec)
        h.addWidget(slider, 1)
        h.addWidget(inc)
        return row, slider

    # ------------------------------------------------------------------ build: device
    def _build_device(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(2, 8, 8, 8)
        lay.setSpacing(10)
        tiles = QHBoxLayout()
        tiles.setSpacing(8)
        self.tile_main = StatTile("Clear stream")
        self.tile_sub = StatTile("Fluent stream")
        tiles.addWidget(self.tile_main)
        tiles.addWidget(self.tile_sub)
        lay.addLayout(tiles)
        lay.addWidget(_section("Device"))
        self.info = KeyValueGrid()
        lay.addWidget(self.info)
        lay.addWidget(_divider())
        lay.addWidget(_section("Playback"))
        self.stats = KeyValueGrid()
        lay.addWidget(self.stats)
        lay.addWidget(_divider())
        row = QHBoxLayout()
        self.btn_sync = QPushButton("Sync clock")
        self.btn_sync.setToolTip("Set the camera's clock to this PC's time")
        self.btn_sync.clicked.connect(self._sync_clock)
        self.btn_web = QPushButton("Web interface")
        self.btn_web.setToolTip("Open the camera's own web page in your browser")
        self.btn_web.clicked.connect(self._open_web)
        self.btn_reboot = QPushButton("Reboot")
        self.btn_reboot.setProperty("variant", "danger")
        self.btn_reboot.clicked.connect(self._reboot)
        row.addWidget(self.btn_sync)
        row.addWidget(self.btn_web)
        row.addStretch(1)
        row.addWidget(self.btn_reboot)
        lay.addLayout(row)
        lay.addStretch(1)
        return page

    # ------------------------------------------------------------------ state
    def channel(self) -> Channel | None:
        return self.cameras.channel(self.cam_id, self.ch_index) if self.cam_id else None

    def source(self) -> Source | None:
        if not self.cam_id:
            return None
        tile = self.wall.primary_tile(self.cam_id, self.ch_index)
        return tile.source if tile else Source(self.cam_id, self.ch_index)

    def set_camera(self, cam_id: str | None, ch_index: int) -> None:
        self.cam_id, self.ch_index = cam_id, ch_index
        self.refresh()

    def _camera_changed(self, cam_id: str) -> None:
        if cam_id == self.cam_id:
            self.refresh()

    def _poll_update(self, cam_id: str) -> None:
        if cam_id != self.cam_id or self._updating:
            return
        ch = self.channel()
        if not ch:
            return
        self._updating = True
        try:
            self._sync_lights(ch)
            if ch.auto_track_on is not None and not self.autotrack.hasFocus():
                self.autotrack.setChecked(bool(ch.auto_track_on))
        finally:
            self._updating = False

    def refresh(self) -> None:
        self._updating = True
        try:
            self._refresh()
        finally:
            self._updating = False

    def _refresh(self) -> None:
        pal = theme.palette
        e = self.cameras.entry(self.cam_id) if self.cam_id else None
        ch = self.channel()
        enabled = ch is not None
        self.tabs.setEnabled(enabled)
        if not e:
            self.dot.set_color(pal.text_faint, False)
            self.name.setText("No camera selected")
            self.model.setText("Select a camera in the list or click a video.")
            for box in (self.ptz_box, self.lights_box):
                box.hide()
            self.lens_row.hide()
            self._fill_info(None, None)
            return
        dev = e.device
        self.name.setText(self.cameras.label(self.cam_id, self.ch_index))
        if e.online and ch:
            self.dot.set_color(pal.success if ch.online else pal.text_faint, ch.online)
            host = "demo camera" if e.cfg.demo else e.cfg.host
            model = ch.model or dev.info.model
            self.model.setText(f"{model} · {host}" + (f" · channel {ch.index + 1}" if dev.is_nvr else ""))
        else:
            color = {"connecting": pal.warning, "error": pal.danger}.get(e.status, pal.text_faint)
            self.dot.set_color(color, False)
            self.model.setText({"connecting": "Connecting…", "offline": "Disconnected"}.get(e.status, e.error))
        if not ch:
            for box in (self.ptz_box, self.lights_box):
                box.hide()
            self.lens_row.hide()
            self._fill_info(e, None)
            return
        caps = ch.caps
        self.lens_row.setVisible(caps.telephoto)
        self.lens.set_value(self.wall.lens_mode(self.cam_id, self.ch_index))
        self._update_view_buttons()

        # PTZ
        self.ptz_box.setVisible(caps.any_ptz or caps.auto_track)
        self.pad.setVisible(caps.pan_tilt)
        self.speed_row.setVisible(caps.pan_tilt and caps.ptz_speed)
        self.zoom_row.setVisible(caps.optical_zoom)
        self.zoom_slider.setRange(ch.zoom.zoom_min, max(ch.zoom.zoom_min, ch.zoom.zoom_max))
        self.zoom_slider.setValue(ch.zoom.zoom_pos)
        self.focus_row.setVisible(caps.focus)
        self.focus_slider.setRange(ch.zoom.focus_min, max(ch.zoom.focus_min, ch.zoom.focus_max))
        self.focus_slider.setValue(ch.zoom.focus_pos)
        self.autofocus.setVisible(caps.auto_focus)
        self.autofocus.setChecked(bool(ch.auto_focus_on))
        self.preset_row.setVisible(caps.presets)
        self._fill_presets(ch)
        self.patrol_row.setVisible(caps.patrol)
        self.patrols.clear()
        for pat in ch.patrols:
            self.patrols.addItem(pat.name, pat.id)
        self.autotrack.setVisible(caps.auto_track)
        self.autotrack.setChecked(bool(ch.auto_track_on))
        self.guard_row.setVisible(caps.guard or caps.calibrate)
        self.btn_calibrate.setVisible(caps.calibrate)

        # Lights
        self.lights_box.setVisible(caps.ir_lights or caps.spotlight or caps.siren)
        self.ir_row.setVisible(caps.ir_lights)
        self.spot_row.setVisible(caps.spotlight)
        self.siren_row.setVisible(caps.siren)
        self._sync_lights(ch)
        self._fill_info(e, ch)

    def _sync_lights(self, ch: Channel) -> None:
        if ch.lights.ir_state:
            self.ir.set_value(ch.lights.ir_state)
        if ch.lights.spotlight_on is not None:
            self.spotlight.setChecked(bool(ch.lights.spotlight_on))
        if ch.lights.spotlight_brightness is not None and not self.brightness.isSliderDown():
            self.brightness.setValue(int(ch.lights.spotlight_brightness))
        self.brightness.setVisible(ch.lights.spotlight_brightness is not None)

    def _fill_presets(self, ch: Channel) -> None:
        current = self.presets.currentData()
        self.presets.clear()
        for preset in ch.presets:
            self.presets.addItem(preset.name, preset.id)
        if not ch.presets:
            self.presets.addItem("No presets yet", None)
        idx = self.presets.findData(current)
        if idx >= 0:
            self.presets.setCurrentIndex(idx)

    def _update_view_buttons(self) -> None:
        src = self.source()
        if not src:
            return
        ch = self.channel()
        tile = self.wall.tiles.get(src.key)
        self.quality.set_value(self.wall.tile_quality.get(src.key) or self.wall.quality_for(src))
        self.btn_record.setChecked(self.recordings.is_recording(src.key))
        muted = tile.video.muted if tile else True
        self.btn_mute.setChecked(not muted)
        self.btn_mute._binder.set_name("volume" if not muted else "mute")
        self.btn_enlarge.setChecked(self.wall.focus == (self.cam_id, self.ch_index))
        if ch:
            self.lens.set_value(self.wall.lens_mode(self.cam_id, self.ch_index))

    def _fill_info(self, e, ch: Channel | None) -> None:
        self.info.clear()
        if not e or not e.online:
            self.tile_main.set("—")
            self.tile_sub.set("—")
            self.info.add_row("Status", e.error if e and e.error else "Not connected")
            if e and not e.cfg.demo:
                self.info.add_row("Address", e.cfg.host)
            return
        dev = e.device
        if ch:
            codec = (dev.stream_codec(ch.index, 0, MAIN) or ch.main.codec or "").upper()
            self.tile_main.set(ch.main.resolution or "—", f"{codec} · {ch.main.fps} fps · {ch.main.bitrate} kbps"
                               if ch.main.fps else "")
            self.tile_main.caption.setText(f"Clear · {codec}" if codec else "Clear stream")
            sub_codec = (ch.sub.codec or "").upper()
            self.tile_sub.set(ch.sub.resolution or "—")
            self.tile_sub.caption.setText(f"Fluent · {sub_codec}" if sub_codec else "Fluent stream")
        info = dev.info
        rows = [("Model", (ch.model if ch and dev.is_nvr and ch.model else info.model) or "—"),
                ("Name", info.name or "—"),
                ("Firmware", info.firmware or "—"),
                ("Hardware", info.hardware or "—"),
                ("Address", "demo" if e.cfg.demo else f"{dev.client.scheme}://{e.cfg.host}:{dev.client.effective_port}"),
                ("MAC", info.mac or "—"),
                ("UID", info.uid or "—"),
                ("Serial", info.serial or "—"),
                ("Storage", dev.storage_summary())]
        if ch:
            feats = []
            c = ch.caps
            for flag, name in ((c.pan_tilt, "pan/tilt"), (c.optical_zoom, "zoom"), (c.telephoto, "dual lens"),
                               (c.auto_track, "auto tracking"), (c.spotlight, "spotlight"), (c.ir_lights, "infrared"),
                               (c.siren, "siren")):
                if flag:
                    feats.append(name)
            ai = {"people": "person", "vehicle": "vehicle", "dog_cat": "animal", "face": "face", "package": "package"}
            dets = [ai.get(a, a) for a in c.ai_types]
            if c.telephoto and ch.tele_main.resolution:
                rows.append(("Telephoto", f"{ch.tele_main.resolution} {ch.tele_main.codec.upper()}".strip()))
            rows.append(("Features", ", ".join(feats) or "—"))
            rows.append(("AI detection", ", ".join(dets) or "motion only"))
        if not e.cfg.demo:
            p = dev.ports
            rows.append(("Ports", f"RTSP {p['rtsp']}{'' if p['rtsp_enabled'] else ' (off)'} · "
                                  f"RTMP {p['rtmp']}{'' if p['rtmp_enabled'] else ' (off)'} · "
                                  f"ONVIF {p['onvif']}{'' if p['onvif_enabled'] else ' (off)'}"))
        offset = dev.clock_offset()
        if dev.clock:
            drift = f" ({offset:+.0f} s vs this PC)" if offset is not None and abs(offset) >= 2 else ""
            rows.append(("Clock", dev.clock.strftime("%Y-%m-%d %H:%M:%S") + drift))
        for k, v in rows:
            self.info.add_row(k, v)
        self._update_stats()

    def _update_stats(self) -> None:
        if not self.isVisible() or self.tabs.currentIndex() != 1:
            return
        self.stats.clear()
        tile = self.wall.primary_tile(self.cam_id, self.ch_index) if self.cam_id else None
        st = tile.video.stats() if tile else {}
        if not st:
            self.stats.add_row("Video", "not playing")
            return
        self.stats.add_row("Resolution", f"{st['width']}×{st['height']}")
        self.stats.add_row("Codec", st["codec"])
        self.stats.add_row("Decoder", "software" if st["hwdec"] in ("no", "") else f"hardware ({st['hwdec']})")
        self.stats.add_row("Frame rate", f"{st['fps']:.1f} fps")
        if st["bitrate"]:
            self.stats.add_row("Bitrate", f"{st['bitrate'] / 1000:.0f} kbps ({human_size(st['bitrate'] / 8)}/s)")
        if st["dropped"]:
            self.stats.add_row("Dropped frames", str(st["dropped"]))

    # ------------------------------------------------------------------ actions
    def _control(self, fn, what: str, done=None) -> None:
        if not self.cam_id:
            return
        idx = self.ch_index
        self.cameras.control(self.cam_id, lambda d: fn(d, idx), done, what)

    def _set_lens(self, mode) -> None:
        if self._updating or not self.cam_id:
            return
        self.wall.set_lens_mode(self.cam_id, self.ch_index, mode)
        self._update_view_buttons()

    def _set_quality(self, quality) -> None:
        if self._updating or not self.cam_id:
            return
        self.wall.set_view_quality(self.cam_id, self.ch_index, quality)

    def _toggle_mute(self) -> None:
        src = self.source()
        if src:
            self.wall.toggle_mute(src)
        self._update_view_buttons()

    def _reset_digital_zoom(self) -> None:
        for tile in self.wall.tiles_for_view(self.cam_id, self.ch_index) if self.cam_id else []:
            tile.video.reset_zoom()

    def _speed_changed(self, value: int) -> None:
        self.speed_value.setText(str(value))
        self.cameras.config.settings.ptz_speed = value

    def _ptz_move(self, op: str) -> None:
        speed = self.speed.value()
        self._control(lambda d, i: d.ptz(i, op, speed=speed), "PTZ")

    def _ptz_stop(self) -> None:
        self._control(lambda d, i: d.ptz_stop(i), "PTZ")

    def _ptz_stop_and_refresh(self) -> None:
        def work(d, i):
            d.ptz_stop(i)
            try:
                d.refresh_zoom(i)
            except Exception:  # noqa: BLE001 - the position readback is best-effort
                pass

        self._control(work, "PTZ", lambda _r: self.refresh())

    def _auto_scan(self, checked: bool) -> None:
        speed = self.speed.value()
        if checked:
            self._control(lambda d, i: d.ptz(i, "Auto", speed=speed), "Auto scan")
        else:
            self._ptz_stop()

    def _zoom_released(self) -> None:
        pos = self.zoom_slider.value()
        self._control(lambda d, i: d.set_zoom(i, pos), "Zoom")

    def _focus_released(self) -> None:
        pos = self.focus_slider.value()
        self._control(lambda d, i: d.set_focus(i, pos), "Focus")

    def _autofocus_toggled(self, on: bool) -> None:
        if not self._updating:
            self._control(lambda d, i: d.set_auto_focus(i, on), "Auto focus")

    def _goto_preset(self) -> None:
        pid = self.presets.currentData()
        if pid is None:
            return
        speed = self.speed.value()
        self._control(lambda d, i: d.goto_preset(i, pid, speed), "Preset")

    def _save_preset(self) -> None:
        ch = self.channel()
        if not ch:
            return
        name, ok = QInputDialog.getText(self, "Save preset", "Name for the current position:",
                                        text=f"Preset {len(ch.presets) + 1}")
        if not ok or not name.strip():
            return
        existing = next((p for p in ch.presets if p.name == name.strip()), None)

        def work(d, i):
            pid = existing.id if existing else d.free_preset_id(i)
            d.save_preset(i, pid, name.strip())

        self._control(work, "Save preset", lambda _r: self.refresh())

    def _delete_preset(self) -> None:
        pid = self.presets.currentData()
        if pid is None:
            return
        name = self.presets.currentText()
        if QMessageBox.question(self, "Delete preset", f"Delete the preset “{name}”?") != QMessageBox.Yes:
            return
        self._control(lambda d, i: d.delete_preset(i, pid), "Delete preset", lambda _r: self.refresh())

    def _toggle_patrol(self, checked: bool) -> None:
        pid = self.patrols.currentData()
        if pid is None:
            self.btn_patrol.setChecked(False)
            return
        self.btn_patrol.setText("Stop" if checked else "Start")
        if checked:
            self._control(lambda d, i: d.start_patrol(i, pid), "Patrol")
        else:
            self._control(lambda d, i: d.stop_patrol(i, pid), "Patrol")

    def _autotrack_toggled(self, on: bool) -> None:
        if not self._updating:
            self._control(lambda d, i: d.set_auto_track(i, on), "Auto tracking")

    def _set_guard(self) -> None:
        self._control(lambda d, i: d.set_guard_here(i, True), "Guard position",
                      lambda _r: self.cameras.notify.emit("Guard position saved", "success"))

    def _calibrate(self) -> None:
        if QMessageBox.question(self, "Calibrate PTZ",
                                "The camera will pan and tilt through its whole range to recalibrate. Continue?") \
                != QMessageBox.Yes:
            return
        self._control(lambda d, i: d.calibrate(i), "Calibration")

    def _set_ir(self, state) -> None:
        if not self._updating:
            self._control(lambda d, i: d.set_ir(i, state), "Infrared")

    def _spot_toggled(self, on: bool) -> None:
        if not self._updating:
            self._control(lambda d, i: d.set_spotlight(i, on=on), "Spotlight")

    def _brightness_released(self) -> None:
        value = self.brightness.value()
        self._control(lambda d, i: d.set_spotlight(i, brightness=value), "Spotlight")

    def _siren(self) -> None:
        self._control(lambda d, i: d.siren(i, True, 1), "Siren",
                      lambda _r: self.cameras.notify.emit("Siren sounded", "warning"))

    def _sync_clock(self) -> None:
        now = dt.datetime.now()
        self._control(lambda d, i: d.sync_clock(now), "Clock",
                      lambda _r: (self.cameras.notify.emit("Camera clock set to this PC's time", "success"),
                                  self.refresh()))

    def _open_web(self) -> None:
        e = self.cameras.entry(self.cam_id) if self.cam_id else None
        if not e or e.cfg.demo:
            return
        dev = e.device
        scheme = dev.client.scheme if dev else ("https" if e.cfg.https is not False else "http")
        webbrowser.open(f"{scheme}://{e.cfg.host}" + (f":{e.cfg.port}" if e.cfg.port not in (None, 80, 443) else ""))

    def _reboot(self) -> None:
        e = self.cameras.entry(self.cam_id) if self.cam_id else None
        if not e:
            return
        if QMessageBox.question(self, "Reboot", f"Reboot {e.cfg.label}? Video stops for about a minute.") \
                != QMessageBox.Yes:
            return
        self._control(lambda d, i: d.reboot(), "Reboot",
                      lambda _r: self.cameras.notify.emit(f"{e.cfg.label} is rebooting", "info"))
