"""The live view: camera list and the video wall."""

from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.config import Config
from ..core.models import MAIN, SUB, TELE, WIDE
from . import worker
from .manager import CameraManager, RecordingManager, Source
from .theme import theme
from .video import VideoTile
from .widgets import StatusDot, bind_icon, scaled_font

QUALITY_LABEL = {MAIN: "CLEAR", SUB: "FLUENT"}
RETRY_DELAYS = (3, 5, 10, 20, 30)


# ---------------------------------------------------------------------- camera list
class CameraRow(QWidget):
    def __init__(self, title: str, subtitle: str, indent: bool = False):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18 if indent else 4, 5, 6, 5)
        lay.setSpacing(8)
        self.dot = StatusDot(8)
        lay.addWidget(self.dot, 0, Qt.AlignVCenter)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.title = QLabel(title)
        self.title.setObjectName("Value")
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("Muted")
        self.subtitle.setFont(scaled_font(self.subtitle, 0.86))
        text.addWidget(self.title)
        text.addWidget(self.subtitle)
        lay.addLayout(text, 1)
        self.rec = QLabel()
        self.rec.setToolTip("Recording to this PC")
        self.rec_binder = bind_icon(self.rec, "record", "rec", 14)
        self.rec.hide()
        lay.addWidget(self.rec, 0, Qt.AlignVCenter)
        self.det = QLabel()
        self.det_binder = bind_icon(self.det, "person", "person", 14)
        self.det.hide()
        lay.addWidget(self.det, 0, Qt.AlignVCenter)

    def set_state(self, status: str, subtitle: str, recording: bool = False, detection: str = "") -> None:
        pal = theme.palette
        color = {"online": pal.success, "connecting": pal.warning, "error": pal.danger}.get(status, pal.text_faint)
        self.dot.set_color(color, halo=status == "online")
        self.subtitle.setText(subtitle)
        self.subtitle.setToolTip(subtitle)
        self.rec.setVisible(recording)
        if detection:
            icon = {"people": "person", "vehicle": "car", "dog_cat": "paw", "face": "person"}.get(detection, "motion")
            token = {"people": "person", "vehicle": "vehicle", "dog_cat": "animal", "face": "person"}.get(detection, "motion")
            self.det_binder.set_name(icon, token)
        self.det.setVisible(bool(detection))


class CameraList(QListWidget):
    selected = Signal(str, int)
    activated_view = Signal(str, int)
    context = Signal(str, int, QPoint)

    def __init__(self, cameras: CameraManager, recordings: RecordingManager):
        super().__init__()
        self.cameras = cameras
        self.recordings = recordings
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.setSelectionMode(QListWidget.SingleSelection)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context)
        self.itemSelectionChanged.connect(self._on_select)
        self.itemDoubleClicked.connect(self._on_double)
        self._rows: dict[tuple[str, int | None], tuple[QListWidgetItem, CameraRow]] = {}
        self._suppress = False
        cameras.cameras_changed.connect(self.rebuild)
        cameras.camera_changed.connect(lambda _c: self.refresh())
        cameras.detection_changed.connect(lambda _c: self.refresh())
        recordings.changed.connect(lambda _k: self.refresh())
        theme.changed.connect(lambda _p: self.refresh())
        self.rebuild()

    def rebuild(self) -> None:
        current = self.current_view()
        self._suppress = True
        self.clear()
        self._rows.clear()
        for e in self.cameras.ordered():
            dev = e.device if e.online else None
            if dev and dev.is_nvr:
                self._add((e.cfg.id, None), e.cfg.label, "", False)
                for ch in dev.channels:
                    if ch.index in e.cfg.hidden_channels:
                        continue
                    self._add((e.cfg.id, ch.index), ch.name or f"Channel {ch.index + 1}", "", True)
            else:
                self._add((e.cfg.id, 0), e.cfg.label, "", False)
        self._suppress = False
        self.refresh()
        if current:
            self.select_view(*current)

    def _add(self, key, title: str, subtitle: str, indent: bool) -> None:
        item = QListWidgetItem(self)
        row = CameraRow(title, subtitle, indent)
        item.setSizeHint(QSize(100, row.sizeHint().height() + 2))
        item.setData(Qt.UserRole, key)
        if key[1] is None:
            item.setFlags(item.flags() & ~Qt.ItemIsSelectable)
        self.setItemWidget(item, row)
        self._rows[key] = (item, row)

    def refresh(self) -> None:
        for (cam_id, ch_index), (_item, row) in self._rows.items():
            e = self.cameras.entry(cam_id)
            if not e:
                continue
            row.title.setText(self.cameras.label(cam_id, ch_index) if ch_index is not None and e.online and e.device.is_nvr
                              else e.cfg.label)
            if e.status == "online":
                dev = e.device
                if ch_index is None:
                    sub = f"{dev.info.model or 'NVR'} · {len(dev.channels)} channels"
                    status = "online"
                else:
                    ch = next((c for c in dev.channels if c.index == ch_index), None)
                    if dev.is_nvr:
                        sub = (ch.model if ch and ch.model else "Camera") if ch and ch.online else "Offline"
                    else:
                        sub = f"{dev.info.model} · {e.cfg.host if not e.cfg.demo else 'demo'}"
                    status = "online" if ch is None or ch.online else "offline"
            else:
                status = e.status
                sub = {"connecting": "Connecting…", "offline": "Disconnected"}.get(e.status, e.error or "Error")
            rec = False
            det = ""
            if ch_index is not None:
                rec = any(self.recordings.is_recording(Source(cam_id, ch_index, lens).key)
                          or self.recordings.is_continuous(Source(cam_id, ch_index, lens).key) for lens in (WIDE, TELE))
                ch = self.cameras.channel(cam_id, ch_index)
                if ch and self.cameras.config.settings.show_detection:
                    active = ch.detection.active
                    det = active[0] if active else ""
            row.set_state(status, sub, rec, det)

    def current_view(self) -> tuple[str, int] | None:
        items = self.selectedItems()
        if not items:
            return None
        key = items[0].data(Qt.UserRole)
        return (key[0], key[1]) if key and key[1] is not None else None

    def select_view(self, cam_id: str, channel: int) -> None:
        pair = self._rows.get((cam_id, channel))
        if pair:
            self._suppress = True
            self.setCurrentItem(pair[0])
            self._suppress = False

    def _on_select(self) -> None:
        if self._suppress:
            return
        view = self.current_view()
        if view:
            self.selected.emit(*view)

    def _on_double(self, item: QListWidgetItem) -> None:
        key = item.data(Qt.UserRole)
        if key and key[1] is not None:
            self.activated_view.emit(key[0], key[1])

    def _context(self, pos: QPoint) -> None:
        item = self.itemAt(pos)
        if not item:
            return
        key = item.data(Qt.UserRole)
        self.context.emit(key[0], key[1] if key[1] is not None else -1, self.viewport().mapToGlobal(pos))


# ---------------------------------------------------------------------- video wall
class EmptyState(QWidget):
    add_clicked = Signal()
    demo_clicked = Signal()

    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        icon = QLabel()
        icon.setAlignment(Qt.AlignCenter)
        self._icon = bind_icon(icon, "cctv", "text_faint", 56)
        title = QLabel("No cameras yet")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(scaled_font(title, 1.35, bold=True))
        title.setStyleSheet("color: #e8ecf2;")
        text = QLabel("Add a Reolink camera, NVR or Home Hub by its IP address, or let ReolinkLinux\n"
                      "search your network. You can also try the app with simulated cameras.")
        text.setAlignment(Qt.AlignCenter)
        text.setStyleSheet("color: #9aa4b4;")
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        add = QPushButton("Add camera")
        add.setProperty("variant", "primary")
        add.setCursor(Qt.PointingHandCursor)
        add.clicked.connect(self.add_clicked.emit)
        demo = QPushButton("Try demo cameras")
        demo.setCursor(Qt.PointingHandCursor)
        demo.clicked.connect(self.demo_clicked.emit)
        buttons.addWidget(add)
        buttons.addWidget(demo)
        buttons.addStretch(1)
        lay.addWidget(icon)
        lay.addSpacing(6)
        lay.addWidget(title)
        lay.addWidget(text)
        lay.addSpacing(10)
        lay.addLayout(buttons)
        lay.addStretch(1)


class VideoWall(QFrame):
    """All live tiles, in a grid or with one camera enlarged."""

    selected = Signal(str, int)
    focus_changed = Signal(object)          # (cam_id, channel) or None
    tile_context = Signal(object, QPoint)   # Source, global pos
    page_changed = Signal(int, int)         # page, pages
    add_clicked = Signal()
    demo_clicked = Signal()

    def __init__(self, cameras: CameraManager, recordings: RecordingManager, config: Config):
        super().__init__()
        self.setObjectName("VideoWall")
        self.cameras = cameras
        self.recordings = recordings
        self.config = config
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(6, 6, 6, 6)
        self.grid.setSpacing(6)
        self.tiles: dict[str, VideoTile] = {}
        self.tile_quality: dict[str, str] = {}
        self.quality_override: dict[str, str] = {}
        self.lens_pref: dict[str, str] = {}        # view_key -> "wide" | "tele" | "both"
        self.focus: tuple[str, int] | None = None
        self.selection: tuple[str, int] | None = None
        self.layout_mode = config.settings.layout if config.settings.remember_layout else "auto"
        self.page = 0
        self.fill = config.settings.fill_tiles
        self.user_muted: dict[str, bool] = {}
        self._retry: dict[str, int] = {}
        self._retry_timers: dict[str, QTimer] = {}
        self.empty = EmptyState()
        self.empty.add_clicked.connect(self.add_clicked.emit)
        self.empty.demo_clicked.connect(self.demo_clicked.emit)
        self.empty.setParent(self)
        self.empty.hide()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        cameras.cameras_changed.connect(self.relayout)
        cameras.detection_changed.connect(self._update_overlays)
        recordings.changed.connect(lambda _k: self._update_overlays())
        self._clock = QTimer(self)
        self._clock.timeout.connect(self._update_overlays)
        self._clock.start(1000)

    # ------------------------------------------------------------------ sources
    def _lenses(self, cam_id: str, ch_index: int) -> list[int]:
        ch = self.cameras.channel(cam_id, ch_index)
        if ch is None:
            return [WIDE]
        mode = self.lens_mode(cam_id, ch_index)
        lenses = {"wide": [WIDE], "tele": [TELE], "both": [WIDE, TELE]}.get(mode, [WIDE])
        return [lens for lens in lenses if lens in ch.lenses] or [WIDE]

    def visible_sources(self) -> list[Source]:
        if self.focus:
            cam_id, ch_index = self.focus
            if self.cameras.channel(cam_id, ch_index) is None:
                self.focus = None
                return self.visible_sources()
            return [Source(cam_id, ch_index, lens) for lens in self._lenses(cam_id, ch_index)]
        return [Source(s.cam_id, s.channel, lens) for s in self.cameras.sources()
                for lens in self._lenses(s.cam_id, s.channel)]

    def page_size(self) -> int:
        return {"1": 1, "4": 4, "9": 9, "16": 16}.get(self.layout_mode, 0)

    def pages(self, total: int) -> int:
        size = self.page_size()
        return max(1, math.ceil(total / size)) if size else 1

    def quality_for(self, source: Source) -> str:
        if source.key in self.quality_override:
            return self.quality_override[source.key]
        s = self.config.settings
        shown = len(self.visible_sources())
        if self.focus or shown == 1:
            return s.focus_quality
        if s.grid_quality == "auto":
            # Full quality while the videos are big enough to show it.
            return MAIN if shown <= 4 else SUB
        return s.grid_quality

    # ------------------------------------------------------------------ layout
    def relayout(self) -> None:
        all_sources = self.visible_sources()
        total = len(all_sources)
        if self.focus:
            shown = all_sources
            pages = 1
        else:
            size = self.page_size()
            pages = self.pages(total)
            self.page = min(self.page, pages - 1)
            shown = all_sources[self.page * size:(self.page + 1) * size] if size else all_sources
        self.page_changed.emit(self.page, pages)
        shown_keys = {s.key for s in shown}
        existing_sources = {s.key for s in self._all_possible_sources()}

        # remove tiles whose source is gone for good; stop hidden ones
        for key, tile in list(self.tiles.items()):
            self.grid.removeWidget(tile)
            if key not in existing_sources:
                self._drop_tile(key)
            elif key not in shown_keys:
                tile.hide()
                if tile.video.playing:
                    tile.video.stop()
                self._cancel_retry(key)

        no_cameras = not self.cameras.ordered()
        self.empty.setVisible(no_cameras)
        if no_cameras:
            self.empty.setGeometry(self.rect())
            self.focus_changed.emit(self.focus)
            return

        n = len(shown)
        if n == 0:
            self.focus_changed.emit(self.focus)
            return
        cols, rows = self._grid_shape(n, shown)
        for i, source in enumerate(shown):
            tile = self.tiles.get(source.key) or self._make_tile(source)
            r, c = divmod(i, cols)
            self.grid.addWidget(tile, r, c)
            tile.show()
            self._ensure_playing(source, tile)
        for c in range(4):
            self.grid.setColumnStretch(c, 1 if c < cols else 0)
        for r in range(4):
            self.grid.setRowStretch(r, 1 if r < rows else 0)
        self._update_overlays()
        self.focus_changed.emit(self.focus)

    def _grid_shape(self, n: int, sources: list[Source] | None = None) -> tuple[int, int]:
        fixed = 0 if self.focus else self.page_size()
        if fixed:
            side = int(math.sqrt(fixed))
            return side, side
        if n <= 1:
            return 1, 1
        # Pick the column count that shows the videos largest for their aspect ratio.
        aspect = self._aspect(sources or [])
        width = max(1, self.width() - 12)
        height = max(1, self.height() - 12)
        best, best_area = (n, 1), -1.0
        # One or two cameras: whatever shows them largest (a Duo 2 panorama stacks nicely).
        # Three or more: a familiar near-square grid, oriented to suit the window.
        side = math.ceil(math.sqrt(n))
        choices = range(1, n + 1) if n <= 2 else sorted({side, max(1, side - 1), side + 1})
        for cols in choices:
            if n > 2 and math.ceil(n / cols) > cols + 1:
                continue
            rows = math.ceil(n / cols)
            cw, chh = width / cols, height / rows
            vw = min(cw, chh * aspect)
            area = vw * (vw / aspect) * n
            if area > best_area + 1:
                best, best_area = (cols, rows), area
        return best

    def _aspect(self, sources: list[Source]) -> float:
        ratios = []
        for s in sources:
            ch = self.cameras.channel(s.cam_id, s.channel)
            info = ch.main if ch else None
            if info and info.width and info.height and s.lens != TELE:
                ratios.append(info.width / info.height)
            else:
                ratios.append(16 / 9)
        return sum(ratios) / len(ratios) if ratios else 16 / 9

    def _all_possible_sources(self) -> list[Source]:
        out = []
        for s in self.cameras.sources():
            ch = self.cameras.channel(s.cam_id, s.channel)
            for lens in (ch.lenses if ch else [WIDE]):
                out.append(Source(s.cam_id, s.channel, lens))
        return out

    def _make_tile(self, source: Source) -> VideoTile:
        s = self.config.settings
        tile = VideoTile(self, live=True, hwdec=self.cameras.hwdec(source.cam_id), low_latency=s.low_latency)
        tile.source = source
        tile.video.set_fill(self.fill)
        tile.video.clicked.connect(lambda src=source: self._tile_clicked(src))
        tile.video.double_clicked.connect(lambda src=source: self.toggle_focus(src.cam_id, src.channel))
        tile.video.context_requested.connect(lambda pos, src=source: self._tile_context(src, pos))
        tile.video.started.connect(lambda src=source: self._tile_started(src))
        tile.video.failed.connect(lambda msg, src=source: self._tile_failed(src, msg))
        tile.retry_requested.connect(lambda src=source: self._retry_now(src))
        self.tiles[source.key] = tile
        if not tile.video.available:
            tile.set_status(tile.video.error or "libmpv is not available", "error")
        return tile

    def _drop_tile(self, key: str) -> None:
        tile = self.tiles.pop(key, None)
        self._cancel_retry(key)
        self.tile_quality.pop(key, None)
        if tile:
            tile.video.shutdown()
            tile.hide()
            tile.deleteLater()

    def shutdown(self) -> None:
        for key in list(self.tiles):
            self._drop_tile(key)

    # ------------------------------------------------------------------ playback
    def _ensure_playing(self, source: Source, tile: VideoTile, force: bool = False) -> None:
        quality = self.quality_for(source)
        tile.video.set_muted(self._muted(source))
        if not force and tile.video.playing and self.tile_quality.get(source.key) == quality:
            return
        dev = self.cameras.device(source.cam_id)
        if not dev or not tile.video.available:
            return
        self.tile_quality[source.key] = quality
        protocol = self.config.settings.protocol
        first = dev.stream_url(source.channel, source.lens, quality, protocol)
        urls = [first] + [u for u in dev.stream_candidates(source.channel, source.lens, quality, protocol) if u != first]
        tile.set_status("Connecting…", "busy")
        tile.video.play(urls)

    def _muted(self, source: Source) -> bool:
        if source.key in self.user_muted:
            return self.user_muted[source.key]
        if self.focus:
            return source.lens == TELE      # one audio track is enough in dual-lens view
        return not self.config.settings.grid_audio

    def _tile_started(self, source: Source) -> None:
        tile = self.tiles.get(source.key)
        if tile:
            tile.clear_status()
        self._retry.pop(source.key, None)

    def _tile_failed(self, source: Source, message: str) -> None:
        tile = self.tiles.get(source.key)
        if not tile or not tile.isVisible():
            return
        attempt = self._retry.get(source.key, 0)
        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
        self._retry[source.key] = attempt + 1
        tile.set_status(f"{message}\nReconnecting in {delay} s…", "error", retry=True)
        self._cancel_retry(source.key)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda src=source: self._retry_now(src))
        timer.start(delay * 1000)
        self._retry_timers[source.key] = timer

    def _cancel_retry(self, key: str) -> None:
        t = self._retry_timers.pop(key, None)
        if t:
            t.stop()
            t.deleteLater()

    def _retry_now(self, source: Source) -> None:
        self._cancel_retry(source.key)
        tile = self.tiles.get(source.key)
        dev = self.cameras.device(source.cam_id)
        if not tile or not tile.isVisible():
            return
        if not dev:
            tile.set_status("The camera is offline", "error", retry=True)
            return
        quality = self.quality_for(source)
        protocol = self.config.settings.protocol
        tile.set_status("Connecting…", "busy")

        def probe():
            dev.forget_stream(source.channel, source.lens, quality, protocol)
            return dev.probe_stream(source.channel, source.lens, quality, protocol)

        worker.run(probe, lambda _u, src=source: self._replay(src), lambda _e, src=source: self._replay(src))

    def _replay(self, source: Source) -> None:
        tile = self.tiles.get(source.key)
        if tile and tile.isVisible():
            self._ensure_playing(source, tile, force=True)

    def reload(self, source: Source | None = None) -> None:
        for key, tile in self.tiles.items():
            if tile.isVisible() and (source is None or key == source.key):
                self._retry.pop(key, None)
                self._ensure_playing(tile.source, tile, force=True)

    # ------------------------------------------------------------------ interaction
    def _tile_clicked(self, source: Source) -> None:
        self.selection = (source.cam_id, source.channel)
        self._update_overlays()
        self.selected.emit(source.cam_id, source.channel)

    def select(self, cam_id: str, channel: int) -> None:
        self.selection = (cam_id, channel)
        self._update_overlays()

    def _tile_context(self, source: Source, pos: QPoint) -> None:
        self._tile_clicked(source)
        self.tile_context.emit(source, pos)

    def toggle_focus(self, cam_id: str, channel: int) -> None:
        if self.focus == (cam_id, channel):
            self.focus = None
        else:
            self.focus = (cam_id, channel)
        self.selection = (cam_id, channel)
        self.relayout()
        self.selected.emit(cam_id, channel)

    def set_focus(self, view: tuple[str, int] | None) -> None:
        if self.focus != view:
            self.focus = view
            self.relayout()

    def set_layout_mode(self, mode: str) -> None:
        self.layout_mode = mode
        self.page = 0
        self.focus = None
        self.relayout()

    def next_page(self, step: int) -> None:
        total = len(self.visible_sources())
        self.page = (self.page + step) % self.pages(total)
        self.relayout()

    def set_lens_mode(self, cam_id: str, channel: int, mode: str) -> None:
        self.lens_pref[f"{cam_id}/{channel}"] = mode
        self.relayout()

    def lens_mode(self, cam_id: str, channel: int) -> str:
        mode = self.lens_pref.get(f"{cam_id}/{channel}")
        if mode:
            return mode
        ch = self.cameras.channel(cam_id, channel)
        if ch and ch.caps.telephoto and (self.focus == (cam_id, channel) or self.config.settings.both_lenses):
            return "both"
        return "wide"

    def set_quality(self, source: Source, quality: str) -> None:
        self.quality_override[source.key] = quality
        tile = self.tiles.get(source.key)
        if tile and tile.isVisible():
            self._ensure_playing(source, tile, force=True)
        self._update_overlays()

    def set_view_quality(self, cam_id: str, channel: int, quality: str) -> None:
        for s in self.visible_sources():
            if (s.cam_id, s.channel) == (cam_id, channel):
                self.set_quality(s, quality)

    def set_fill(self, fill: bool) -> None:
        self.fill = fill
        for tile in self.tiles.values():
            tile.video.set_fill(fill)

    def toggle_mute(self, source: Source) -> bool:
        tile = self.tiles.get(source.key)
        muted = not (tile.video.muted if tile else True)
        self.user_muted[source.key] = muted
        if tile:
            tile.video.set_muted(muted)
        return muted

    def tiles_for_view(self, cam_id: str, channel: int) -> list[VideoTile]:
        return [t for t in self.tiles.values() if t.isVisible() and (t.source.cam_id, t.source.channel) == (cam_id, channel)]

    def primary_tile(self, cam_id: str, channel: int) -> VideoTile | None:
        tiles = self.tiles_for_view(cam_id, channel)
        return tiles[0] if tiles else None

    def apply_settings(self, cam_id: str | None = None) -> None:
        """Recreate players after a playback setting (hwdec, latency, protocol) changed."""
        for key in list(self.tiles):
            if cam_id is None or key.startswith(cam_id + "/"):
                self._drop_tile(key)
        self.fill = self.config.settings.fill_tiles
        self.relayout()

    # ------------------------------------------------------------------ overlays
    def _update_overlays(self, *_args) -> None:
        show_det = self.config.settings.show_detection
        for key, tile in self.tiles.items():
            if not tile.isVisible():
                continue
            s = tile.source
            ov = tile.overlay
            label = self.cameras.label(s.cam_id, s.channel)
            ov.title = label + ("  ·  Tele" if s.lens == TELE else "")
            ov.badge = QUALITY_LABEL.get(self.tile_quality.get(key, ""), "")
            rec = self.recordings.is_recording(key)
            cont = self.recordings.is_continuous(key)
            ov.recording = rec or cont
            ov.record_started = self.recordings.elapsed_start(key) if rec else 0.0
            ch = self.cameras.channel(s.cam_id, s.channel)
            ov.detections = ch.detection.active if (ch and show_det) else []
            ov.selected = (not self.focus and self.selection == (s.cam_id, s.channel)
                           and len(self.visible_sources()) > 1)
            ov.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.empty.setGeometry(self.rect())
