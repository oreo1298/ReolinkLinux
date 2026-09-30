"""Playback of SD-card / NVR recordings: calendar, event timeline, clip list, downloads."""

from __future__ import annotations

import datetime as dt
import subprocess
from pathlib import Path

from PySide6.QtCore import QDate, QPointF, QRectF, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDesktopServices,
    QFont,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QShortcut,
    QTextCharFormat,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCalendarWidget,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.config import Config
from ..core.models import MAIN, SUB, TELE, WIDE, Recording, Trigger
from ..core.recorder import safe_name
from . import icons, worker
from .manager import CameraManager, DownloadJob, DownloadManager
from .theme import theme
from .video import VideoTile
from .widgets import Card, SegmentedControl, bind_icon, chip, flat_button, human_duration, human_size, scaled_font

DAY = 86400.0


def trigger_style(trig: Trigger) -> tuple[str, str]:
    """(icon, palette token) for the most important trigger of a clip."""
    if trig & Trigger.PERSON or trig & Trigger.FACE:
        return "person", "person"
    if trig & Trigger.VEHICLE:
        return "car", "vehicle"
    if trig & Trigger.ANIMAL:
        return "paw", "animal"
    if trig & Trigger.PACKAGE or trig & Trigger.DOORBELL:
        return "bookmark", "warning"
    if trig & Trigger.MOTION:
        return "motion", "motion"
    if trig & Trigger.TIMER:
        return "clock", "timer"
    return "film", "accent"


class Timeline(QWidget):
    """24-hour strip of recordings, coloured by event; click to play from that moment."""

    seek_requested = Signal(object)     # datetime

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.day = dt.date.today()
        self.recordings: list[Recording] = []
        self.playhead: dt.datetime | None = None
        self.view = (0.0, DAY)
        self._hover: float | None = None
        self._drag_x: float | None = None
        self._dragged = False
        theme.changed.connect(lambda _p: self.update())

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(600, 68)

    def set_day(self, day: dt.date, recordings: list[Recording]) -> None:
        self.day = day
        self.recordings = recordings
        self.update()

    def set_playhead(self, moment: dt.datetime | None) -> None:
        self.playhead = moment
        self.update()

    def _secs(self, moment: dt.datetime) -> float:
        return (moment - dt.datetime.combine(self.day, dt.time())).total_seconds()

    def _x(self, secs: float) -> float:
        a, b = self.view
        return 10 + (secs - a) / (b - a) * (self.width() - 20)

    def _t(self, x: float) -> float:
        a, b = self.view
        return a + (x - 10) / max(1, self.width() - 20) * (b - a)

    def zoom_to(self, center: float, span: float) -> None:
        span = max(900.0, min(DAY, span))
        a = max(0.0, min(DAY - span, center - span / 2))
        self.view = (a, a + span)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        track = QRectF(10, 22, w - 20, h - 30)
        path = QPainterPath()
        path.addRoundedRect(track, 8, 8)
        p.fillPath(path, QColor(pal.surface_alt))
        p.setPen(QPen(QColor(pal.border), 1))
        p.drawPath(path)

        a, b = self.view
        span = b - a
        step = next(s for s in (300, 900, 1800, 3600, 7200, 10800) if span / s <= 12) if span < DAY else 7200
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.8))
        p.setFont(font)
        t = (a // step) * step
        while t <= b:
            x = self._x(t)
            if 10 <= x <= w - 10:
                p.setPen(QPen(QColor(pal.border_strong), 1))
                p.drawLine(QPointF(x, track.top() + 2), QPointF(x, track.bottom() - 2))
                p.setPen(QColor(pal.text_faint))
                hh, mm = int(t // 3600), int(t % 3600 // 60)
                label = f"{hh:02d}:{mm:02d}" if hh < 24 else "24:00"
                lx = max(0.0, min(w - 60.0, x - 30))
                align = Qt.AlignLeft if lx == 0 else Qt.AlignRight if lx == w - 60.0 else Qt.AlignHCenter
                p.drawText(QRectF(lx, 2, 60, 16), align | Qt.AlignVCenter, label)
            t += step

        p.save()
        p.setClipPath(path)
        for rec in self.recordings:
            s, e = self._secs(rec.start), self._secs(rec.end)
            if e < a or s > b:
                continue
            x1, x2 = self._x(max(s, a)), self._x(min(e, b))
            _, token = trigger_style(rec.triggers)
            color = QColor(getattr(pal, token, pal.accent))
            rect = QRectF(x1, track.top() + 6, max(2.5, x2 - x1), track.height() - 12)
            rp = QPainterPath()
            rp.addRoundedRect(rect, 3, 3)
            p.fillPath(rp, color)
        p.restore()

        if self.playhead and self.playhead.date() == self.day:
            x = self._x(self._secs(self.playhead))
            if 10 <= x <= w - 10:
                p.setPen(QPen(QColor(pal.text), 2))
                p.drawLine(QPointF(x, track.top() - 3), QPointF(x, track.bottom() + 3))
                p.setBrush(QColor(pal.text))
                p.setPen(Qt.NoPen)
                p.drawEllipse(QPointF(x, track.top() - 3), 3.5, 3.5)
        if self._hover is not None:
            x = self._x(self._hover)
            p.setPen(QPen(QColor(pal.accent), 1, Qt.DashLine))
            p.drawLine(QPointF(x, track.top()), QPointF(x, track.bottom()))
        p.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        x = event.position().x()
        if self._drag_x is not None and event.buttons() & Qt.LeftButton:
            dx = x - self._drag_x
            if abs(dx) > 3 or self._dragged:
                self._dragged = True
                a, b = self.view
                shift = -dx / max(1, self.width() - 20) * (b - a)
                shift = max(-a, min(DAY - b, shift))
                self.view = (a + shift, b + shift)
                self._drag_x = x
        secs = max(0.0, min(DAY - 1, self._t(x)))
        self._hover = secs
        hh, mm, ss = int(secs // 3600), int(secs % 3600 // 60), int(secs % 60)
        rec = next((r for r in self.recordings if self._secs(r.start) <= secs < self._secs(r.end)), None)
        tip = f"{hh:02d}:{mm:02d}:{ss:02d}"
        if rec:
            tip += f" — {', '.join(rec.triggers.labels()) or 'Recording'} ({human_duration(rec.duration.total_seconds())})"
        self.setToolTip(tip)
        self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self._hover = None
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_x = event.position().x()
            self._dragged = False

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and not self._dragged:
            secs = max(0.0, min(DAY - 1, self._t(event.position().x())))
            self.seek_requested.emit(dt.datetime.combine(self.day, dt.time()) + dt.timedelta(seconds=secs))
        self._drag_x = None
        self._dragged = False

    def wheelEvent(self, event) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        if not steps:
            return
        a, b = self.view
        center = self._t(event.position().x())
        span = (b - a) / (1.4 ** steps)
        span = max(900.0, min(DAY, span))
        frac = (center - a) / (b - a) if b > a else 0.5
        na = max(0.0, min(DAY - span, center - frac * span))
        self.view = (na, na + span)
        self.update()


class DownloadRow(QWidget):
    cancel_clicked = Signal()
    open_clicked = Signal()

    def __init__(self, job: DownloadJob):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 5)
        lay.setSpacing(3)
        top = QHBoxLayout()
        self.name = QLabel(job.path.name)
        self.name.setToolTip(str(job.path))
        self.name.setFont(scaled_font(self.name, 0.9))
        top.addWidget(self.name, 1)
        self.cancel = flat_button("close", "Cancel", size=14)
        self.cancel.clicked.connect(self.cancel_clicked.emit)
        self.open = flat_button("folder", "Show in folder", size=14)
        self.open.clicked.connect(self.open_clicked.emit)
        self.open.hide()
        top.addWidget(self.open)
        top.addWidget(self.cancel)
        lay.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)
        self.state = QLabel()
        self.state.setObjectName("Muted")
        self.state.setFont(scaled_font(self.state, 0.82))
        lay.addWidget(self.state)
        self.update_job(job)

    def update_job(self, job: DownloadJob) -> None:
        self.bar.setValue(int(job.fraction * 1000))
        total = job.total or job.rec.size
        if job.state == "running":
            speed = f" · {human_size(job.speed)}/s" if job.speed else ""
            self.state.setText(f"{human_size(job.done_bytes)} of {human_size(total)}{speed}" if total
                               else f"{human_size(job.done_bytes)}{speed}")
        elif job.state == "queued":
            self.state.setText("Waiting…")
        elif job.state == "done":
            self.state.setText(f"Saved · {human_size(job.done_bytes)}")
            self.bar.setValue(1000)
        elif job.state == "cancelled":
            self.state.setText("Cancelled")
        else:
            self.state.setText(job.error or "Failed")
        finished = job.state in ("done", "failed", "cancelled")
        self.cancel.setVisible(not finished)
        self.open.setVisible(job.state == "done")
        self.bar.setVisible(not finished or job.state == "done")


def reveal(path: Path) -> None:
    """Show a file in the desktop's file manager (select it where supported)."""
    try:
        subprocess.Popen(["dbus-send", "--session", "--dest=org.freedesktop.FileManager1", "--type=method_call",
                          "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                          f"array:string:{QUrl.fromLocalFile(str(path)).toString()}", "string:"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    except OSError:
        pass
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if path.is_file() else path)))


class PlaybackPage(QWidget):
    notify = Signal(str, str)

    def __init__(self, cameras: CameraManager, downloads: DownloadManager, config: Config):
        super().__init__()
        self.setObjectName("Page")
        self.cameras = cameras
        self.downloads = downloads
        self.config = config
        self.recordings: list[Recording] = []
        self.shown: list[Recording] = []
        self.current: Recording | None = None
        self._duration = 0.0
        self._seeking = False
        self._search_id = 0
        self._month_id = 0
        self._marked_dates: list[QDate] = []
        self._rows: dict[int, tuple[QListWidgetItem, DownloadRow]] = {}
        self._marked_days_cache: set[int] = set()

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)

        # ---- left: search
        left = Card("Search", "search")
        left.setMinimumWidth(270)
        left.setMaximumWidth(360)
        self.camera = QComboBox()
        self.camera.currentIndexChanged.connect(self._camera_changed)
        left.add(self.camera)
        self.lens = SegmentedControl()
        self.lens.add("Wide", WIDE, "Recordings of the wide-angle lens")
        self.lens.add("Tele", TELE, "Recordings of the telephoto lens")
        self.lens.set_value(WIDE)
        self.lens.changed.connect(lambda _v: self._reload_all())
        left.add(self.lens)
        self.stream = SegmentedControl()
        self.stream.add("Clear", MAIN, "Full-resolution recordings")
        self.stream.add("Fluent", SUB, "Low-resolution recordings (faster to load)")
        self.stream.set_value(MAIN)
        self.stream.changed.connect(lambda _v: self._reload_all())
        left.add(self.stream)
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(False)
        self.calendar.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.calendar.setHorizontalHeaderFormat(QCalendarWidget.SingleLetterDayNames)
        self.calendar.setMaximumDate(QDate.currentDate())
        self.calendar.setFirstDayOfWeek(Qt.Monday)
        theme.changed.connect(lambda _p: self._style_weekends())
        self._style_weekends()
        self.calendar.selectionChanged.connect(self._search)
        self.calendar.currentPageChanged.connect(lambda y, m: self._load_month())
        left.add(self.calendar)
        hint = QLabel("Days with recordings are highlighted.")
        hint.setObjectName("Faint")
        hint.setFont(scaled_font(hint, 0.85))
        left.add(hint)
        filters = QLabel("EVENTS")
        filters.setObjectName("CardTitle")
        filters.setFont(scaled_font(filters, 0.78, bold=True))
        left.add(filters)
        chips = QGridLayout()
        chips.setContentsMargins(0, 0, 0, 0)
        chips.setSpacing(5)
        self.filters: dict[str, QToolButton] = {}
        for i, (key, label) in enumerate((("all", "All"), ("person", "Person"), ("vehicle", "Vehicle"),
                                          ("animal", "Animal"), ("motion", "Other"))):
            c = chip(label, key == "all")
            c.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            c.clicked.connect(lambda _c=False, k=key: self._filter_clicked(k))
            self.filters[key] = c
            chips.addWidget(c, i // 3, i % 3)
        wrap = QWidget()
        wrap.setLayout(chips)
        left.add(wrap)
        left.body().addStretch(1)
        split.addWidget(left)

        # ---- center: player
        center = Card("Playback", "film")
        self.clip_title = QLabel("")
        self.clip_title.setObjectName("Muted")
        center.add_header_widget(self.clip_title)
        s = config.settings
        self.player = VideoTile(live=False, hwdec=s.hwdec, low_latency=False)
        self.player.setMinimumSize(420, 240)
        self.player.set_status("Pick a day and a recording", "info")
        self.player.video.position_changed.connect(self._position)
        self.player.video.duration_changed.connect(self._duration_changed)
        self.player.video.pause_changed.connect(self._pause_changed)
        self.player.video.started.connect(self._started)
        self.player.video.failed.connect(self._failed)
        self.player.video.ended.connect(self._ended)
        self.player.video.clicked.connect(self._toggle_pause)
        self.player.retry_requested.connect(lambda: self.play(self.current) if self.current else None)
        center.add(self.player, 1)

        transport = QHBoxLayout()
        transport.setSpacing(4)
        self.btn_prev = flat_button("skip_back", "Previous recording")
        self.btn_prev.clicked.connect(lambda: self._step_clip(-1))
        self.btn_back = flat_button("rewind10", "Back 10 seconds")
        self.btn_back.clicked.connect(lambda: self.player.video.seek(-10, relative=True))
        self.btn_play = flat_button("play", "Play / pause (Space)", size=22)
        self.btn_play.clicked.connect(self._toggle_pause)
        self.btn_fwd = flat_button("forward10", "Forward 10 seconds")
        self.btn_fwd.clicked.connect(lambda: self.player.video.seek(10, relative=True))
        self.btn_next = flat_button("skip_fwd", "Next recording")
        self.btn_next.clicked.connect(lambda: self._step_clip(1))
        for b in (self.btn_prev, self.btn_back, self.btn_play, self.btn_fwd, self.btn_next):
            transport.addWidget(b)
        self.pos_label = QLabel("0:00")
        self.pos_label.setObjectName("Muted")
        transport.addWidget(self.pos_label)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.slider.sliderReleased.connect(self._slider_released)
        transport.addWidget(self.slider, 1)
        self.dur_label = QLabel("0:00")
        self.dur_label.setObjectName("Muted")
        transport.addWidget(self.dur_label)
        self.speed = QComboBox()
        for sp in (0.5, 1.0, 2.0, 4.0, 8.0, 16.0):
            self.speed.addItem(f"{sp:g}×", sp)
        self.speed.setCurrentIndex(1)
        self.speed.setToolTip("Playback speed")
        self.speed.currentIndexChanged.connect(lambda _i: self.player.video.set_speed(self.speed.currentData()))
        transport.addWidget(self.speed)
        self.btn_mute = flat_button("mute", "Sound on/off", checkable=True)
        self.btn_mute.clicked.connect(self._toggle_mute)
        transport.addWidget(self.btn_mute)
        self.btn_snap = flat_button("snapshot", "Save this frame as a picture")
        self.btn_snap.clicked.connect(self._snapshot)
        transport.addWidget(self.btn_snap)
        self.btn_dl = flat_button("download", "Download this recording to the PC")
        self.btn_dl.clicked.connect(lambda: self._download([self.current] if self.current else []))
        transport.addWidget(self.btn_dl)
        center.add_layout(transport)
        self.timeline = Timeline()
        self.timeline.seek_requested.connect(self._timeline_seek)
        center.add(self.timeline)
        split.addWidget(center)

        # ---- right: recordings + downloads
        right = Card("Recordings", "recordings")
        right.setMinimumWidth(300)
        right.setMaximumWidth(420)
        self.count = QLabel("")
        self.count.setObjectName("Muted")
        right.add_header_widget(self.count)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Time", "Event", "Length", "Size"])
        self.list.setRootIsDecorated(False)
        self.list.setAlternatingRowColors(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setIconSize(QSize(16, 16))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.list.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.list.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.list.header().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.list.itemActivated.connect(lambda item, _c: self.play(item.data(0, Qt.UserRole)))
        self.list.itemClicked.connect(lambda item, _c: self.play(item.data(0, Qt.UserRole)))
        right.add(self.list, 3)
        row = QHBoxLayout()
        dl_sel = QPushButton("Download selected")
        dl_sel.setProperty("variant", "primary")
        dl_sel._binder = bind_icon(dl_sel, "download", "accent_text")
        dl_sel.clicked.connect(lambda: self._download([i.data(0, Qt.UserRole) for i in self.list.selectedItems()]))
        dl_all = QPushButton("All shown")
        dl_all.setToolTip("Download every recording in the list")
        dl_all.clicked.connect(lambda: self._download(list(self.shown)))
        row.addWidget(dl_sel)
        row.addWidget(dl_all)
        row.addStretch(1)
        right.add_layout(row)
        dl_head = QHBoxLayout()
        title = QLabel("DOWNLOADS")
        title.setObjectName("CardTitle")
        title.setFont(scaled_font(title, 0.78, bold=True))
        dl_head.addWidget(title)
        dl_head.addStretch(1)
        clear = QPushButton("Clear")
        clear.setProperty("variant", "ghost")
        clear.clicked.connect(self._clear_downloads)
        folder = QPushButton("Open folder")
        folder.setProperty("variant", "ghost")
        folder.clicked.connect(self._open_folder)
        dl_head.addWidget(clear)
        dl_head.addWidget(folder)
        right.add_layout(dl_head)
        self.dl_list = QListWidget()
        self.dl_list.setFrameShape(QFrame.NoFrame)
        self.dl_list.setSelectionMode(QListWidget.NoSelection)
        self.dl_empty = QLabel("Downloaded recordings are saved in your Videos folder.")
        self.dl_empty.setObjectName("Faint")
        self.dl_empty.setWordWrap(True)
        right.add(self.dl_empty)
        right.add(self.dl_list, 2)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([290, 900, 340])

        downloads.job_added.connect(self._job_added)
        downloads.job_changed.connect(self._job_changed)
        cameras.cameras_changed.connect(self.refresh_cameras)
        theme.changed.connect(lambda _p: (self._mark_days(self._marked_days_cache), self._fill_list()))
        self._update_downloads_visibility()
        self._shortcuts()
        self.refresh_cameras()

    def _style_weekends(self) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QBrush(QColor(theme.palette.text_muted)))
        for day in (Qt.Saturday, Qt.Sunday):
            self.calendar.setWeekdayTextFormat(day, fmt)

    # ------------------------------------------------------------------ cameras
    def refresh_cameras(self) -> None:
        current = self.camera.currentData()
        self.camera.blockSignals(True)
        self.camera.clear()
        for cam_id, ch_index, label in self.cameras.all_views():
            ch = self.cameras.channel(cam_id, ch_index)
            e = self.cameras.entry(cam_id)
            if e and e.online and ch and not ch.caps.replay:
                continue
            suffix = "" if (e and e.online) else "  (offline)"
            self.camera.addItem(label + suffix, (cam_id, ch_index))
        idx = self.camera.findData(current) if current else 0
        self.camera.setCurrentIndex(max(0, idx))
        self.camera.blockSignals(False)
        if self.camera.currentData() != current:
            self._camera_changed()
        else:
            self._update_lens_visibility()

    def select_view(self, cam_id: str, ch_index: int) -> None:
        idx = self.camera.findData((cam_id, ch_index))
        if idx >= 0 and idx != self.camera.currentIndex():
            self.camera.setCurrentIndex(idx)

    def view(self) -> tuple[str, int] | None:
        return self.camera.currentData()

    def _update_lens_visibility(self) -> None:
        view = self.view()
        ch = self.cameras.channel(*view) if view else None
        self.lens.setVisible(bool(ch and ch.caps.telephoto))
        if not (ch and ch.caps.telephoto):
            self.lens.set_value(WIDE)

    def _camera_changed(self, *_args) -> None:
        self._update_lens_visibility()
        self._reload_all()

    def _reload_all(self) -> None:
        self._load_month()
        self._search()

    # ------------------------------------------------------------------ search
    def _load_month(self) -> None:
        view = self.view()
        self._mark_days(set())
        if not view or not self.cameras.device(view[0]):
            return
        year, month = self.calendar.yearShown(), self.calendar.monthShown()
        self._month_id += 1
        mid = self._month_id
        stream, lens = self.stream.value() or MAIN, self.lens.value() or WIDE
        self.cameras.call(view[0], lambda d: d.recording_days(view[1], year, month, stream, lens),
                          lambda days, m=mid: self._month_loaded(m, year, month, days),
                          lambda exc: None)

    def _month_loaded(self, mid: int, year: int, month: int, days: set[int]) -> None:
        if mid != self._month_id or (year, month) != (self.calendar.yearShown(), self.calendar.monthShown()):
            return
        self._mark_days(days)

    def _mark_days(self, days: set[int]) -> None:
        self._marked_days_cache = set(days)
        plain = QTextCharFormat()
        for d in self._marked_dates:
            self.calendar.setDateTextFormat(d, plain)
        self._marked_dates = []
        pal = theme.palette
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Bold)
        fmt.setForeground(QBrush(QColor(pal.accent)))
        bg = QColor(pal.accent)
        bg.setAlpha(40)
        fmt.setBackground(QBrush(bg))
        year, month = self.calendar.yearShown(), self.calendar.monthShown()
        for day in days:
            date = QDate(year, month, day)
            if date.isValid():
                self.calendar.setDateTextFormat(date, fmt)
                self._marked_dates.append(date)

    def _search(self) -> None:
        view = self.view()
        day = self.calendar.selectedDate().toPython()
        self._search_id += 1
        sid = self._search_id
        self.recordings = []
        self._fill_list()
        self.timeline.set_day(day, [])
        if not view:
            self.count.setText("")
            return
        if not self.cameras.device(view[0]):
            self.count.setText("camera offline")
            return
        self.count.setText("searching…")
        stream, lens = self.stream.value() or MAIN, self.lens.value() or WIDE
        self.cameras.call(view[0], lambda d: d.recordings(view[1], day, stream, lens),
                          lambda recs, s=sid: self._results(s, day, recs),
                          lambda exc, s=sid: self._search_failed(s, exc))

    def _results(self, sid: int, day: dt.date, recs: list[Recording]) -> None:
        if sid != self._search_id:
            return
        self.recordings = recs
        self.timeline.set_day(day, recs)
        self._fill_list()

    def _search_failed(self, sid: int, exc: Exception) -> None:
        if sid != self._search_id:
            return
        self.count.setText("search failed")
        self.notify.emit(f"Could not search recordings: {exc}", "error")

    def _filter_clicked(self, key: str) -> None:
        if key == "all":
            for k, c in self.filters.items():
                c.setChecked(k == "all")
        else:
            self.filters["all"].setChecked(False)
            if not any(c.isChecked() for c in self.filters.values()):
                self.filters["all"].setChecked(True)
        self._fill_list()

    def _matches(self, rec: Recording) -> bool:
        if self.filters["all"].isChecked():
            return True
        t = rec.triggers
        wanted = {k for k, c in self.filters.items() if c.isChecked()}
        if "person" in wanted and t & (Trigger.PERSON | Trigger.FACE):
            return True
        if "vehicle" in wanted and t & Trigger.VEHICLE:
            return True
        if "animal" in wanted and t & Trigger.ANIMAL:
            return True
        if "motion" in wanted and not t & (Trigger.PERSON | Trigger.FACE | Trigger.VEHICLE | Trigger.ANIMAL):
            return True
        return False

    def _fill_list(self) -> None:
        pal = theme.palette
        self.shown = [r for r in self.recordings if self._matches(r)]
        self.list.clear()
        current_item = None
        for rec in reversed(self.shown):
            icon, token = trigger_style(rec.triggers)
            labels = ", ".join(rec.triggers.labels()) or "Recording"
            short = ", ".join(lbl for lbl in rec.triggers.labels() if lbl != "Motion" or len(rec.triggers.labels()) == 1)
            item = QTreeWidgetItem([f"{rec.start:%H:%M:%S}", short or "Recording",
                                    human_duration(rec.duration.total_seconds()), human_size(rec.size) if rec.size else ""])
            item.setIcon(0, icons.icon(icon, getattr(pal, token, pal.accent)))
            tip = f"{rec.start:%H:%M:%S} – {rec.end:%H:%M:%S}\n{labels}\n{rec.name}"
            item.setToolTip(0, tip)
            item.setToolTip(1, tip)
            item.setData(0, Qt.UserRole, rec)
            self.list.addTopLevelItem(item)
            if self.current and rec.name == self.current.name:
                current_item = item
        if current_item:
            self.list.setCurrentItem(current_item)
        total = len(self.recordings)
        if total == 0:
            self.count.setText("no recordings" if self._search_id else "")
        elif len(self.shown) == total:
            self.count.setText(f"{total} recordings")
        else:
            self.count.setText(f"{len(self.shown)} of {total}")

    # ------------------------------------------------------------------ playback
    def play(self, rec: Recording | None, offset: float = 0.0) -> None:
        if rec is None:
            return
        view = self.view()
        dev = self.cameras.device(view[0]) if view else None
        if not dev:
            self.notify.emit("The camera is offline", "warning")
            return
        self.current = rec
        self.clip_title.setText(f"{rec.start:%a %d %b %Y · %H:%M:%S}")
        self.player.set_status("Loading recording…", "busy")
        self.timeline.set_playhead(rec.start + dt.timedelta(seconds=offset))
        self.player.video.set_speed(self.speed.currentData())

        def urls():
            return dev.playback_urls(rec)

        worker.run(urls, lambda u, r=rec: self._start_urls(r, u, offset),
                   lambda exc: self._failed(str(exc)))
        for i in range(self.list.topLevelItemCount()):
            item = self.list.topLevelItem(i)
            if item.data(0, Qt.UserRole).name == rec.name:
                self.list.setCurrentItem(item)
                break

    def _start_urls(self, rec: Recording, urls: list[str], offset: float) -> None:
        if self.current is not rec:
            return
        self.player.video.play(urls, start=offset or None)

    def _started(self) -> None:
        self.player.clear_status()

    def _failed(self, message: str) -> None:
        self.player.set_status(f"Could not play this recording\n{message}", "error", retry=True)

    def _ended(self) -> None:
        self._step_clip(1)

    def _toggle_pause(self) -> None:
        if self.player.video.playing:
            self.player.video.toggle_pause()
        elif self.current:
            self.play(self.current)

    def _pause_changed(self, paused: bool) -> None:
        self.btn_play._binder.set_name("play" if paused else "pause")

    def _position(self, pos: float) -> None:
        if self.current:
            self.timeline.set_playhead(self.current.start + dt.timedelta(seconds=pos))
        self.pos_label.setText(human_duration(pos))
        if not self._seeking and self._duration > 0:
            self.slider.setValue(int(pos / self._duration * 1000))

    def _duration_changed(self, duration: float) -> None:
        if self.current:
            expected = self.current.duration.total_seconds()
            if expected > 0 and duration < expected * 0.5:
                duration = expected      # streamed files may not report their full length yet
        self._duration = duration
        self.dur_label.setText(human_duration(duration))

    def _slider_released(self) -> None:
        self._seeking = False
        if self._duration > 0:
            self.player.video.seek(self.slider.value() / 1000 * self._duration)

    def _step_clip(self, step: int) -> None:
        if not self.shown:
            return
        names = [r.name for r in self.shown]
        if self.current and self.current.name in names:
            i = names.index(self.current.name) + step
        else:
            i = 0 if step > 0 else len(self.shown) - 1
        if 0 <= i < len(self.shown):
            self.play(self.shown[i])

    def _timeline_seek(self, moment: dt.datetime) -> None:
        rec = next((r for r in self.recordings if r.contains(moment)), None)
        if rec is None:
            later = [r for r in self.recordings if r.start > moment]
            if not later:
                return
            rec, offset = later[0], 0.0
        else:
            offset = (moment - rec.start).total_seconds()
        if self.current and rec.name == self.current.name and self.player.video.playing:
            self.player.video.seek(offset)
        else:
            self.play(rec, offset)

    def _toggle_mute(self) -> None:
        muted = not self.player.video.muted
        self.player.video.set_muted(muted)
        self.btn_mute._binder.set_name("mute" if muted else "volume")
        self.btn_mute.setChecked(not muted)

    def _snapshot(self) -> None:
        if not self.current:
            return
        view = self.view()
        label = self.cameras.label(*view) if view else "camera"
        folder = self.config.settings.pictures() / safe_name(label)
        folder.mkdir(parents=True, exist_ok=True)
        moment = self.current.start + dt.timedelta(seconds=self.player.video.position())
        path = folder / f"{safe_name(label)}_{moment:%Y-%m-%d_%H-%M-%S}_playback.jpg"
        if self.player.video.screenshot(str(path)):
            self.notify.emit(f"Saved {path.name}", "success")

    # ------------------------------------------------------------------ downloads
    def _download(self, recs: list[Recording]) -> None:
        view = self.view()
        recs = [r for r in recs if r is not None]
        if not view or not recs:
            return
        label = self.cameras.label(*view)
        folder = self.config.settings.videos() / safe_name(label) / "SD card"
        added = 0
        for rec in recs:
            path = folder / rec.local_filename(label)
            if path.exists():
                continue
            if self.downloads.enqueue(view[0], rec, path):
                added += 1
        if added:
            self.notify.emit(f"Downloading {added} recording{'s' if added != 1 else ''} to {folder}", "info")
        else:
            self.notify.emit("Already downloaded (see the Downloads list)", "info")

    def _job_added(self, job: DownloadJob) -> None:
        item = QListWidgetItem()
        row = DownloadRow(job)
        row.cancel_clicked.connect(lambda j=job: self.downloads.cancel(j))
        row.open_clicked.connect(lambda j=job: reveal(j.path))
        item.setSizeHint(row.sizeHint())
        self.dl_list.insertItem(0, item)
        self.dl_list.setItemWidget(item, row)
        self._rows[job.id] = (item, row)
        self._update_downloads_visibility()

    def _job_changed(self, job: DownloadJob) -> None:
        pair = self._rows.get(job.id)
        if pair:
            pair[1].update_job(job)

    def _clear_downloads(self) -> None:
        for job in list(self.downloads.jobs):
            if job.state not in ("queued", "running"):
                pair = self._rows.pop(job.id, None)
                if pair:
                    self.dl_list.takeItem(self.dl_list.row(pair[0]))
        self.downloads.clear_finished()
        self._update_downloads_visibility()

    def _update_downloads_visibility(self) -> None:
        has = self.dl_list.count() > 0
        self.dl_list.setVisible(has)
        self.dl_empty.setVisible(not has)

    def _open_folder(self) -> None:
        view = self.view()
        folder = self.config.settings.videos()
        if view:
            candidate = folder / safe_name(self.cameras.label(*view)) / "SD card"
            if candidate.exists():
                folder = candidate
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ------------------------------------------------------------------ lifecycle
    def activate(self) -> None:
        if not self.recordings and self.view():
            self._reload_all()

    def deactivate(self) -> None:
        if self.player.video.playing:
            self.player.video.set_paused(True)

    def shutdown(self) -> None:
        self.player.video.shutdown()

    def _shortcuts(self) -> None:
        for key, fn in (("Space", self._toggle_pause), ("Left", lambda: self.player.video.seek(-5, relative=True)),
                        ("Right", lambda: self.player.video.seek(5, relative=True))):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(fn)
