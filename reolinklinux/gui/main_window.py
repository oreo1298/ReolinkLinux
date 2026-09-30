"""The main window: toolbar, live view (cameras · video wall · controls) and playback."""

from __future__ import annotations

import base64
import datetime as dt

from PySide6.QtCore import QByteArray, QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__, __version__
from ..core import recorder as rec_mod
from ..core.config import CameraConfig, Config
from ..core.models import MAIN, SUB, TELE
from . import mpv, worker
from .controls import ControlPanel
from .dialogs import AboutDialog, CameraDialog, SettingsDialog
from .live import CameraList, VideoWall
from .manager import CameraManager, DownloadManager, RecordingManager, Source
from .playback import PlaybackPage
from .theme import theme
from .widgets import Card, StatusDot, Toast, bind_icon, flat_button, scaled_font, set_prop, tool_button

PTZ_KEYS = {Qt.Key_Left: "Left", Qt.Key_Right: "Right", Qt.Key_Up: "Up", Qt.Key_Down: "Down",
            Qt.Key_Plus: "ZoomInc", Qt.Key_Equal: "ZoomInc", Qt.Key_Minus: "ZoomDec"}


class MainWindow(QMainWindow):
    def __init__(self, config: Config, app_icon=None):
        super().__init__()
        self.config = config
        self.app_icon = app_icon
        self.setWindowTitle(__app_name__)
        self.resize(1480, 900)
        self.setMinimumSize(QSize(980, 600))
        self.cameras = CameraManager(config)
        self.recordings = RecordingManager(self.cameras, config)
        self.downloads = DownloadManager(self.cameras)
        self._fullscreen = False
        self._ptz_key: int | None = None
        self._closing = False

        self._build_toolbar()
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)
        self.stack.addWidget(self._build_live())
        self.playback = PlaybackPage(self.cameras, self.downloads, config)
        self.stack.addWidget(self.playback)
        self._build_statusbar()
        self.toast = Toast(self)

        for source in (self.cameras.notify, self.recordings.notify, self.downloads.notify, self.playback.notify):
            source.connect(self.show_message)
        self.cameras.cameras_changed.connect(self._update_status)
        self.cameras.camera_changed.connect(lambda _c: self._update_status())
        self.recordings.changed.connect(lambda _k: self._update_record_button())
        self.recordings.changed.connect(lambda _k: self._update_status())
        self.downloads.job_changed.connect(lambda _j: self._update_status())
        self.downloads.job_added.connect(lambda _j: self._update_status())
        self._shortcuts()
        QApplication.instance().installEventFilter(self)
        self._restore()
        self._update_status()
        if not mpv.available():
            QTimer.singleShot(300, lambda: QMessageBox.warning(
                self, "libmpv is missing", "Video needs the mpv library.\n\n" + mpv.load_error()))
        QTimer.singleShot(0, self.cameras.connect_all)

    # ------------------------------------------------------------------ toolbar
    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        bar.setFloatable(False)
        bar.setIconSize(QSize(22, 22))
        bar.setContextMenuPolicy(Qt.PreventContextMenu)
        self.addToolBar(Qt.TopToolBarArea, bar)
        self.toolbar = bar

        ident = QWidget()
        il = QHBoxLayout(ident)
        il.setContentsMargins(4, 0, 14, 0)
        il.setSpacing(10)
        logo = QLabel()
        if self.app_icon is not None and not self.app_icon.isNull():
            logo.setPixmap(self.app_icon.pixmap(34, 34))
        else:
            self._logo = bind_icon(logo, "cctv", "accent", 30)
        il.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        app = QLabel(__app_name__)
        app.setObjectName("AppName")
        app.setFont(scaled_font(app, 1.28, bold=True))
        sub = QLabel("Reolink camera client for Linux")
        sub.setObjectName("Faint")
        sub.setFont(scaled_font(sub, 0.8))
        names.addWidget(app)
        names.addWidget(sub)
        il.addLayout(names)
        bar.addWidget(ident)
        bar.addSeparator()

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.btn_live = tool_button("Live", "live", "Live view (Ctrl+1)", checkable=True)
        self.btn_playback = tool_button("Playback", "film", "Recordings on the SD card / NVR (Ctrl+2)", checkable=True)
        self.btn_live.setChecked(True)
        self.nav.addButton(self.btn_live, 0)
        self.nav.addButton(self.btn_playback, 1)
        self.nav.idClicked.connect(self.show_page)
        bar.addWidget(self.btn_live)
        bar.addWidget(self.btn_playback)
        bar.addSeparator()

        self.btn_add = tool_button("Add camera", "plus", "Add a camera, NVR or Home Hub (Ctrl+N)")
        self.btn_add.clicked.connect(self.add_camera)
        bar.addWidget(self.btn_add)
        self.btn_snapshot = tool_button("Snapshot", "snapshot", "Save a full-resolution picture (Ctrl+S)")
        self.btn_snapshot.clicked.connect(self.snapshot)
        bar.addWidget(self.btn_snapshot)
        self.btn_record = tool_button("Record", "record", "Record the selected camera to this PC (Ctrl+R)",
                                      checkable=True)
        self.btn_record.setProperty("accent", True)
        self.btn_record.clicked.connect(self.toggle_record)
        bar.addWidget(self.btn_record)
        bar.addSeparator()

        self.btn_layout = tool_button("Layout", "grid", "Grid layout")
        self.btn_layout.setPopupMode(self.btn_layout.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.btn_layout)
        group = QActionGroup(menu)
        self.layout_actions: dict[str, QAction] = {}
        for mode, label in (("auto", "Automatic"), ("1", "1 camera"), ("4", "2 × 2"), ("9", "3 × 3"),
                            ("16", "4 × 4")):
            act = QAction(label, menu, checkable=True)
            act.triggered.connect(lambda _c=False, m=mode: self.set_layout(m))
            group.addAction(act)
            menu.addAction(act)
            self.layout_actions[mode] = act
        menu.addSeparator()
        self.act_fill = QAction("Fill tiles (crop)", menu, checkable=True)
        self.act_fill.setChecked(self.config.settings.fill_tiles)
        self.act_fill.toggled.connect(self._set_fill)
        menu.addAction(self.act_fill)
        self.btn_layout.setMenu(menu)
        bar.addWidget(self.btn_layout)
        self.btn_full = tool_button("Full screen", "fullscreen", "Full screen video (F11)")
        self.btn_full.clicked.connect(self.toggle_fullscreen)
        bar.addWidget(self.btn_full)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bar.addWidget(spacer)

        pill = QWidget()
        pl = QHBoxLayout(pill)
        pl.setContentsMargins(0, 0, 6, 0)
        pl.setSpacing(4)
        self.pill_dot = StatusDot(8)
        self.pill = QLabel("No cameras")
        pl.addWidget(self.pill_dot)
        pl.addWidget(self.pill)
        bar.addWidget(pill)
        self.btn_theme = flat_button("sun", "Switch between dark and light", size=20)
        self.btn_theme.clicked.connect(self.toggle_theme)
        bar.addWidget(self.btn_theme)
        self.btn_settings = flat_button("settings", "Settings", size=20)
        self.btn_settings.clicked.connect(self.open_settings)
        bar.addWidget(self.btn_settings)
        self.btn_about = flat_button("info", f"About {__app_name__}", size=20)
        self.btn_about.clicked.connect(lambda: AboutDialog(self, self.app_icon).exec())
        bar.addWidget(self.btn_about)
        theme.changed.connect(lambda p: self.btn_theme._binder.set_name("sun" if p.dark else "moon", "text_muted"))
        self.btn_theme._binder.set_name("sun" if theme.palette.dark else "moon", "text_muted")

    # ------------------------------------------------------------------ live page
    def _build_live(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        root = QHBoxLayout(page)
        root.setContentsMargins(12, 12, 12, 12)
        self.live_root = root
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)
        self.live_split = split

        self.side = Card("Cameras", "cctv")
        self.side.setMinimumWidth(220)
        self.side.setMaximumWidth(380)
        add = flat_button("plus", "Add camera")
        add.clicked.connect(self.add_camera)
        reconnect = flat_button("refresh", "Reconnect cameras that are offline")
        reconnect.clicked.connect(self.cameras.connect_all)
        self.side.add_header_widget(reconnect)
        self.side.add_header_widget(add)
        self.camera_list = CameraList(self.cameras, self.recordings)
        self.side.add(self.camera_list, 1)
        split.addWidget(self.side)

        self.center = Card("Live view", "grid")
        self.page_label = QLabel("")
        self.page_label.setObjectName("Muted")
        self.btn_prev_page = flat_button("left", "Previous page")
        self.btn_next_page = flat_button("right", "Next page")
        self.btn_back = flat_button("grid", "Back to all cameras (Esc)")
        for w in (self.btn_back, self.btn_prev_page, self.page_label, self.btn_next_page):
            self.center.add_header_widget(w)
        self.wall = VideoWall(self.cameras, self.recordings, self.config)
        self.center.add(self.wall, 1)
        self.btn_prev_page.clicked.connect(lambda: self.wall.next_page(-1))
        self.btn_next_page.clicked.connect(lambda: self.wall.next_page(1))
        self.btn_back.clicked.connect(lambda: self.wall.set_focus(None))
        split.addWidget(self.center)

        self.right = Card("Controls", "sliders")
        self.right.setMinimumWidth(300)
        self.right.setMaximumWidth(420)
        self.controls = ControlPanel(self.cameras, self.recordings, self.wall)
        self.right.add(self.controls, 1)
        split.addWidget(self.right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([260, 900, 330])

        self.camera_list.selected.connect(self.select_view)
        self.camera_list.activated_view.connect(self.wall.toggle_focus)
        self.camera_list.context.connect(self._camera_context)
        self.wall.selected.connect(self._wall_selected)
        self.wall.focus_changed.connect(self._focus_changed)
        self.wall.page_changed.connect(self._page_changed)
        self.wall.tile_context.connect(self._tile_context)
        self.wall.add_clicked.connect(self.add_camera)
        self.wall.demo_clicked.connect(self.add_demo_cameras)
        self.controls.snapshot_requested.connect(self.snapshot)
        self.controls.record_requested.connect(self.toggle_record)
        self.controls.enlarge_requested.connect(self._toggle_focus_selected)
        self.cameras.cameras_changed.connect(self._ensure_selection)
        return page

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        self.status_text = QLabel("Ready")
        sb.addWidget(self.status_text, 1)
        self.status_right = QLabel(f"{__app_name__} {__version__}")
        self.status_right.setObjectName("Muted")
        sb.addPermanentWidget(self.status_right)

    def _shortcuts(self) -> None:
        def sc(key, fn):
            s = QShortcut(QKeySequence(key), self)
            s.activated.connect(fn)
            return s

        sc("F11", self.toggle_fullscreen)
        sc("Escape", self._escape)
        sc("Ctrl+N", self.add_camera)
        sc("Ctrl+S", self.snapshot)
        sc("Ctrl+R", self.toggle_record)
        sc("Ctrl+1", lambda: self.show_page(0))
        sc("Ctrl+2", lambda: self.show_page(1))
        sc("Ctrl+,", self.open_settings)
        sc("Ctrl+Q", self.close)
        sc("M", self._mute_selected)
        sc("F", self._toggle_focus_selected)

    # ------------------------------------------------------------------ navigation
    def show_page(self, index: int) -> None:
        if index == 1:
            view = self.controls.cam_id and (self.controls.cam_id, self.controls.ch_index)
            self.playback.refresh_cameras()
            if view:
                self.playback.select_view(*view)
            self.playback.activate()
            self.btn_playback.setChecked(True)
        else:
            self.playback.deactivate()
            self.btn_live.setChecked(True)
        self.stack.setCurrentIndex(index)
        live = index == 0
        for b in (self.btn_layout, self.btn_full, self.btn_record, self.btn_snapshot):
            b.setEnabled(live)

    def select_view(self, cam_id: str, channel: int) -> None:
        self.wall.select(cam_id, channel)
        self.controls.set_camera(cam_id, channel)
        self._update_record_button()

    def _wall_selected(self, cam_id: str, channel: int) -> None:
        self.camera_list.select_view(cam_id, channel)
        self.controls.set_camera(cam_id, channel)
        self._update_record_button()

    def _ensure_selection(self) -> None:
        if self.controls.cam_id and self.cameras.entry(self.controls.cam_id):
            self.controls.refresh()
            return
        views = self.cameras.all_views()
        if views:
            self.select_view(views[0][0], views[0][1])
            self.camera_list.select_view(views[0][0], views[0][1])
        else:
            self.controls.set_camera(None, 0)

    def _focus_changed(self, focus) -> None:
        self.btn_back.setVisible(focus is not None)
        self.center.title_label.setText((self.cameras.label(*focus) if focus else "Live view").upper())
        self.controls._update_view_buttons()

    def _page_changed(self, page: int, pages: int) -> None:
        multi = pages > 1 and self.wall.focus is None
        self.page_label.setText(f"{page + 1} / {pages}" if multi else "")
        self.btn_prev_page.setVisible(multi)
        self.btn_next_page.setVisible(multi)

    def set_layout(self, mode: str) -> None:
        self.config.settings.layout = mode
        self.wall.set_layout_mode(mode)

    def _set_fill(self, fill: bool) -> None:
        self.config.settings.fill_tiles = fill
        self.wall.set_fill(fill)

    def _toggle_focus_selected(self) -> None:
        if self.controls.cam_id:
            self.wall.toggle_focus(self.controls.cam_id, self.controls.ch_index)

    def _escape(self) -> None:
        if self._fullscreen:
            self.toggle_fullscreen()
        elif self.wall.focus:
            self.wall.set_focus(None)

    def toggle_fullscreen(self) -> None:
        if self.stack.currentIndex() != 0 and not self._fullscreen:
            return
        self._fullscreen = not self._fullscreen
        full = self._fullscreen
        for w in (self.toolbar, self.statusBar(), self.side, self.right):
            w.setVisible(not full)
        self.live_root.setContentsMargins(*((0, 0, 0, 0) if full else (12, 12, 12, 12)))
        self.center._layout.setContentsMargins(*((0, 0, 0, 0) if full else (14, 12, 14, 14)))
        for i in range(self.center.header.count()):
            w = self.center.header.itemAt(i).widget()
            if w is not None:
                w.setVisible(not full and (w not in (self.btn_back, self.btn_prev_page, self.btn_next_page)
                                           or w.isVisible()))
        self.center.setObjectName("Plain" if full else "Card")
        set_prop(self.center, "dummy", full)
        if full:
            self.showFullScreen()
            self.show_message("Press Esc or F11 to leave full screen", "info")
        else:
            self.showNormal()
            self._focus_changed(self.wall.focus)
            self._page_changed(self.wall.page, self.wall.pages(len(self.wall.visible_sources())))

    # ------------------------------------------------------------------ status
    def show_message(self, text: str, kind: str = "info") -> None:
        self.status_text.setText(text)
        if kind in ("success", "error", "warning") or kind == "info":
            self.toast.show_message(text, kind)

    def _update_status(self) -> None:
        entries = self.cameras.ordered()
        online = sum(1 for e in entries if e.online)
        rec = self.recordings.recording_count()
        pal = theme.palette
        if not entries:
            self.pill.setText("No cameras")
            self.pill_dot.set_color(pal.text_faint, False)
        else:
            text = f"{online} of {len(entries)} online"
            if rec:
                text += f" · {rec} recording"
            self.pill.setText(text)
            color = pal.rec if rec else (pal.success if online == len(entries) else
                                         pal.warning if online else pal.danger)
            self.pill_dot.set_color(color, True)
        dl = self.downloads.active()
        self.status_right.setText((f"{dl} download{'s' if dl != 1 else ''} in progress · " if dl else "")
                                  + f"{__app_name__} {__version__}")

    def _update_record_button(self) -> None:
        view = f"{self.controls.cam_id}/{self.controls.ch_index}/"
        recording = bool(self.controls.cam_id) and any(
            k.startswith(view) for k in list(self.recordings.manual) + list(self.recordings.fallback))
        self.btn_record.setChecked(recording)
        self.btn_record.setText("Stop" if recording else "Record")
        set_prop(self.btn_record, "recording", recording)
        set_prop(self.btn_record, "accent", not recording)

    # ------------------------------------------------------------------ actions
    def add_camera(self) -> None:
        dlg = CameraDialog(self)
        if not dlg.exec():
            return
        if getattr(dlg, "wants_demo", False):
            self.add_demo_cameras()
            return
        if getattr(dlg, "cfg_result", None):
            cfg, password = dlg.cfg_result, dlg.password_result or ""
            self.cameras.add_camera(cfg, password)
            self.show_message(f"Added {cfg.label}", "success")

    def add_demo_cameras(self) -> None:
        existing = {c.demo for c in self.config.cameras if c.demo}
        added = 0
        for kind, name in (("duo2", "Driveway"), ("trackmix", "Back Garden"), ("rlc811a", "Front Door")):
            if kind in existing:
                continue
            self.cameras.add_camera(CameraConfig(name=name, host=f"demo:{kind}", demo=kind), "")
            added += 1
        if added:
            self.show_message("Added demo cameras: a Duo 2, a TrackMix and an RLC-811A (simulated)", "success")

    def edit_camera(self, cam_id: str) -> None:
        e = self.cameras.entry(cam_id)
        if not e:
            return
        dlg = CameraDialog(self, e.cfg, has_password=bool(self.config.password(e.cfg)))
        if dlg.exec() and getattr(dlg, "cfg_result", None):
            self.cameras.update_camera(dlg.cfg_result, dlg.password_result)
            self.recordings.sync_continuous()

    def remove_camera(self, cam_id: str) -> None:
        e = self.cameras.entry(cam_id)
        if not e:
            return
        if QMessageBox.question(self, "Remove camera", f"Remove {e.cfg.label} from {__app_name__}?\n\n"
                                "Nothing on the camera is changed; files saved on this PC are kept.") \
                != QMessageBox.Yes:
            return
        for key in [k for k in list(self.recordings.manual) + list(self.recordings.fallback) if k.startswith(cam_id)]:
            self.recordings.stop(key)
        self.cameras.remove_camera(cam_id)
        self.recordings.sync_continuous()

    def _selected_sources(self) -> list[Source]:
        if not self.controls.cam_id:
            return []
        tiles = self.wall.tiles_for_view(self.controls.cam_id, self.controls.ch_index)
        if tiles:
            return [t.source for t in tiles]
        return [Source(self.controls.cam_id, self.controls.ch_index)]

    def snapshot(self) -> None:
        sources = self._selected_sources()
        if not sources:
            self.show_message("Select a camera first", "warning")
            return
        for src in sources:
            self._snapshot_one(src)

    def _snapshot_one(self, src: Source) -> None:
        label = self.cameras.label(src.cam_id, src.channel) + (" tele" if src.lens == TELE else "")
        folder = self.config.settings.pictures() / rec_mod.safe_name(self.cameras.label(src.cam_id, src.channel))
        path = folder / f"{rec_mod.safe_name(label)}_{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.jpg"
        tile = self.wall.tiles.get(src.key)

        def save(data: bytes) -> None:
            folder.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.show_message(f"Saved {path.name}", "success")

        def fallback(exc: Exception) -> None:
            folder.mkdir(parents=True, exist_ok=True)
            if tile and tile.video.screenshot(str(path)):
                self.show_message(f"Saved {path.name} (from the video)", "success")
            else:
                self.show_message(f"Snapshot failed: {exc}", "error")

        self.cameras.call(src.cam_id, lambda d: d.snapshot(src.channel, src.lens), save, fallback)

    def toggle_record(self) -> None:
        sources = self._selected_sources()
        if not sources:
            self.show_message("Select a camera first", "warning")
            return
        view = f"{self.controls.cam_id}/{self.controls.ch_index}/"
        active = [k for k in list(self.recordings.manual) + list(self.recordings.fallback) if k.startswith(view)]
        if active:
            # Stop every lens of this camera, even one no longer on screen.
            for key in active:
                self.recordings.stop(key)
        else:
            for s in sources:
                tile = self.wall.tiles.get(s.key)
                self.recordings.start(s, tile.video if tile else None)
        QTimer.singleShot(200, self._update_record_button)

    def _mute_selected(self) -> None:
        for s in self._selected_sources():
            muted = self.wall.toggle_mute(s)
            self.show_message("Sound off" if muted else "Sound on", "info")
        self.controls._update_view_buttons()

    def toggle_theme(self) -> None:
        mode = theme.toggle(QApplication.instance())
        self.config.settings.theme = mode
        self.config.save()

    def open_settings(self) -> None:
        old_theme = self.config.settings.theme
        dlg = SettingsDialog(self.config, self)
        if not dlg.exec():
            return
        s = self.config.settings
        if s.theme != old_theme:
            theme.apply(QApplication.instance(), s.theme)
        self.act_fill.setChecked(s.fill_tiles)
        if getattr(dlg, "video_changed", False):
            self.wall.apply_settings()
        else:
            self.wall.set_fill(s.fill_tiles)
            self.wall.relayout()
        self.recordings.sync_continuous()
        self.show_message("Settings saved", "success")

    # ------------------------------------------------------------------ context menus
    def _tile_context(self, source: Source, pos) -> None:
        menu = QMenu(self)
        focus = self.wall.focus == (source.cam_id, source.channel)
        menu.addAction("Back to all cameras" if focus else "Enlarge",
                       lambda: self.wall.toggle_focus(source.cam_id, source.channel))
        quality = self.wall.tile_quality.get(source.key, self.wall.quality_for(source))
        qm = menu.addMenu("Quality")
        for label, value in (("Clear (full resolution)", MAIN), ("Fluent (low bandwidth)", SUB)):
            act = qm.addAction(label, lambda v=value: self.wall.set_quality(source, v))
            act.setCheckable(True)
            act.setChecked(quality == value)
        ch = self.cameras.channel(source.cam_id, source.channel)
        if ch and ch.caps.telephoto:
            lm = menu.addMenu("Lens")
            mode = self.wall.lens_mode(source.cam_id, source.channel)
            for label, value in (("Wide", "wide"), ("Telephoto", "tele"), ("Both", "both")):
                act = lm.addAction(label, lambda v=value: self.wall.set_lens_mode(source.cam_id, source.channel, v))
                act.setCheckable(True)
                act.setChecked(mode == value)
        menu.addSeparator()
        menu.addAction("Snapshot", lambda: self._snapshot_one(source))
        rec = self.recordings.is_recording(source.key)
        tile = self.wall.tiles.get(source.key)
        menu.addAction("Stop recording" if rec else "Record to this PC",
                       lambda: self.recordings.toggle(source, tile.video if tile else None))
        muted = tile.video.muted if tile else True
        menu.addAction("Sound on" if muted else "Sound off",
                       lambda: (self.wall.toggle_mute(source), self.controls._update_view_buttons()))
        if tile and tile.video.zoom > 1.0:
            menu.addAction("Reset zoom", tile.video.reset_zoom)
        menu.addSeparator()
        menu.addAction("Reconnect", lambda: self.wall.reload(source))
        menu.addAction("Recordings on the camera…", lambda: (self.select_view(source.cam_id, source.channel),
                                                             self.show_page(1)))
        menu.addAction("Edit camera…", lambda: self.edit_camera(source.cam_id))
        menu.exec(pos)

    def _camera_context(self, cam_id: str, channel: int, pos) -> None:
        e = self.cameras.entry(cam_id)
        if not e:
            return
        menu = QMenu(self)
        if e.online:
            menu.addAction("Disconnect", lambda: self.cameras.disconnect_camera(cam_id))
            menu.addAction("Reconnect", lambda: (self.cameras.disconnect_camera(cam_id),
                                                 self.cameras.connect_camera(cam_id)))
        else:
            menu.addAction("Connect", lambda: self.cameras.connect_camera(cam_id))
        if channel >= 0 and e.online:
            menu.addAction("Enlarge", lambda: self.wall.toggle_focus(cam_id, channel))
        menu.addSeparator()
        cont = menu.addAction("Record continuously to this PC")
        cont.setCheckable(True)
        cont.setChecked(e.cfg.continuous_record)
        cont.toggled.connect(lambda on: self._set_continuous(cam_id, on))
        folder = self.config.settings.videos() / rec_mod.safe_name(e.cfg.label)
        menu.addAction("Open recordings folder", lambda: (folder.mkdir(parents=True, exist_ok=True),
                                                          QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))))
        menu.addSeparator()
        menu.addAction("Move up", lambda: self.cameras.move_camera(cam_id, -1))
        menu.addAction("Move down", lambda: self.cameras.move_camera(cam_id, 1))
        menu.addAction("Edit…", lambda: self.edit_camera(cam_id))
        menu.addAction("Remove…", lambda: self.remove_camera(cam_id))
        menu.exec(pos)

    def _set_continuous(self, cam_id: str, on: bool) -> None:
        e = self.cameras.entry(cam_id)
        if not e:
            return
        if on and not rec_mod.ffmpeg_path():
            self.show_message("Continuous recording needs FFmpeg: install the 'ffmpeg' package", "error")
            return
        e.cfg.continuous_record = on
        self.config.save()
        self.recordings.sync_continuous()
        folder = self.config.settings.videos() / rec_mod.safe_name(e.cfg.label) / "Continuous"
        self.show_message(f"Continuous recording {'on' if on else 'off'} for {e.cfg.label}"
                          + (f" → {folder}" if on else ""), "success" if on else "info")

    # ------------------------------------------------------------------ keyboard PTZ
    def _ptz_target(self):
        if self.stack.currentIndex() != 0 or not self.controls.cam_id:
            return None
        ch = self.cameras.channel(self.controls.cam_id, self.controls.ch_index)
        if not ch or not (ch.caps.pan_tilt or ch.caps.optical_zoom):
            return None
        return ch

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Arrow keys and +/- drive a PTZ camera on the live page, whatever has focus
        (the camera list would otherwise use the arrows to move its selection)."""
        etype = event.type()
        if etype not in (QEvent.KeyPress, QEvent.KeyRelease) or not self.isActiveWindow():
            return False
        if event.key() not in PTZ_KEYS or event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            return False
        if isinstance(QApplication.focusWidget(), (QLineEdit, QAbstractSpinBox)):
            return False
        ch = self._ptz_target()
        if ch is None:
            return False
        op = PTZ_KEYS[event.key()]
        if op.startswith("Zoom") and not ch.caps.optical_zoom or not op.startswith("Zoom") and not ch.caps.pan_tilt:
            return False
        if event.isAutoRepeat():
            return True
        cam_id, index = self.controls.cam_id, ch.index
        if etype == QEvent.KeyPress:
            self._ptz_key = event.key()
            speed = self.config.settings.ptz_speed
            self.cameras.control(cam_id, lambda d: d.ptz(index, op, speed=speed), what="PTZ")
        elif event.key() == self._ptz_key:
            self._ptz_key = None
            self.cameras.control(cam_id, lambda d: d.ptz_stop(index), what="PTZ")
        return True

    # ------------------------------------------------------------------ persistence
    def _restore(self) -> None:
        w = self.config.window
        try:
            if w.get("geometry"):
                self.restoreGeometry(QByteArray(base64.b64decode(w["geometry"])))
            if w.get("split"):
                self.live_split.setSizes([int(x) for x in w["split"]])
        except (ValueError, TypeError):
            pass
        mode = self.config.settings.layout if self.config.settings.remember_layout else "auto"
        if mode in self.layout_actions:
            self.layout_actions[mode].setChecked(True)

    def closeEvent(self, event) -> None:  # noqa: N802
        manual = len(self.recordings.manual) + len(self.recordings.fallback)
        if manual and not self._closing:
            answer = QMessageBox.question(self, "Recording in progress",
                                          f"{manual} recording{'s are' if manual != 1 else ' is'} still running. "
                                          "Stop and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self._closing = True
        QApplication.instance().removeEventFilter(self)
        self.config.window = {
            "geometry": base64.b64encode(bytes(self.saveGeometry())).decode(),
            "split": self.live_split.sizes(),
        }
        self.config.save()
        self.recordings.stop_all()
        self.wall.shutdown()
        self.playback.shutdown()
        self.cameras.shutdown()
        worker.wait_all(2000)
        super().closeEvent(event)

