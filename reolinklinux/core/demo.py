"""Simulated cameras for demo mode: try the whole app without any hardware.

A demo device behaves like a real one (capabilities, PTZ, presets, lights, detection,
SD-card recordings) but keeps its state in memory. Its video comes from FFmpeg's
``lavfi`` sources through mpv: a still scene image (drawn by the GUI) with a running
clock, or a test pattern when no image is available.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import shutil
import subprocess
import time
from typing import Callable

from .device import Device
from .errors import ApiError, Cancelled
from .models import MAIN, SUB, TELE, WIDE, Capabilities, Channel, DeviceInfo, Preset, Recording, StreamInfo, Trigger

DEMO_KINDS = {
    "duo2": dict(name="Driveway", model="Reolink Duo 2 PoE", hw="IPC_529SD78MP", main=(4608, 1728), sub=(1536, 576),
                 fps=20, h265=True),
    "trackmix": dict(name="Back Garden", model="Reolink TrackMix PoE", hw="IPC_560SD88MP", main=(3840, 2160),
                     sub=(896, 512), fps=25, h265=True),
    "rlc811a": dict(name="Front Door", model="RLC-811A", hw="IPC_523128M8MP", main=(3840, 2160), sub=(640, 360),
                    fps=25, h265=True),
}

# Set by the GUI: returns a PNG path for (kind, lens) or "" (then a test pattern is used).
scene_provider: Callable[[str, int], str] | None = None


def _escape(path: str) -> str:
    return path.replace("\\", "\\\\").replace(":", "\\:").replace(",", "\\,").replace("'", "\\'")


class DemoDevice(Device):
    demo = True

    def __init__(self, kind: str):
        super().__init__(f"demo-{kind}", "admin", "")
        self.kind = kind if kind in DEMO_KINDS else "rlc811a"
        self.spec = DEMO_KINDS[self.kind]
        self._recordings: dict[tuple[int, dt.date], list[Recording]] = {}

    @property
    def host(self) -> str:
        return f"demo:{self.kind}"

    # ------------------------------------------------------------------ connect
    def connect(self, probe_streams: bool = True) -> None:
        time.sleep(0.2)
        s = self.spec
        self.info = DeviceInfo(name=s["name"], model=s["model"], hardware=s["hw"], firmware="v3.1.0.4054_2408291845",
                               serial="DEMO" + self.kind.upper(), uid="95270000DEMO" + self.kind.upper()[:4],
                               device_type="IPC", channel_count=1, mac="ec:71:db:00:00:01")
        caps = Capabilities(main_h265=s["h265"], ai_types=["people", "vehicle", "dog_cat"], siren=True,
                            ir_lights=True, spotlight=self.kind != "rlc811a")
        ch = Channel(index=0, name=s["name"], model=s["model"], caps=caps,
                     main=StreamInfo(*s["main"], fps=s["fps"], bitrate=8192, codec="h265"),
                     sub=StreamInfo(*s["sub"], fps=15, bitrate=512, codec="h264"))
        ch.lights.ir_state = "Auto"
        ch.lights.spotlight_on = False
        ch.lights.spotlight_brightness = 80
        if self.kind == "trackmix":
            caps.pan_tilt = caps.optical_zoom = caps.presets = caps.patrol = caps.guard = True
            caps.auto_track = caps.telephoto = caps.calibrate = caps.ptz_speed = True
            ch.presets = [Preset(1, "Gate"), Preset(2, "Patio"), Preset(3, "Shed")]
            ch.patrols = [Preset(0, "Garden tour")]
            ch.zoom.zoom_min, ch.zoom.zoom_max, ch.zoom.zoom_pos = 0, 33, 0
            ch.auto_track_on = True
            ch.guard_enabled = True
        elif self.kind == "rlc811a":
            caps.optical_zoom = caps.focus = caps.auto_focus = caps.presets = True
            ch.zoom.zoom_min, ch.zoom.zoom_max, ch.zoom.focus_max = 0, 33, 223
            ch.auto_focus_on = True
        self.channels = [ch]
        self.hdd = [{"capacity": 244140, "size": 61035, "mount": 1, "format": 1, "storageType": 2}]
        self.clock = dt.datetime.now()
        self.clock_read_at = dt.datetime.now()
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def poll(self, channels=None, lights: bool = False) -> None:
        ch = self.channels[0]
        # Occasional synthetic detections so the overlay badges can be seen.
        beat = (int(time.time() / 4) + {"duo2": 0, "trackmix": 3, "rlc811a": 6}.get(self.kind, 0)) % 9
        ch.detection.motion = beat in (1, 2, 5)
        ch.detection.ai = {"people": beat == 2, "vehicle": beat == 5 and self.kind == "duo2",
                           "dog_cat": beat == 7}

    # ------------------------------------------------------------------ streams
    def _lavfi(self, lens: int, quality: str) -> str:
        w, h = self.spec["main"] if quality == MAIN else self.spec["sub"]
        if lens == TELE:
            w, h = (2560, 1440) if quality == MAIN else (896, 512)
        # Keep the demo light on the CPU: at most ~1080p of synthetic pixels.
        scale = min(1.0, 1920 / w)
        w, h = int(w * scale) // 16 * 16, int(h * scale) // 16 * 16
        fps = self.spec["fps"] if quality == MAIN else 15
        label = self.spec["name"].upper() + (" · TELE" if lens == TELE else "")
        clock = "%{localtime\\:%Y-%m-%d %H\\\\\\:%M\\\\\\:%S}"
        size = max(14, h // 26)
        text = (f"drawtext=text='{label}   {clock}':x={size}:y=h-{size * 2}:fontsize={size}:fontcolor=white"
                f":borderw=2:bordercolor=black@0.55")
        scene = scene_provider(self.kind, lens) if scene_provider else ""
        if scene and os.path.exists(scene):
            src = f"movie='{_escape(scene)}':loop=0,setpts=N/({fps}*TB),scale={w}:{h},setsar=1,fps={fps}"
        else:
            src = f"testsrc2=size={w}x{h}:rate={fps}"
        # Explicit BT.709 tags: frames converted from RGB would otherwise be mislabelled for the renderer.
        return (f"av://lavfi:{src},{text},scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
                f"setparams=range=tv:colorspace=bt709:color_primaries=bt709:color_trc=bt709")

    def stream_candidates(self, index: int, lens: int = WIDE, quality: str = MAIN, protocol: str = "rtsp") -> list[str]:
        return [self._lavfi(lens, quality)]

    def stream_url(self, index: int, lens: int = WIDE, quality: str = MAIN, protocol: str = "rtsp") -> str:
        return self._lavfi(lens, quality)

    def probe_stream(self, index: int, lens: int, quality: str, protocol: str = "rtsp") -> str:
        return self._lavfi(lens, quality)

    def probe_streams(self) -> None:
        return None

    # ------------------------------------------------------------------ controls
    def ptz(self, index: int, op: str, speed: int | None = None, preset_id: int | None = None) -> None:
        ch = self.channel(index)
        if op == "ZoomInc":
            ch.zoom.zoom_pos = min(ch.zoom.zoom_max, ch.zoom.zoom_pos + 3)
        elif op == "ZoomDec":
            ch.zoom.zoom_pos = max(ch.zoom.zoom_min, ch.zoom.zoom_pos - 3)

    def ptz_stop(self, index: int) -> None:
        return None

    def save_preset(self, index: int, preset_id: int, name: str) -> None:
        ch = self.channel(index)
        ch.presets = [p for p in ch.presets if p.id != preset_id] + [Preset(preset_id, name)]
        ch.presets.sort(key=lambda p: p.id)

    def delete_preset(self, index: int, preset_id: int) -> None:
        ch = self.channel(index)
        ch.presets = [p for p in ch.presets if p.id != preset_id]

    def refresh_presets(self, index: int) -> list[Preset]:
        return self.channel(index).presets

    def set_zoom(self, index: int, position: int) -> None:
        self.channel(index).zoom.zoom_pos = int(position)

    def set_focus(self, index: int, position: int) -> None:
        self.channel(index).zoom.focus_pos = int(position)

    def refresh_zoom(self, index: int) -> None:
        return None

    def set_auto_focus(self, index: int, enable: bool) -> None:
        self.channel(index).auto_focus_on = enable

    def set_auto_track(self, index: int, enable: bool) -> None:
        self.channel(index).auto_track_on = enable

    def calibrate(self, index: int) -> None:
        return None

    def set_guard_here(self, index: int, enable: bool = True, timeout: int = 60) -> None:
        self.channel(index).guard_enabled = enable

    def goto_guard(self, index: int) -> None:
        return None

    def set_ir(self, index: int, state: str) -> None:
        self.channel(index).lights.ir_state = state

    def set_spotlight(self, index: int, on: bool | None = None, brightness: int | None = None) -> None:
        ch = self.channel(index)
        if on is not None:
            ch.lights.spotlight_on = on
        if brightness is not None:
            ch.lights.spotlight_brightness = brightness

    def siren(self, index: int, on: bool = True, times: int = 1) -> None:
        return None

    def snapshot(self, index: int, lens: int = WIDE) -> bytes:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ApiError("Snap", None, "demo snapshots need ffmpeg")
        src = self._lavfi(lens, MAIN).removeprefix("av://lavfi:")
        out = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", src,
                              "-frames:v", "1", "-f", "image2", "-c:v", "mjpeg", "-q:v", "3", "-"],
                             capture_output=True, timeout=30)
        if out.returncode != 0 or not out.stdout:
            raise ApiError("Snap", None, out.stderr.decode(errors="replace").strip() or "ffmpeg failed")
        return out.stdout

    def reboot(self) -> None:
        return None

    def read_clock(self):
        self.clock = dt.datetime.now()
        self.clock_read_at = dt.datetime.now()
        return self.clock

    def sync_clock(self, moment=None) -> None:
        self.read_clock()

    # ------------------------------------------------------------------ recordings
    def _day(self, index: int, day: dt.date, stream: str, lens: int) -> list[Recording]:
        key = (index * 10 + lens, day)
        if key not in self._recordings:
            rng = random.Random(f"{self.kind}{day.isoformat()}{lens}")
            recs = []
            moment = dt.datetime.combine(day, dt.time(0, 0)) + dt.timedelta(minutes=rng.randint(5, 50))
            end_of_day = dt.datetime.combine(day, dt.time(23, 59, 59))
            now = dt.datetime.now()
            choices = [Trigger.PERSON | Trigger.MOTION, Trigger.VEHICLE | Trigger.MOTION, Trigger.MOTION,
                       Trigger.ANIMAL | Trigger.MOTION, Trigger.MOTION, Trigger.PERSON | Trigger.MOTION]
            while moment < end_of_day and moment < now:
                length = dt.timedelta(seconds=rng.randint(25, 240))
                trig = rng.choice(choices)
                if lens == TELE:
                    trig |= Trigger.PERSON
                name = (f"Mp4Record/{day:%Y-%m-%d}/RecM0{'2' if lens == TELE else '1'}_"
                        f"{moment:%Y%m%d_%H%M%S}_{(moment + length):%H%M%S}_demo.mp4")
                recs.append(Recording(channel=index, name=name, start=moment, end=moment + length,
                                      size=int(length.total_seconds() * 780_000), stream=MAIN, lens=lens,
                                      triggers=trig))
                moment += length + dt.timedelta(minutes=rng.choice([3, 7, 12, 25, 40, 55, 80]))
            self._recordings[key] = recs
        recs = self._recordings[key]
        if stream == SUB:
            return [Recording(r.channel, r.name.replace("RecM", "RecS"), r.start, r.end, r.size // 10, SUB, r.lens,
                              triggers=r.triggers) for r in recs]
        return recs

    def recording_days(self, index: int, year: int, month: int, stream: str = MAIN, lens: int = WIDE) -> set[int]:
        today = dt.date.today()
        days = set()
        for d in range(1, 32):
            try:
                day = dt.date(year, month, d)
            except ValueError:
                break
            if today - dt.timedelta(days=21) <= day <= today:
                days.add(d)
        return days

    def recordings(self, index: int, day: dt.date, stream: str = MAIN, lens: int = WIDE) -> list[Recording]:
        if day not in {dt.date(day.year, day.month, d) for d in self.recording_days(index, day.year, day.month)}:
            return []
        return list(self._day(index, day, stream, lens))

    def playback_urls(self, rec: Recording) -> list[str]:
        seconds = max(1, int(rec.duration.total_seconds()))
        src = self._lavfi(rec.lens, rec.stream).removeprefix("av://lavfi:")
        # A finite clip with a timestamp that counts from the recording's start time.
        src = src.replace("%{localtime\\:%Y-%m-%d %H\\\\\\:%M\\\\\\:%S}",
                          f"%{{pts\\:localtime\\:{int(rec.start.timestamp())}}}")
        return [f"av://lavfi:{src},trim=duration={seconds}"]

    def download(self, rec: Recording, path: str, progress=None, cancelled=None) -> int:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ApiError("Download", None, "demo downloads need ffmpeg")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        seconds = min(20, max(2, int(rec.duration.total_seconds())))
        src = self.playback_urls(rec)[0].removeprefix("av://lavfi:")
        tmp = path + ".part"
        proc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src,
                                 "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                                 "-f", "mp4", tmp], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        start = time.monotonic()
        while proc.poll() is None:
            if cancelled and cancelled():
                proc.kill()
                proc.wait()
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise Cancelled("download cancelled")
            if progress:
                progress(int(min(0.95, (time.monotonic() - start) / (seconds * 0.6 + 1)) * rec.size), rec.size)
            time.sleep(0.2)
        if proc.returncode != 0:
            err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            raise ApiError("Download", None, err.strip() or "ffmpeg failed")
        os.replace(tmp, path)
        size = os.path.getsize(path)
        if progress:
            progress(rec.size, rec.size)
        return size

    def storage_summary(self) -> str:
        return "SD card: 178.8 of 238.4 GB used"
