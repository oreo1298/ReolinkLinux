"""Application state shared by all pages: cameras, local recordings and downloads."""

from __future__ import annotations

import datetime as dt
import itertools
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ..core import recorder as rec_mod
from ..core.config import CameraConfig, Config
from ..core.demo import DemoDevice
from ..core.device import Device
from ..core.errors import AuthError, Cancelled, ReolinkError
from ..core.models import MAIN, TELE, WIDE, Channel, Recording
from . import worker


@dataclass(frozen=True)
class Source:
    """One video shown in a tile: a camera channel and a lens."""

    cam_id: str
    channel: int = 0
    lens: int = WIDE

    @property
    def key(self) -> str:
        return f"{self.cam_id}/{self.channel}/{self.lens}"

    @property
    def view_key(self) -> str:
        return f"{self.cam_id}/{self.channel}"


@dataclass
class CameraEntry:
    cfg: CameraConfig
    device: Device | None = None
    status: str = "offline"        # offline | connecting | online | error
    error: str = ""
    queue: worker.SerialQueue = field(default_factory=worker.SerialQueue)
    polling: bool = False
    tick: int = 0
    retry_at: float = 0.0
    failures: int = 0

    @property
    def online(self) -> bool:
        return self.status == "online" and self.device is not None


def friendly_error(exc: BaseException) -> str:
    if isinstance(exc, AuthError):
        return str(exc)
    if isinstance(exc, ReolinkError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


class CameraManager(QObject):
    cameras_changed = Signal()          # cameras added / removed / reordered
    camera_changed = Signal(str)        # status or details of one camera changed
    detection_changed = Signal(str)
    notify = Signal(str, str)           # message, kind (info/success/warning/error)

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.entries: dict[str, CameraEntry] = {c.id: CameraEntry(c) for c in config.cameras}
        self._poll = QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(self._poll_all)
        self._poll.start()
        self._retry = QTimer(self)
        self._retry.setInterval(5000)
        self._retry.timeout.connect(self._retry_failed)
        self._retry.start()

    # ------------------------------------------------------------------ queries
    def ordered(self) -> list[CameraEntry]:
        return [self.entries[c.id] for c in self.config.cameras if c.id in self.entries]

    def entry(self, cam_id: str) -> CameraEntry | None:
        return self.entries.get(cam_id)

    def device(self, cam_id: str) -> Device | None:
        e = self.entries.get(cam_id)
        return e.device if e and e.online else None

    def channel(self, cam_id: str, index: int) -> Channel | None:
        dev = self.device(cam_id)
        if not dev:
            return None
        return next((c for c in dev.channels if c.index == index), None)

    def label(self, cam_id: str, index: int = 0) -> str:
        e = self.entries.get(cam_id)
        if not e:
            return "Camera"
        dev = e.device
        if dev and dev.is_nvr:
            ch = next((c for c in dev.channels if c.index == index), None)
            return ch.name if ch and ch.name else f"{e.cfg.label} · {index + 1}"
        return e.cfg.label

    def sources(self) -> list[Source]:
        out = []
        for e in self.ordered():
            if not e.online:
                continue
            for ch in e.device.channels:
                if ch.online and ch.index not in e.cfg.hidden_channels:
                    out.append(Source(e.cfg.id, ch.index, WIDE))
        return out

    def all_views(self) -> list[tuple[str, int, str]]:
        """(cam_id, channel, label) for every known channel, online or not."""
        out = []
        for e in self.ordered():
            if e.online:
                for ch in e.device.channels:
                    if ch.index not in e.cfg.hidden_channels:
                        out.append((e.cfg.id, ch.index, self.label(e.cfg.id, ch.index)))
            else:
                out.append((e.cfg.id, 0, e.cfg.label))
        return out

    # ------------------------------------------------------------------ connection
    def _make_device(self, cfg: CameraConfig) -> Device:
        if cfg.demo:
            return DemoDevice(cfg.demo)
        return Device(cfg.host, cfg.username, self.config.password(cfg), cfg.port, cfg.https)

    def connect_all(self) -> None:
        for e in self.ordered():
            if e.status in ("offline", "error"):
                self.connect_camera(e.cfg.id)

    def connect_camera(self, cam_id: str) -> None:
        e = self.entries.get(cam_id)
        if not e or e.status == "connecting":
            return
        e.status, e.error = "connecting", ""
        self.camera_changed.emit(cam_id)
        device = self._make_device(e.cfg)

        def work():
            device.connect()
            return device

        worker.run(work, lambda dev, cid=cam_id: self._connected(cid, dev),
                   lambda exc, cid=cam_id: self._connect_failed(cid, exc))

    def _connected(self, cam_id: str, device: Device) -> None:
        e = self.entries.get(cam_id)
        if not e:
            device.disconnect()
            return
        e.device, e.status, e.error, e.failures = device, "online", "", 0
        changed = False
        if not e.cfg.name and device.info.name:
            e.cfg.name = device.info.name
            changed = True
        if not e.cfg.demo:
            if e.cfg.https != device.client.use_https or e.cfg.port != device.client.port:
                e.cfg.https, e.cfg.port = device.client.use_https, device.client.port
                changed = True
        if changed:
            self.config.save()
        self.camera_changed.emit(cam_id)
        self.cameras_changed.emit()

    def _connect_failed(self, cam_id: str, exc: Exception) -> None:
        e = self.entries.get(cam_id)
        if not e:
            return
        e.device, e.status, e.error = None, "error", friendly_error(exc)
        e.failures += 1
        # Wrong credentials will not fix themselves: do not hammer the camera (it locks logins).
        delay = 0 if isinstance(exc, AuthError) else min(300, 15 * (2 ** min(e.failures - 1, 4)))
        e.retry_at = time.monotonic() + delay if delay else 0.0
        self.camera_changed.emit(cam_id)
        self.cameras_changed.emit()
        if e.failures == 1:
            self.notify.emit(f"{e.cfg.label}: {e.error}", "error")

    def _retry_failed(self) -> None:
        now = time.monotonic()
        for e in self.ordered():
            if e.status == "error" and e.retry_at and now >= e.retry_at:
                e.retry_at = 0.0
                self.connect_camera(e.cfg.id)

    def disconnect_camera(self, cam_id: str) -> None:
        e = self.entries.get(cam_id)
        if not e:
            return
        dev, e.device = e.device, None
        e.status, e.error, e.retry_at = "offline", "", 0.0
        if dev:
            worker.run(dev.disconnect)
        self.camera_changed.emit(cam_id)
        self.cameras_changed.emit()

    def shutdown(self) -> None:
        """Log out of every camera (in parallel, best effort) so sessions are freed."""
        self._poll.stop()
        self._retry.stop()
        threads = []
        for e in self.entries.values():
            if e.device and not e.device.demo:
                client = e.device.client
                client.timeout = 2
                t = threading.Thread(target=client.logout, daemon=True)
                t.start()
                threads.append(t)
        deadline = time.monotonic() + 2.5
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))

    # ------------------------------------------------------------------ editing
    def add_camera(self, cfg: CameraConfig, password: str, connect: bool = True) -> None:
        self.config.add(cfg, password)
        self.entries[cfg.id] = CameraEntry(cfg)
        self.cameras_changed.emit()
        if connect:
            self.connect_camera(cfg.id)

    def update_camera(self, cfg: CameraConfig, password: str | None) -> None:
        if password is not None:
            self.config.set_password(cfg, password)
        self.config.save()
        self.disconnect_camera(cfg.id)
        self.entries[cfg.id].cfg = cfg
        self.connect_camera(cfg.id)

    def remove_camera(self, cam_id: str) -> None:
        self.disconnect_camera(cam_id)
        self.entries.pop(cam_id, None)
        self.config.remove(cam_id)
        self.cameras_changed.emit()

    def move_camera(self, cam_id: str, offset: int) -> None:
        self.config.move(cam_id, offset)
        self.cameras_changed.emit()

    # ------------------------------------------------------------------ commands
    def control(self, cam_id: str, fn, done=None, what: str = "") -> None:
        """Run ``fn(device)`` on the camera's ordered command queue; report failures."""
        e = self.entries.get(cam_id)
        if not e or not e.online:
            self.notify.emit("The camera is not connected", "warning")
            return
        dev = e.device

        def fail(exc: Exception) -> None:
            self.notify.emit(f"{what + ': ' if what else ''}{friendly_error(exc)}", "error")

        e.queue.run(lambda: fn(dev), done, fail)

    def call(self, cam_id: str, fn, done=None, error=None) -> None:
        """Run ``fn(device)`` on the shared pool (for slow, independent requests)."""
        dev = self.device(cam_id)
        if not dev:
            if error:
                error(ReolinkError("The camera is not connected"))
            return
        worker.run(lambda: fn(dev), done, error)

    # ------------------------------------------------------------------ polling
    def _poll_all(self) -> None:
        for e in self.ordered():
            if not e.online or e.polling:
                continue
            e.polling = True
            e.tick += 1
            dev = e.device
            lights = e.tick % 5 == 1

            def work(d=dev, lights=lights):
                d.poll(lights=lights)

            def done(_r, cid=e.cfg.id):
                ent = self.entries.get(cid)
                if ent:
                    ent.polling = False
                    self.detection_changed.emit(cid)

            def failed(exc, cid=e.cfg.id):
                ent = self.entries.get(cid)
                if ent:
                    ent.polling = False
                    ent.tick = 0

            worker.run(work, done, failed)


class RecordingManager(QObject):
    """Local recording of live streams: manual (per tile) and continuous (per camera)."""

    changed = Signal(str)          # source key (or "" for many)
    notify = Signal(str, str)

    def __init__(self, cameras: CameraManager, config: Config):
        super().__init__()
        self.cameras = cameras
        self.config = config
        self.manual: dict[str, rec_mod.Recorder] = {}
        self.continuous: dict[str, rec_mod.Recorder] = {}
        self.fallback: dict[str, object] = {}       # key -> VideoWidget recording with mpv
        self.started: dict[str, float] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._check)
        self._timer.start()
        self._cleanup = QTimer(self)
        self._cleanup.setInterval(15 * 60 * 1000)
        self._cleanup.timeout.connect(self.apply_retention)
        self._cleanup.start()
        cameras.camera_changed.connect(lambda _cid: self.sync_continuous())

    @property
    def settings(self):
        return self.config.settings

    def camera_dir(self, cam_id: str, channel: int, sub: str) -> Path:
        name = rec_mod.safe_name(self.cameras.label(cam_id, channel))
        return self.settings.videos() / name / sub

    def is_recording(self, key: str) -> bool:
        return key in self.manual or key in self.fallback

    def is_continuous(self, key: str) -> bool:
        return key in self.continuous

    def recording_count(self) -> int:
        return len(self.manual) + len(self.fallback) + len(self.continuous)

    def start(self, source: Source, fallback_video=None) -> None:
        dev = self.cameras.device(source.cam_id)
        if not dev:
            self.notify.emit("The camera is not connected", "warning")
            return
        key = source.key
        if self.is_recording(key):
            return
        label = self.cameras.label(source.cam_id, source.channel) + (" tele" if source.lens == TELE else "")
        directory = self.camera_dir(source.cam_id, source.channel, "Recordings")
        protocol = self.settings.protocol
        if not rec_mod.ffmpeg_path():
            if fallback_video is not None:
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{rec_mod.safe_name(label)}_{dt.datetime.now():%Y-%m-%d_%H-%M-%S}.mkv"
                if fallback_video.start_stream_record(str(path)):
                    self.fallback[key] = fallback_video
                    self.started[key] = time.monotonic()
                    self.changed.emit(key)
                    self.notify.emit(f"Recording {label} (install FFmpeg for more reliable recording)", "info")
                    return
            self.notify.emit("Recording needs FFmpeg: install the 'ffmpeg' package", "error")
            return

        def resolve():
            return dev.probe_stream(source.channel, source.lens, MAIN, protocol)

        def begin(url: str) -> None:
            if self.is_recording(key):
                return
            r = rec_mod.Recorder(url, directory, label, self.settings.record_format)
            try:
                r.start()
            except (RuntimeError, OSError) as exc:
                self.notify.emit(str(exc), "error")
                return
            self.manual[key] = r
            self.started[key] = time.monotonic()
            self.changed.emit(key)
            self.notify.emit(f"Recording {label} to {directory}", "success")

        worker.run(resolve, begin, lambda exc: self.notify.emit(friendly_error(exc), "error"))

    def stop(self, key: str) -> Path | None:
        path = None
        if key in self.manual:
            r = self.manual.pop(key)
            path = r.current_file
            worker.run(r.stop)
        if key in self.fallback:
            video = self.fallback.pop(key)
            try:
                video.stop_stream_record()
            except RuntimeError:
                pass
        self.started.pop(key, None)
        self.changed.emit(key)
        if path:
            self.notify.emit(f"Saved {path.name}", "success")
        return path

    def toggle(self, source: Source, fallback_video=None) -> None:
        if self.is_recording(source.key):
            self.stop(source.key)
        else:
            self.start(source, fallback_video)

    def elapsed_start(self, key: str) -> float:
        r = self.manual.get(key) or self.continuous.get(key)
        if r and r.started_at:
            return r.started_at
        return self.started.get(key, 0.0)

    def _check(self) -> None:
        for key, r in list(self.manual.items()):
            if not r.running:
                self.manual.pop(key, None)
                self.started.pop(key, None)
                self.changed.emit(key)
                if r.error:
                    self.notify.emit(f"Recording stopped: {r.error}", "error")

    # -- continuous
    def sync_continuous(self) -> None:
        if not rec_mod.ffmpeg_path():
            return
        wanted: dict[str, Source] = {}
        for e in self.cameras.ordered():
            if not (e.cfg.continuous_record and e.online):
                continue
            for ch in e.device.channels:
                if not ch.online or ch.index in e.cfg.hidden_channels:
                    continue
                for lens in ch.lenses:
                    s = Source(e.cfg.id, ch.index, lens)
                    wanted[s.key] = s
        for key in list(self.continuous):
            if key not in wanted:
                worker.run(self.continuous.pop(key).stop)
                self.changed.emit(key)
        for key, s in wanted.items():
            if key in self.continuous:
                continue
            dev = self.cameras.device(s.cam_id)
            if not dev:
                continue
            label = self.cameras.label(s.cam_id, s.channel) + (" tele" if s.lens == TELE else "")
            directory = self.camera_dir(s.cam_id, s.channel, "Continuous")
            r = rec_mod.Recorder(dev.stream_url(s.channel, s.lens, MAIN, self.settings.protocol), directory, label,
                                 self.settings.record_format, segment_minutes=max(1, self.settings.segment_minutes))
            try:
                r.start()
            except (RuntimeError, OSError) as exc:
                self.notify.emit(str(exc), "error")
                continue
            self.continuous[key] = r
            self.changed.emit(key)

    def apply_retention(self) -> None:
        days = self.settings.retention_days
        if days <= 0:
            return
        base = self.settings.videos()

        def work():
            removed = 0
            if base.is_dir():
                for cam_dir in base.iterdir():
                    removed += rec_mod.cleanup_old_files(cam_dir / "Continuous", days)
            return removed

        worker.run(work)

    def stop_all(self) -> None:
        for key in list(self.manual):
            self.manual.pop(key).stop()
        for key in list(self.continuous):
            self.continuous.pop(key).stop()
        for key in list(self.fallback):
            try:
                self.fallback.pop(key).stop_stream_record()
            except RuntimeError:
                pass


_job_ids = itertools.count(1)


@dataclass
class DownloadJob:
    cam_id: str
    camera: str
    rec: Recording
    path: Path
    id: int = field(default_factory=lambda: next(_job_ids))
    state: str = "queued"      # queued | running | done | failed | cancelled
    done_bytes: int = 0
    total: int = 0
    error: str = ""
    cancel: bool = False
    speed: float = 0.0
    _t0: float = 0.0

    @property
    def fraction(self) -> float:
        total = self.total or self.rec.size
        return min(1.0, self.done_bytes / total) if total else 0.0


class DownloadManager(QObject):
    job_added = Signal(object)
    job_changed = Signal(object)
    notify = Signal(str, str)

    MAX_PARALLEL = 2

    def __init__(self, cameras: CameraManager):
        super().__init__()
        self.cameras = cameras
        self.jobs: list[DownloadJob] = []
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)

    def enqueue(self, cam_id: str, rec: Recording, path: Path) -> DownloadJob | None:
        for j in self.jobs:
            if j.rec.name == rec.name and j.cam_id == cam_id and j.state in ("queued", "running"):
                return None
        job = DownloadJob(cam_id, self.cameras.label(cam_id, rec.channel), rec, path)
        self.jobs.append(job)
        self.job_added.emit(job)
        self._pump()
        return job

    def cancel(self, job: DownloadJob) -> None:
        job.cancel = True
        if job.state == "queued":
            job.state = "cancelled"
            self.job_changed.emit(job)
            self._pump()

    def clear_finished(self) -> None:
        self.jobs = [j for j in self.jobs if j.state in ("queued", "running")]

    def active(self) -> int:
        return sum(1 for j in self.jobs if j.state in ("queued", "running"))

    def _pump(self) -> None:
        running = [j for j in self.jobs if j.state == "running"]
        busy_cams = {j.cam_id for j in running}
        for job in self.jobs:
            if len(running) >= self.MAX_PARALLEL:
                break
            if job.state != "queued" or job.cam_id in busy_cams:
                continue
            dev = self.cameras.device(job.cam_id)
            if not dev:
                job.state, job.error = "failed", "The camera is not connected"
                self.job_changed.emit(job)
                continue
            job.state = "running"
            job._t0 = time.monotonic()
            running.append(job)
            busy_cams.add(job.cam_id)
            self.job_changed.emit(job)

            def work(j=job, d=dev):
                def progress(done: int, total: int) -> None:
                    j.done_bytes, j.total = done, total
                    elapsed = time.monotonic() - j._t0
                    j.speed = done / elapsed if elapsed > 0 else 0.0

                d.download(j.rec, str(j.path), progress, lambda: j.cancel)
                return j

            worker.run(work, self._finished, lambda exc, j=job: self._failed(j, exc))
        if running and not self._timer.isActive():
            self._timer.start()

    def _finished(self, job: DownloadJob) -> None:
        job.state = "done"
        try:
            job.done_bytes = job.total = os.path.getsize(job.path)
        except OSError:
            pass
        self.job_changed.emit(job)
        self.notify.emit(f"Downloaded {job.path.name}", "success")
        self._pump()

    def _failed(self, job: DownloadJob, exc: Exception) -> None:
        if isinstance(exc, Cancelled) or job.cancel:
            job.state = "cancelled"
        else:
            job.state, job.error = "failed", friendly_error(exc)
            self.notify.emit(f"Download failed: {job.error}", "error")
        self.job_changed.emit(job)
        self._pump()

    def _tick(self) -> None:
        running = [j for j in self.jobs if j.state == "running"]
        for j in running:
            self.job_changed.emit(j)
        if not running:
            self._timer.stop()
