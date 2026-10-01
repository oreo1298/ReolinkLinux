"""Video rendering: an mpv player drawn into a QOpenGLWidget.

The OpenGL render API works the same on X11 and Wayland, supports hardware decoding
(VA-API, NVDEC, …) and lets several players share one window. Mouse wheel zooms
digitally around the cursor and dragging pans, which is how you inspect the 4608×1728
panorama of a Duo 2 or the 4K image of a TrackMix at full quality.
"""

from __future__ import annotations

import math
import os
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QOpenGLContext, QPainter, QPainterPath, QPen
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QFrame, QPushButton, QSizePolicy, QStackedLayout, QVBoxLayout, QWidget

from ..core.models import GPU_DECODE_MAX
from . import icons, mpv
from .theme import theme
from .widgets import human_duration

MAX_ZOOM = 8.0

gl_description = ""  # "vendor · renderer · version" of the OpenGL context (for diagnostics)


class VideoWidget(QOpenGLWidget):
    """Plays one URL (or tries a list of URLs in order) with libmpv."""

    started = Signal()                 # first frame after (re)loading
    failed = Signal(str)               # every URL failed
    ended = Signal()                   # end of file (recordings)
    position_changed = Signal(float)
    duration_changed = Signal(float)
    pause_changed = Signal(bool)
    video_size_changed = Signal(int, int)
    zoom_changed = Signal(float)
    clicked = Signal()
    double_clicked = Signal()
    context_requested = Signal(object)  # global QPoint

    _frame_ready = Signal()
    _wakeup = Signal()

    def __init__(self, parent=None, live: bool = True, hwdec: str = "auto-copy-safe", low_latency: bool = True,
                 cpu_for_large: bool = True):
        super().__init__(parent)
        self.live = live
        self.cpu_for_large = cpu_for_large
        self.large_on_cpu = False      # the current stream is larger than 4K and decoded on the CPU
        self.decode_errors = 0         # damaged frames reported by the decoder since play()
        self._hwdec = "no"
        self.setUpdateBehavior(QOpenGLWidget.NoPartialUpdate)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(QSize(80, 45))
        self.setMouseTracking(False)
        self.player: mpv.Mpv | None = None
        self._render: mpv.RenderContext | None = None
        self._urls: list[str] = []
        self._url_index = 0
        self._start: float | None = None
        self._last_error = ""
        self._playing = False
        self._has_frame = False
        self.video_w = 0
        self.video_h = 0
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.fill = False
        self._drag_from: QPointF | None = None
        self._drag_moved = False
        self.muted = True
        self._frame_ready.connect(self.update, Qt.QueuedConnection)
        self._wakeup.connect(self._drain_events, Qt.QueuedConnection)
        self._create_player(hwdec, low_latency)

    # ------------------------------------------------------------------ setup
    def _create_player(self, hwdec: str, low_latency: bool) -> None:
        opts: dict[str, object] = {
            "vo": "libmpv", "osd-level": 0, "terminal": "no",
            "input-default-bindings": "no", "input-vo-keyboard": "no", "idle": "yes",
            "keep-open": "no" if self.live else "yes", "mute": "yes", "volume": 100,
            "network-timeout": 12,
            "rtsp-transport": "tcp", "tls-verify": "no", "ytdl": "no", "load-scripts": "no",
            "screenshot-format": "jpg", "screenshot-jpeg-quality": 95, "audio-client-name": "ReolinkLinux",
            "demuxer-lavf-o-add": "reconnect=1", "force-seekable": "yes",
        }
        if self.live:
            opts.update({"cache": "no" if low_latency else "yes", "demuxer-lavf-analyzeduration": 1.0,
                         "video-latency-hacks": "yes" if low_latency else "no", "untimed": "no",
                         "demuxer-readahead-secs": 0.5 if low_latency else 2, "audio-buffer": 0.2,
                         "interpolation": "no"})
            if low_latency:
                opts["demuxer-lavf-o"] = "fflags=+nobuffer"
        else:
            opts.update({"cache": "yes", "demuxer-max-bytes": "400MiB", "demuxer-max-back-bytes": "200MiB",
                         "hr-seek": "yes"})
        try:
            self.player = mpv.Mpv(opts)
        except (OSError, mpv.MpvError) as exc:
            self.player = None
            self._last_error = str(exc)
            return
        self.set_hwdec(hwdec)
        # Pick the decoder for each stream once its size is known, before the decoder starts.
        self.player.hook_add("on_preloaded")
        self.player.request_log_messages("error")
        for name, fmt in (("dwidth", mpv.FORMAT_INT64), ("dheight", mpv.FORMAT_INT64),
                          ("time-pos", mpv.FORMAT_DOUBLE), ("duration", mpv.FORMAT_DOUBLE),
                          ("pause", mpv.FORMAT_FLAG)):
            self.player.observe(name, fmt)
        self.player.set_wakeup_callback(self._wakeup.emit)

    def set_hwdec(self, hwdec: str) -> None:
        """Video decoder for the next stream: an mpv ``hwdec`` value, "no" for the CPU."""
        if not self.player:
            return
        # Older mpv releases lack some hwdec values: fall back to the nearest one they know.
        fallbacks = {"auto-copy-safe": ["auto-copy-safe", "auto-copy"], "auto-safe": ["auto-safe", "auto"]}
        for value in fallbacks.get(hwdec, [hwdec]) + ["no"]:
            if value and self.player.set("hwdec", value):
                self._hwdec = value
                break

    def _on_preloaded(self, hook_id: int) -> None:
        """mpv opened a stream and waits for us before it starts the decoders."""
        player = self.player
        if not player:
            return
        try:
            w, h = _video_track_size(player)
            self.large_on_cpu = self.cpu_for_large and self._hwdec != "no" and (w > GPU_DECODE_MAX[0]
                                                                               or h > GPU_DECODE_MAX[1])
            player.set("hwdec", "no" if self.large_on_cpu else self._hwdec)
        finally:
            player.hook_continue(hook_id)

    @property
    def available(self) -> bool:
        return self.player is not None

    @property
    def error(self) -> str:
        return self._last_error

    def initializeGL(self) -> None:  # noqa: N802
        if not self.player or self._render:
            return
        ctx = QOpenGLContext.currentContext()
        global gl_description
        gl_description = gl_description or _describe_gl(ctx)
        # mpv probes texture formats by checking glGetError(); a stale error left in the
        # context by Qt would make it reject formats and draw nothing.
        _clear_gl_errors(ctx)
        if _use_simple_renderer(ctx):
            self.player.set("gpu-dumb-mode", True)
        try:
            self._render = mpv.RenderContext(self.player, lambda name: ctx.getProcAddress(name))
        except mpv.MpvError as exc:
            self._last_error = str(exc)
            self._render = None
            return
        self._render.set_update_callback(self._frame_ready.emit)
        ctx.aboutToBeDestroyed.connect(self._free_render)
        if self._urls and not self._playing:
            self._load_current()

    def _free_render(self) -> None:
        if self._render:
            self.makeCurrent()
            self._render.free()
            self._render = None
            self.doneCurrent()

    def paintGL(self) -> None:  # noqa: N802
        ratio = self.devicePixelRatioF()
        w, h = int(self.width() * ratio), int(self.height() * ratio)
        if self._render and self._playing:
            _clear_gl_errors(QOpenGLContext.currentContext())
            self._render.update()
            self._render.render(self.defaultFramebufferObject(), w, h)
        else:
            p = QPainter(self)
            p.fillRect(self.rect(), QColor("#07090c"))
            p.end()

    def shutdown(self) -> None:
        """Stop playback and release mpv (call before the widget is destroyed)."""
        self._playing = False
        self._urls = []
        if self._render:
            self._free_render()
        if self.player:
            player, self.player = self.player, None
            player.terminate()

    # ------------------------------------------------------------------ playback
    def play(self, urls, start: float | None = None) -> None:
        if isinstance(urls, str):
            urls = [urls]
        self._urls = [u for u in urls if u]
        self._url_index = 0
        self._start = start
        self._last_error = ""
        self._has_frame = False
        self.decode_errors = 0
        if not self.player:
            self.failed.emit(self._last_error or mpv.load_error() or "libmpv is not available")
            return
        if self._render:
            self._load_current()
        # otherwise initializeGL() starts playback once the widget is shown

    def _load_current(self) -> None:
        if not self.player or self._url_index >= len(self._urls):
            return
        url = self._urls[self._url_index]
        self._playing = True
        # "start" applies to the next loadfile (works the same on every mpv version)
        self.player.set("start", f"{self._start:.2f}" if self._start else "none")
        try:
            self.player.command("loadfile", url, "replace")
        except mpv.MpvError as exc:
            self._fail(str(exc))

    def stop(self) -> None:
        self._urls = []
        self._playing = False
        self._has_frame = False
        if self.player:
            try:
                self.player.command("stop")
            except mpv.MpvError:
                pass
        self.update()

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def has_frame(self) -> bool:
        return self._has_frame

    def current_url(self) -> str:
        return self._urls[self._url_index] if self._url_index < len(self._urls) else ""

    def _fail(self, message: str) -> None:
        self._url_index += 1
        if self._url_index < len(self._urls):
            self._load_current()
            return
        self._playing = False
        self._has_frame = False
        self.update()
        self.failed.emit(message or self._last_error or "Could not open the stream")

    def _drain_events(self) -> None:
        if not self.player:
            return
        for event_id, _error, payload in self.player.events():
            if event_id == mpv.EVENT_PROPERTY_CHANGE and payload:
                name, value = payload
                if name == "dwidth" and value:
                    self.video_w = int(value)
                    self.video_size_changed.emit(self.video_w, self.video_h)
                elif name == "dheight" and value:
                    self.video_h = int(value)
                    self.video_size_changed.emit(self.video_w, self.video_h)
                elif name == "time-pos" and value is not None:
                    self.position_changed.emit(float(value))
                elif name == "duration" and value is not None:
                    self.duration_changed.emit(float(value))
                elif name == "pause" and value is not None:
                    self.pause_changed.emit(bool(value))
            elif event_id == mpv.EVENT_LOG_MESSAGE and payload:
                prefix, level, text = payload
                if prefix == "ffmpeg/video":
                    self.decode_errors += 1   # damaged data in the stream, not a reason it failed
                elif text and level in ("error", "fatal"):
                    self._last_error = _clean_error(text)
            elif event_id == mpv.EVENT_HOOK and payload:
                self._on_preloaded(payload[1])
            elif event_id == mpv.EVENT_PLAYBACK_RESTART:
                if not self._has_frame:
                    self._has_frame = True
                    self.started.emit()
            elif event_id == mpv.EVENT_END_FILE and payload:
                reason, err = payload
                if reason == mpv.END_FILE_ERROR:
                    msg = self._last_error or self.player.error_string(err)
                    self._fail(msg)
                elif reason == mpv.END_FILE_EOF:
                    self._has_frame = False
                    if self.live:
                        self._playing = False
                        self.failed.emit(self._last_error or "The stream ended")
                    else:
                        self.ended.emit()

    # ------------------------------------------------------------------ controls
    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if self.player:
            self.player.set("mute", muted)

    def set_volume(self, volume: int) -> None:
        if self.player:
            self.player.set("volume", float(max(0, min(130, volume))))

    def set_paused(self, paused: bool) -> None:
        if self.player:
            self.player.set("pause", paused)

    def toggle_pause(self) -> None:
        if self.player:
            self.player.set("pause", not bool(self.player.get_flag("pause")))

    def paused(self) -> bool:
        return bool(self.player.get_flag("pause")) if self.player else False

    def seek(self, seconds: float, relative: bool = False) -> None:
        if self.player and self._playing:
            try:
                self.player.command("seek", f"{seconds:.3f}", "relative" if relative else "absolute+exact")
            except mpv.MpvError:
                pass

    def set_speed(self, speed: float) -> None:
        if self.player:
            self.player.set("speed", float(speed))

    def position(self) -> float:
        return (self.player.get_double("time-pos") or 0.0) if self.player else 0.0

    def screenshot(self, path: str) -> bool:
        if not (self.player and self._has_frame):
            return False
        try:
            self.player.command("screenshot-to-file", path, "video")
            return True
        except mpv.MpvError:
            return False

    def start_stream_record(self, path: str) -> bool:
        return bool(self.player and self.player.set("stream-record", path))

    def stop_stream_record(self) -> None:
        if self.player:
            self.player.set("stream-record", "")

    def stats(self) -> dict:
        if not self.player or not self._has_frame:
            return {}
        p = self.player
        out = {
            "codec": p.get_string("video-codec") or p.get_string("video-format") or "",
            "hwdec": p.get_string("hwdec-current") or "no",
            "fps": p.get_double("estimated-vf-fps") or p.get_double("container-fps") or 0.0,
            "width": self.video_w, "height": self.video_h,
            "bitrate": p.get_double("video-bitrate") or 0.0,
            "dropped": p.get_int("decoder-frame-drop-count") or 0,
            "errors": self.decode_errors, "large_on_cpu": self.large_on_cpu,
        }
        return out

    # ------------------------------------------------------------------ digital zoom
    def set_fill(self, fill: bool) -> None:
        self.fill = fill
        if self.player:
            self.player.set("panscan", 1.0 if fill else 0.0)
        self._apply_zoom()

    def _base_size(self) -> tuple[float, float]:
        if not (self.video_w and self.video_h):
            return float(self.width()), float(self.height())
        sx, sy = self.width() / self.video_w, self.height() / self.video_h
        s = max(sx, sy) if self.fill else min(sx, sy)
        return self.video_w * s, self.video_h * s

    def _clamp_pan(self) -> None:
        bw, bh = self._base_size()
        for axis, base, view in (("x", bw, self.width()), ("y", bh, self.height())):
            size = base * self.zoom
            limit = max(0.0, 0.5 - view / (2 * size)) if size > 0 else 0.0
            value = getattr(self, f"pan_{axis}")
            setattr(self, f"pan_{axis}", max(-limit, min(limit, value)))

    def _apply_zoom(self) -> None:
        self._clamp_pan()
        if self.player:
            self.player.set("video-zoom", math.log2(self.zoom))
            self.player.set("video-pan-x", self.pan_x)
            self.player.set("video-pan-y", self.pan_y)
        self.zoom_changed.emit(self.zoom)
        self.update()

    def zoom_at(self, pos: QPointF, factor: float) -> None:
        bw, bh = self._base_size()
        old = self.zoom
        new = max(1.0, min(MAX_ZOOM, old * factor))
        if abs(new - old) < 1e-6:
            return
        # keep the point under the cursor fixed
        cx = self.width() / 2 + self.pan_x * bw * old
        cy = self.height() / 2 + self.pan_y * bh * old
        u = (pos.x() - cx) / (bw * old) if bw else 0.0
        v = (pos.y() - cy) / (bh * old) if bh else 0.0
        self.zoom = new
        if new == 1.0:
            self.pan_x = self.pan_y = 0.0
        else:
            self.pan_x = ((pos.x() - u * bw * new) - self.width() / 2) / (bw * new) if bw else 0.0
            self.pan_y = ((pos.y() - v * bh * new) - self.height() / 2) / (bh * new) if bh else 0.0
        self._apply_zoom()

    def reset_zoom(self) -> None:
        self.zoom, self.pan_x, self.pan_y = 1.0, 0.0, 0.0
        self._apply_zoom()

    def resizeGL(self, w: int, h: int) -> None:  # noqa: N802
        if self.zoom > 1.0:
            self._clamp_pan()

    # ------------------------------------------------------------------ mouse
    def wheelEvent(self, event) -> None:  # noqa: N802
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.zoom_at(event.position(), 1.25 ** steps)
        event.accept()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._drag_from = event.position()
            self._drag_moved = False
            if self.zoom > 1.0:
                self.setCursor(Qt.ClosedHandCursor)
        elif event.button() == Qt.RightButton:
            self.context_requested.emit(event.globalPosition().toPoint())
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is None:
            return
        delta = event.position() - self._drag_from
        if not self._drag_moved and abs(delta.x()) + abs(delta.y()) < 4:
            return
        self._drag_moved = True
        if self.zoom > 1.0:
            bw, bh = self._base_size()
            self.pan_x += delta.x() / (bw * self.zoom) if bw else 0.0
            self.pan_y += delta.y() / (bh * self.zoom) if bh else 0.0
            self._drag_from = event.position()
            self._apply_zoom()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            if not self._drag_moved:
                self.clicked.emit()
            self._drag_from = None
            self.unsetCursor()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.double_clicked.emit()


_SOFTWARE_GL = ("llvmpipe", "softpipe", "swrast", "software rasterizer", "svga3d", "virgl")


def _use_simple_renderer(ctx) -> bool:
    """mpv's plain renderer on software OpenGL (VMs, no GPU driver).

    Its full multi-pass pipeline intermittently draws black or loses colour on Mesa's
    software rasterisers; the simple one is reliable there and much lighter.
    """
    forced = os.environ.get("REOLINKLINUX_SIMPLE_RENDERER")
    if forced is not None:
        return forced not in ("", "0", "no")
    try:
        renderer = str(ctx.functions().glGetString(0x1F01) or "").lower()  # GL_RENDERER
    except Exception:  # noqa: BLE001 - never fail playback because of the probe
        return False
    return any(name in renderer for name in _SOFTWARE_GL)


def _describe_gl(ctx) -> str:
    try:
        f = ctx.functions()
        return " · ".join(str(f.glGetString(name) or "?") for name in (0x1F00, 0x1F01, 0x1F02))
    except Exception:  # noqa: BLE001 - only informational
        return ""


def _video_track_size(player: mpv.Mpv) -> tuple[int, int]:
    for i in range(player.get_int("track-list/count") or 0):
        if player.get_string(f"track-list/{i}/type") == "video":
            return player.get_int(f"track-list/{i}/demux-w") or 0, player.get_int(f"track-list/{i}/demux-h") or 0
    return 0, 0


def _clear_gl_errors(ctx) -> None:
    if ctx is None:
        return
    f = ctx.functions()
    for _ in range(16):
        if f.glGetError() == 0:
            break


def _clean_error(text: str) -> str:
    import re
    text = re.sub(r"(rtsp|https?)://[^/@\s]+@", r"\1://", text)
    text = re.sub(r"(password|token)=[^&\s]+", r"\1=…", text)
    lowered = text.lower()
    if "401" in lowered or "unauthorized" in lowered:
        return "The camera refused the user name or password for the stream"
    if "connection refused" in lowered:
        return "Connection refused: is RTSP enabled on the camera?"
    if "timed out" in lowered or "timeout" in lowered:
        return "The camera did not answer (timed out)"
    if "404" in lowered or "not found" in lowered:
        return "The camera does not offer this stream"
    if "no route to host" in lowered or "network is unreachable" in lowered:
        return "The camera cannot be reached on the network"
    return text


class _Overlay(QWidget):
    """Name, badges, REC indicator and status text drawn above the video."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.title = ""
        self.subtitle = ""
        self.badge = ""
        self.recording = False
        self.record_started = 0.0
        self.detections: list[str] = []
        self.status = ""
        self.status_kind = "info"
        self.zoom = 1.0
        self.selected = False
        self.show_title = True
        self._blink = True
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(700)

    def _tick(self) -> None:
        self._blink = not self._blink
        if self.recording or self.status:
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        font = QFont(self.font())
        base = font.pointSizeF() if font.pointSizeF() > 0 else 10.0
        small = r.width() < 360

        if self.show_title and self.title:
            title_font = QFont(font)
            title_font.setBold(True)
            title_font.setPointSizeF(base * (0.92 if small else 1.0))
            badge_font = QFont(font)
            badge_font.setBold(True)
            badge_font.setPointSizeF(base * 0.72)
            p.setFont(title_font)
            tfm = p.fontMetrics()
            text_w = tfm.horizontalAdvance(self.title)
            height = tfm.height() + 10
            badge_w = 0
            if self.badge:
                p.setFont(badge_font)
                badge_w = p.fontMetrics().horizontalAdvance(self.badge) + 12
            scrim_w = 20 + text_w + (10 + badge_w if badge_w else 0)
            scrim = QPainterPath()
            scrim.addRoundedRect(QRectF(8, 8, scrim_w, height), 8, 8)
            p.fillPath(scrim, QColor(0, 0, 0, 140))
            p.setFont(title_font)
            p.setPen(QColor("#ffffff"))
            p.drawText(QRectF(18, 8, text_w + 2, height), Qt.AlignVCenter | Qt.AlignLeft, self.title)
            if badge_w:
                p.setFont(badge_font)
                box = QRectF(18 + text_w + 10, 8 + (height - 18) / 2, badge_w, 18)
                badge = QPainterPath()
                badge.addRoundedRect(box, 5, 5)
                p.fillPath(badge, QColor(255, 255, 255, 40))
                p.setPen(QColor("#e8ecf2"))
                p.drawText(box, Qt.AlignCenter, self.badge)

        # top-right: REC + detections
        x = r.width() - 10
        if self.recording:
            f = QFont(font)
            f.setBold(True)
            f.setPointSizeF(base * 0.85)
            p.setFont(f)
            label = "REC " + human_duration(time.monotonic() - self.record_started) if self.record_started else "REC"
            tw = p.fontMetrics().horizontalAdvance(label)
            box = QRectF(x - tw - 30, 10, tw + 30, 24)
            path = QPainterPath()
            path.addRoundedRect(box, 12, 12)
            p.fillPath(path, QColor(0, 0, 0, 150))
            dot = QColor(pal.rec)
            if not self._blink:
                dot.setAlpha(90)
            p.setBrush(dot)
            p.setPen(Qt.NoPen)
            p.drawEllipse(QPointF(box.left() + 13, box.center().y()), 5, 5)
            p.setPen(QColor("#ffffff"))
            p.drawText(box.adjusted(22, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, label)
            x = box.left() - 6
        colors = {"people": pal.person, "person": pal.person, "vehicle": pal.vehicle, "dog_cat": pal.animal,
                  "animal": pal.animal, "motion": pal.motion, "face": pal.person, "package": pal.warning}
        names = {"people": "person", "person": "person", "vehicle": "car", "dog_cat": "paw", "animal": "paw",
                 "motion": "motion", "face": "person", "package": "bookmark"}
        for det in self.detections:
            color = QColor(colors.get(det, pal.accent))
            box = QRectF(x - 26, 10, 26, 24)
            path = QPainterPath()
            path.addRoundedRect(box, 8, 8)
            fill = QColor(color)
            fill.setAlpha(210)
            p.fillPath(path, fill)
            pm = icons.pixmap(names.get(det, "motion"), "#ffffff", 16)
            p.drawPixmap(int(box.center().x() - 8), int(box.center().y() - 8), pm)
            x = box.left() - 5

        if self.zoom > 1.01:
            f = QFont(font)
            f.setBold(True)
            f.setPointSizeF(base * 0.85)
            p.setFont(f)
            label = f"{self.zoom:.1f}×"
            tw = p.fontMetrics().horizontalAdvance(label) + 16
            box = QRectF(r.width() - tw - 10, r.height() - 34, tw, 24)
            path = QPainterPath()
            path.addRoundedRect(box, 8, 8)
            p.fillPath(path, QColor(0, 0, 0, 150))
            p.setPen(QColor("#ffffff"))
            p.drawText(box, Qt.AlignCenter, label)

        if self.status:
            f = QFont(font)
            f.setPointSizeF(base * (0.9 if small else 1.0))
            p.setFont(f)
            color = QColor(pal.danger if self.status_kind == "error" else "#c9d1dc")
            text_rect = QRectF(16, r.height() / 2 - 40, r.width() - 32, 80)
            if self.status_kind == "busy":
                cx, cy = r.width() / 2, r.height() / 2 - 22
                pen = QPen(QColor(pal.accent), 3)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                angle = int((time.monotonic() * 360) % 360)
                p.drawArc(QRectF(cx - 11, cy - 11, 22, 22), -angle * 16, 270 * 16)
                text_rect = QRectF(16, cy + 18, r.width() - 32, 40)
            p.setPen(color)
            p.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap, self.status)

        if self.selected:
            pen = QPen(QColor(pal.accent), 2)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(r).adjusted(1, 1, -1, -1), 10, 10)
        p.end()


class VideoTile(QFrame):
    """A video with its overlay, a retry button, and automatic reconnection for live streams."""

    retry_requested = Signal()

    def __init__(self, parent=None, live: bool = True, hwdec: str = "auto-copy-safe", low_latency: bool = True,
                 cpu_for_large: bool = True):
        super().__init__(parent)
        self.setObjectName("Tile")
        self.setMinimumSize(160, 90)
        lay = QStackedLayout(self)
        lay.setStackingMode(QStackedLayout.StackAll)
        self.video = VideoWidget(self, live=live, hwdec=hwdec, low_latency=low_latency, cpu_for_large=cpu_for_large)
        self.overlay = _Overlay(self)
        self.retry = QPushButton("Retry")
        self.retry.setCursor(Qt.PointingHandCursor)
        self.retry.setProperty("variant", "primary")
        self.retry.hide()
        self.retry.clicked.connect(self.retry_requested.emit)
        holder = QWidget(self)
        holder.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        hl = QVBoxLayout(holder)
        hl.addStretch(3)
        hl.addWidget(self.retry, 0, Qt.AlignHCenter)
        hl.addStretch(2)
        holder.setAttribute(Qt.WA_NoSystemBackground)
        self._holder = holder
        lay.addWidget(self.video)
        lay.addWidget(self.overlay)
        lay.addWidget(holder)
        self.overlay.raise_()
        holder.raise_()
        holder.hide()
        self.video.zoom_changed.connect(self._on_zoom)
        self._spinner = QTimer(self)
        self._spinner.timeout.connect(self.overlay.update)

    def _on_zoom(self, zoom: float) -> None:
        self.overlay.zoom = zoom
        self.overlay.update()

    def set_status(self, text: str, kind: str = "info", retry: bool = False) -> None:
        self.overlay.status = text
        self.overlay.status_kind = kind
        self.retry.setVisible(retry)
        self._holder.setVisible(retry)
        if kind == "busy":
            self._spinner.start(40)
        else:
            self._spinner.stop()
        self.overlay.update()

    def clear_status(self) -> None:
        self.set_status("")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.overlay.setGeometry(self.rect())
        self._holder.setGeometry(self.rect())
