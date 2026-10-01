"""Record live streams to this PC with FFmpeg (stream copy, no re-encoding).

A ``Recorder`` owns one ``ffmpeg`` process. It restarts it automatically when the
camera drops the connection (reboot, network glitch), and can split the recording into
fixed-length files for continuous, NVR-style recording with a retention period.
Stopping sends ``q`` so FFmpeg closes the file properly; files are written as
fragmented MP4 (or Matroska) so even a crash leaves a playable file.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

_FFMPEG_MAJOR: int | None = None
_FFMPEG_LAVF: tuple[int, int] | None = None


def ffmpeg_path() -> str | None:
    return shutil.which(os.environ.get("REOLINKLINUX_FFMPEG", "ffmpeg"))


def _read_version() -> None:
    global _FFMPEG_MAJOR, _FFMPEG_LAVF
    _FFMPEG_MAJOR, _FFMPEG_LAVF = 0, (0, 0)
    exe = ffmpeg_path()
    if not exe:
        return
    try:
        out = subprocess.run([exe, "-hide_banner", "-version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return
    m = re.search(r"ffmpeg version n?(\d+)", out)
    _FFMPEG_MAJOR = int(m.group(1)) if m else 7
    m = re.search(r"libavformat\s+(\d+)\.\s*(\d+)", out)
    if m:
        _FFMPEG_LAVF = (int(m.group(1)), int(m.group(2)))


def ffmpeg_major() -> int:
    if _FFMPEG_MAJOR is None:
        _read_version()
    return _FFMPEG_MAJOR or 0


def ffmpeg_lavf() -> tuple[int, int]:
    """(major, minor) of the libavformat in the ``ffmpeg`` program, (0, 0) without FFmpeg."""
    if _FFMPEG_LAVF is None:
        _read_version()
    return _FFMPEG_LAVF or (0, 0)


def safe_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in name).strip(" .")
    return cleaned or "camera"


def build_command(url: str, output: str, fmt: str = "mp4", segment_seconds: int = 0) -> list[str]:
    exe = ffmpeg_path() or "ffmpeg"
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y"]
    demo = url.startswith("av://lavfi:")
    if demo:
        cmd += ["-re", "-f", "lavfi", "-i", url[len("av://lavfi:"):]]
    else:
        if url.startswith("rtsp://"):
            cmd += ["-rtsp_transport", "tcp"]
            cmd += ["-timeout" if ffmpeg_major() >= 5 else "-stimeout", "15000000"]
        else:
            cmd += ["-rw_timeout", "15000000"]
        cmd += ["-fflags", "+genpts", "-i", url]
    cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    if demo:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
                "-g", "30"]
    else:
        cmd += ["-c:v", "copy"]
    cmd += ["-c:a", "aac", "-b:a", "64k"]
    movflags = "+frag_keyframe+empty_moov+default_base_moof"
    container = "matroska" if fmt == "mkv" else "mp4"
    if segment_seconds > 0:
        cmd += ["-f", "segment", "-segment_time", str(int(segment_seconds)), "-segment_format", container,
                "-reset_timestamps", "1", "-strftime", "1"]
        if container == "mp4":
            cmd += ["-segment_format_options", f"movflags={movflags}"]
    else:
        if container == "mp4":
            cmd += ["-movflags", movflags]
        cmd += ["-f", container]
    cmd.append(output)
    return cmd


class Recorder:
    """Records one stream until stopped, reconnecting when the stream drops."""

    RESTART_DELAYS = (3, 5, 10, 20, 30)

    def __init__(self, url: str, directory: Path, base_name: str, fmt: str = "mp4",
                 segment_minutes: int = 0, auto_restart: bool = True):
        self.url = url
        self.directory = Path(directory)
        self.base_name = safe_name(base_name)
        self.fmt = "mkv" if fmt == "mkv" else "mp4"
        self.segment_seconds = max(0, int(segment_minutes) * 60)
        self.auto_restart = auto_restart
        self.files: list[Path] = []
        self.error = ""
        self.started_at: float | None = None
        self.restarts = 0
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- public API
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    @property
    def active(self) -> bool:
        """True while FFmpeg is actually writing (not waiting to reconnect)."""
        proc = self._proc
        return proc is not None and proc.poll() is None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at if self.started_at else 0.0

    @property
    def bytes_written(self) -> int:
        total = 0
        for f in list(self.files):
            try:
                total += f.stat().st_size
            except OSError:
                pass
        if self.segment_seconds:
            for f in self.directory.glob(f"{self.base_name}_*.{self.fmt}"):
                if f not in self.files:
                    try:
                        if f.stat().st_mtime >= (time.time() - self.elapsed - 5):
                            total += f.stat().st_size
                    except OSError:
                        pass
        return total

    @property
    def current_file(self) -> Path | None:
        return self.files[-1] if self.files else None

    def start(self) -> None:
        if not ffmpeg_path():
            raise RuntimeError("FFmpeg is not installed; install the 'ffmpeg' package to record to this PC")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, name=f"recorder-{self.base_name}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 8.0) -> None:
        self._stop.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write(b"q")
                    proc.stdin.flush()
                    proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self._thread:
            self._thread.join(timeout=timeout)

    # -- worker
    def _output(self) -> str:
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        if self.segment_seconds:
            return str(self.directory / f"{self.base_name}_%Y-%m-%d_%H-%M-%S.{self.fmt}")
        path = self.directory / f"{self.base_name}_{stamp}.{self.fmt}"
        self.files.append(path)
        return str(path)

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            cmd = build_command(self.url, self._output(), self.fmt, self.segment_seconds)
            launched = time.monotonic()
            try:
                self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                              stderr=subprocess.PIPE)
            except OSError as exc:
                self.error = str(exc)
                return
            # Not communicate(): it would close stdin, which stop() uses to send "q".
            err = self._proc.stderr.read() if self._proc.stderr else b""
            code = self._proc.wait()
            text = (err or b"").decode("utf-8", "replace").strip()
            if text:
                self.error = _redact(text.splitlines()[-1])
            # Drop empty files left by a failed connection.
            if not self.segment_seconds and self.files:
                last = self.files[-1]
                try:
                    if last.stat().st_size < 1024:
                        last.unlink()
                        self.files.pop()
                except OSError:
                    self.files.pop()
            if self._stop.is_set() or not self.auto_restart:
                if code not in (0, 255) and not self._stop.is_set() and not self.error:
                    self.error = f"ffmpeg exited with code {code}"
                break
            if time.monotonic() - launched > 60:
                attempt = 0
            delay = self.RESTART_DELAYS[min(attempt, len(self.RESTART_DELAYS) - 1)]
            attempt += 1
            self.restarts += 1
            self._stop.wait(delay)
        self._proc = None


def _redact(text: str) -> str:
    return re.sub(r"(rtsp|https?)://[^/@\s]+@", r"\1://***@", text)


def cleanup_old_files(directory: Path, days: int, patterns: tuple[str, ...] = ("*.mp4", "*.mkv")) -> int:
    """Delete continuous-recording files older than ``days`` days; returns how many."""
    if days <= 0 or not directory.is_dir():
        return 0
    limit = time.time() - days * 86400
    removed = 0
    for pattern in patterns:
        for f in directory.glob(pattern):
            try:
                if f.is_file() and f.stat().st_mtime < limit:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    return removed
