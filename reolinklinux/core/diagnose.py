"""A plain-text report of what a camera reports and how its streams were chosen.

Used by ``reolinkctl diagnose`` and the Diagnostics button in the app, so a problem
("only Fluent works", "the picture is garbled") can be investigated from one paste.
Passwords and session tokens are removed from everything in the report.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import urllib.parse

from .. import __app_name__, __version__
from . import flv, recorder
from .device import Device
from .models import MAIN, SUB, TELE

_ABILITY_KEYS = ("ptzType", "ptzPreset", "supportAutoTrackStream", "aiTrack", "mainEncType", "supportDigitalZoom",
                 "ledControl", "floodLight", "supportFLswitch", "alarmAudio", "supportAudioAlarm", "recReplay",
                 "battery")


def redact(text: str) -> str:
    text = re.sub(r"((?:rtsp|rtmp|https?)://[^:/@\s]+:)[^@\s]*@", r"\1***@", text)
    return re.sub(r"(password|token)=[^&\s\"']*", r"\1=***", text)


def ffprobe(url: str, timeout: float = 20.0) -> str:
    """What FFmpeg sees in a stream: codec, size, frame rate (one line per stream)."""
    exe = shutil.which("ffprobe")
    if not exe:
        return "ffprobe not installed"
    cmd = [exe, "-v", "error", "-hide_banner"]
    if url.startswith("rtsp://"):
        cmd += ["-rtsp_transport", "tcp", "-timeout" if recorder.ffmpeg_major() >= 5 else "-stimeout", "10000000"]
    cmd += ["-show_entries", "stream=codec_type,codec_name,profile,width,height,pix_fmt,avg_frame_rate",
            "-of", "compact=p=0", "-i", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "ffprobe: no answer within 20 s"
    except OSError as exc:
        return f"ffprobe: {exc}"
    text = (out.stdout.strip() or out.stderr.strip() or f"exit code {out.returncode}").splitlines()
    return redact(" | ".join(line.strip() for line in text[:6]))


def report(dev: Device, run_ffprobe: bool = True, extra: list[str] | None = None) -> str:
    lines = [f"{__app_name__} {__version__} diagnostics",
             f"system: Python {platform.python_version()}, {platform.system()} {platform.release()}, "
             f"ffmpeg {recorder.ffmpeg_major() or 'missing'}"]
    lines += extra or []
    info = dev.info
    lines += ["", "== device",
              f"model {info.model!r}, name {info.name!r}, type {info.device_type}, channels {info.channel_count}",
              f"firmware {info.firmware}, hardware {info.hardware}, build {info.build_day or '-'}",
              f"api {dev.client.base_url if not dev.demo else 'demo'}",
              f"ports {json.dumps(dev.ports)}",
              f"stream protocol {getattr(dev, 'protocol', '-')}; libavformat for playback "
              f"{'.'.join(map(str, getattr(dev, 'lavf', (0, 0))))} (H.265 over FLV needs 62.0, or 60.16 for "
              f"Enhanced FLV)"]
    if not dev.probe_log and not dev.demo:
        dev.probe_streams()
    for ch in dev.channels:
        if not ch.online:
            lines += ["", f"== channel {ch.index} ({ch.name}): offline"]
            continue
        chn = dev._chn_abilities(ch.index)
        abil = {k: chn[k].get("ver") for k in _ABILITY_KEYS if isinstance(chn.get(k), dict)}
        caps = {k: v for k, v in ch.caps.__dict__.items() if v and k != "ai_types"}
        lines += ["", f"== channel {ch.index}: {ch.name!r} model {ch.model!r}",
                  f"abilities {json.dumps(abil)}",
                  f"capabilities {', '.join(caps) or 'none'}; ai {', '.join(ch.caps.ai_types) or '-'}",
                  f"clear  {ch.main.resolution or '?'} {ch.main.codec or '?'} {ch.main.fps} fps {ch.main.bitrate} kbps",
                  f"fluent {ch.sub.resolution or '?'} {ch.sub.codec or '?'} {ch.sub.fps} fps {ch.sub.bitrate} kbps",
                  f"reported rtsp: {redact(ch.rtsp_main) or '-'} / {redact(ch.rtsp_sub) or '-'}"]
        if ch.caps.telephoto:
            lines += [f"tele clear  {ch.tele_main.resolution or '?'} {ch.tele_main.codec or '?'}",
                      f"tele fluent {ch.tele_sub.resolution or '?'} {ch.tele_sub.codec or '?'}",
                      f"reported tele rtsp: {redact(ch.tele_rtsp_main) or '-'} / {redact(ch.tele_rtsp_sub) or '-'}"]
        for lens in ch.lenses:
            for quality in (MAIN, SUB):
                label = f"{'tele ' if lens == TELE else ''}{'clear' if quality == MAIN else 'fluent'}"
                rec = dev.probe_log.get((ch.index, lens, quality))
                chosen = rec.chosen if rec else dev.stream_url(ch.index, lens, quality)
                state = "confirmed" if rec and rec.confirmed else "NOT confirmed"
                lines.append(f"[{label}] plays {redact(chosen)} ({state})")
                for url, result in (rec.tried if rec else []):
                    if isinstance(result, flv.FlvProbe):
                        stream = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("stream", ["?"])[0]
                        note = " (too new for this FFmpeg)" if result.ok and not flv.playable(result, dev.lavf) else ""
                        lines.append(f"    FLV {stream}: {result.describe()}{note}")
                        continue
                    lines.append(f"    DESCRIBE {redact(url).rsplit('/', 1)[-1]}: {result.describe()}")
                    if result.challenges and not result.ok:
                        lines.append(f"      challenges: {redact('; '.join(result.challenges))}")
                if run_ffprobe and not dev.demo:
                    lines.append(f"    ffprobe: {ffprobe(chosen)}")
    return "\n".join(lines)
