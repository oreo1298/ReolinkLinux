"""What a Reolink HTTP-FLV stream carries: read the FLV header and the first video tag.

Reolink's RTSP server drops data under load (the picture freezes every few seconds and
smears until the next keyframe, worst on a TrackMix), while its HTTP-FLV stream of the
same video stays clean. Recent firmware sends H.265 over FLV too, either with the legacy
codec id 12 that FFmpeg reads since 8.0 (libavformat 62) or as Enhanced FLV ("hvc1"),
read since FFmpeg 6.1 (libavformat 60.16). ``probe`` tells which one a stream uses, so
FLV is only chosen when this PC can play it.
"""

from __future__ import annotations

import socket
import ssl
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .api import _ssl_context

CODEC_IDS = {7: "h264", 12: "h265"}
FOURCCS = {b"avc1": "h264", b"hvc1": "h265", b"av01": "av1", b"vp09": "vp9"}

# libavformat versions that demux each form of H.265 in FLV
LAVF_HEVC_ENHANCED = (60, 16)   # FFmpeg 6.1
LAVF_HEVC_LEGACY = (62, 0)      # FFmpeg 8.0


@dataclass
class FlvProbe:
    status: int = 0             # HTTP status, 0 when nothing answered
    codec: str = ""             # video codec of the first video tag: h264 / h265 / …; "" if none came
    enhanced: bool = False      # Enhanced FLV (FourCC) rather than a classic codec id
    has_audio: bool = False
    error: str = ""

    # the same surface as rtsp.DescribeResult, for the diagnostics report
    challenges = ()
    refused = False

    @property
    def ok(self) -> bool:
        return bool(self.codec)

    def describe(self) -> str:
        if self.codec:
            form = "Enhanced FLV" if self.enhanced else "classic FLV"
            return f"FLV {self.codec.upper()} ({form}){', audio' if self.has_audio else ''}"
        if self.status and self.status != 200:
            return f"HTTP {self.status}"
        return self.error or "no video"


def playable(probe: FlvProbe, lavf: tuple[int, int]) -> bool:
    """Can a libavformat of version ``lavf`` (major, minor) demux this stream's video?"""
    if probe.codec == "h264":
        return True
    if probe.codec == "h265":
        return tuple(lavf) >= (LAVF_HEVC_ENHANCED if probe.enhanced else LAVF_HEVC_LEGACY)
    return False


def parse(data: bytes) -> FlvProbe:
    """Inspect the start of an FLV stream (header, then tags until the first video tag)."""
    result = FlvProbe(status=200)
    if len(data) < 9 or data[:3] != b"FLV":
        result.error = "not an FLV stream" if data else "empty answer"
        return result
    offset = struct.unpack(">I", data[5:9])[0]
    pos = max(9, offset)
    while pos + 15 <= len(data):
        tag_type = data[pos + 4] & 0x1F
        size = int.from_bytes(data[pos + 5:pos + 8], "big")
        body = data[pos + 15:pos + 15 + size]
        if tag_type == 8:
            result.has_audio = True
        elif tag_type == 9 and body:
            first = body[0]
            if first & 0x80:                       # Enhanced FLV: FourCC after the header byte
                if len(body) < 5:
                    break                          # the rest of the tag has not arrived yet
                result.enhanced = True
                fourcc = bytes(body[1:5])
                result.codec = FOURCCS.get(fourcc, fourcc.decode("ascii", "replace"))
            else:
                cid = first & 0x0F
                result.codec = CODEC_IDS.get(cid, f"codec id {cid}")
            return result
        pos += 15 + size
    result.error = "no video in the first part of the stream"
    return result


def probe(url: str, timeout: float = 8.0, max_bytes: int = 1 << 20) -> FlvProbe:
    """Open an HTTP(S)-FLV URL and report what it carries (reads at most ``max_bytes``)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=_ssl_context()))
    deadline = time.monotonic() + timeout
    data = b""
    try:
        with opener.open(url, timeout=timeout) as resp:
            status = resp.status
            while len(data) < max_bytes and time.monotonic() < deadline:
                chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                if not chunk:
                    break
                data += chunk
                found = parse(data)
                if found.codec or found.error == "not an FLV stream":
                    found.status = status
                    return found
    except urllib.error.HTTPError as exc:
        return FlvProbe(status=exc.code, error=f"HTTP {exc.code}")
    except ValueError:
        return FlvProbe(error="invalid URL")       # never echo the URL: it holds the password
    except (urllib.error.URLError, OSError, socket.timeout, ssl.SSLError) as exc:
        if not data:
            reason = getattr(exc, "reason", None) or exc
            return FlvProbe(error=f"no answer ({reason})" if str(reason) else "no answer")
    found = parse(data)
    if not found.codec and data and time.monotonic() >= deadline:
        found.error = "no video within the time limit"
    return found
