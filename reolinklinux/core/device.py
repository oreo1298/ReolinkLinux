"""A connected Reolink device (camera, NVR or Home Hub) and everything it can do.

``Device.connect()`` logs in, reads the device and channel information in two batched
requests, works out each channel's capabilities and picks working stream URLs.
All methods block, so the GUI calls them from worker threads.
"""

from __future__ import annotations

import datetime as dt
import os
import random
import string
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from . import rtsp
from .api import Client, check, ok
from .errors import ApiError, NotSupported, ReolinkError
from .models import MAIN, SUB, TELE, WIDE, Capabilities, Channel, DeviceInfo, Preset, Recording, StreamInfo
from .recordings import parse_file, status_days, to_reolink_time

PTZ_DIRECTIONS = ("Left", "Right", "Up", "Down", "LeftUp", "LeftDown", "RightUp", "RightDown")
PTZ_OPS = PTZ_DIRECTIONS + ("ZoomInc", "ZoomDec", "FocusInc", "FocusDec", "Auto", "Stop")

# Model-name fragments of dual-lens tracking cameras (wide + telephoto streams).
TELEPHOTO_MODELS = ("trackmix", "rlc-81ma", "rlc81ma")

PROTOCOL_RTSP = "rtsp"
PROTOCOL_FLV = "flv"


@dataclass
class ProbeRecord:
    """How a stream URL was chosen (for the diagnostics report)."""

    chosen: str
    confirmed: bool
    tried: list[tuple[str, rtsp.DescribeResult]]


def _ver(abilities: dict, key: str, default: int = 0) -> int:
    value = abilities.get(key)
    if isinstance(value, dict):
        try:
            return int(value.get("ver", default))
        except (TypeError, ValueError):
            return default
    return default


def _stream_info(value: dict) -> StreamInfo:
    width, height = int(value.get("width") or 0), int(value.get("height") or 0)
    if not (width and height) and "*" in str(value.get("size", "")):
        try:
            width, height = (int(x) for x in str(value["size"]).split("*", 1))
        except ValueError:
            pass
    return StreamInfo(width=width, height=height, fps=int(value.get("frameRate") or 0),
                      bitrate=int(value.get("bitRate") or 0), codec=str(value.get("vType") or "").lower())


class Device:
    """A Reolink host with one or more video channels."""

    demo = False

    def __init__(self, host: str, username: str, password: str, port: int | None = None,
                 use_https: bool | None = None, timeout: float = 10.0):
        self.client = Client(host, username, password, port, use_https, timeout)
        self.info = DeviceInfo()
        self.channels: list[Channel] = []
        self.abilities: dict = {}
        self.ports = {"rtsp": 554, "rtmp": 1935, "onvif": 8000, "media": 9000,
                      "rtsp_enabled": True, "rtmp_enabled": True, "onvif_enabled": True}
        self.hdd: list[dict] = []
        self.clock: dt.datetime | None = None
        self.clock_read_at: dt.datetime | None = None
        self.connected = False
        self._stream_urls: dict[tuple[int, int, str, str], str] = {}
        self.probe_log: dict[tuple[int, int, str], ProbeRecord] = {}
        self._codecs: dict[tuple[int, int, str], str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ properties
    @property
    def host(self) -> str:
        return self.client.host

    @property
    def is_nvr(self) -> bool:
        return self.info.is_nvr

    def channel(self, index: int) -> Channel:
        for ch in self.channels:
            if ch.index == index:
                return ch
        raise NotSupported(f"channel {index} does not exist on {self.host}")

    def _chn_abilities(self, index: int) -> dict:
        lst = self.abilities.get("abilityChn") or []
        if 0 <= index < len(lst) and isinstance(lst[index], dict):
            return lst[index]
        return {}

    # ------------------------------------------------------------------ connect
    def connect(self, probe_streams: bool = True) -> None:
        self.client.login()
        user = self.client.username
        host_cmds = [
            {"cmd": "GetDevInfo", "action": 0, "param": {}},
            {"cmd": "GetChannelstatus", "action": 0, "param": {}},
            {"cmd": "GetAbility", "action": 0, "param": {"User": {"userName": user}}},
            {"cmd": "GetNetPort", "action": 0, "param": {}},
            {"cmd": "GetLocalLink", "action": 0, "param": {}},
            {"cmd": "GetP2p", "action": 0, "param": {}},
            {"cmd": "GetHddInfo", "action": 0, "param": {}},
            {"cmd": "GetTime", "action": 0, "param": {}},
        ]
        res = self.client.execute(host_cmds)
        by = {r.get("cmd"): r for r in res}
        dev = by.get("GetDevInfo", {})
        if not ok(dev):
            check(dev, "GetDevInfo")
        self._parse_devinfo(dev["value"].get("DevInfo", {}))
        if ok(by.get("GetAbility", {})):
            self.abilities = by["GetAbility"]["value"].get("Ability", {}) or {}
        if ok(by.get("GetNetPort", {})):
            np = by["GetNetPort"]["value"].get("NetPort", {})
            self.ports.update(rtsp=int(np.get("rtspPort", 554)), rtmp=int(np.get("rtmpPort", 1935)),
                              onvif=int(np.get("onvifPort", 8000)), media=int(np.get("mediaPort", 9000)),
                              rtsp_enabled=np.get("rtspEnable", 1) == 1, rtmp_enabled=np.get("rtmpEnable", 1) == 1,
                              onvif_enabled=np.get("onvifEnable", 1) == 1)
        if ok(by.get("GetLocalLink", {})):
            self.info.mac = str(by["GetLocalLink"]["value"].get("LocalLink", {}).get("mac", ""))
        if ok(by.get("GetP2p", {})):
            self.info.uid = str(by["GetP2p"]["value"].get("P2p", {}).get("uid", ""))
        if ok(by.get("GetHddInfo", {})):
            self.hdd = list(by["GetHddInfo"]["value"].get("HddInfo", []) or [])
        if ok(by.get("GetTime", {})):
            self._parse_time(by["GetTime"]["value"])

        channels: list[Channel] = []
        status = by.get("GetChannelstatus", {})
        if ok(status) and status["value"].get("status"):
            for st in status["value"]["status"]:
                idx = int(st.get("channel", 0))
                name = str(st.get("name") or "")
                if name in ("0", "1"):
                    name = ""
                ch = Channel(index=idx, name=name, model=str(st.get("typeInfo") or ""),
                             online=st.get("online", 1) == 1)
                if not self.is_nvr and idx >= max(1, self.info.channel_count):
                    continue
                channels.append(ch)
        if not channels:
            channels = [Channel(index=i) for i in range(max(1, self.info.channel_count))]
        if not self.is_nvr:
            for ch in channels:
                ch.model = ch.model or self.info.model
                ch.name = ch.name or self.info.name
        self.channels = channels
        self._fetch_channel_data([c for c in channels if c.online])
        self.connected = True
        if probe_streams:
            self.probe_streams()

    def _parse_devinfo(self, d: dict) -> None:
        self.info = DeviceInfo(
            name=str(d.get("name", "")), model=str(d.get("model", "")), hardware=str(d.get("hardVer", "")),
            firmware=str(d.get("firmVer", "")), serial=str(d.get("serial", "")),
            item_number=str(d.get("itemNo", "") or ""),
            device_type=str(d.get("exactType") or d.get("type") or "IPC"),
            channel_count=int(d.get("channelNum") or 1), build_day=str(d.get("buildDay", "")),
        )

    def _parse_time(self, value: dict) -> None:
        t = value.get("Time", {})
        try:
            self.clock = dt.datetime(int(t["year"]), int(t["mon"]), int(t["day"]),
                                     int(t["hour"]), int(t["min"]), int(t["sec"]))
            self.clock_read_at = dt.datetime.now()
        except (KeyError, TypeError, ValueError):
            self.clock = None

    def _fetch_channel_data(self, channels: list[Channel]) -> None:
        cmds: list[dict] = []
        owners: list[tuple[Channel, str]] = []

        def add(ch: Channel, cmd: str, action: int = 0, param: dict | None = None, tag: str = "") -> None:
            cmds.append({"cmd": cmd, "action": action, "param": param if param is not None else {"channel": ch.index}})
            owners.append((ch, tag or cmd))

        known = bool(self.abilities.get("abilityChn"))
        for ch in channels:
            a = self._chn_abilities(ch.index)
            ptz = _ver(a, "ptzType") != 0 or _ver(a, "supportDigitalZoom") > 0
            add(ch, "GetEnc")
            add(ch, "GetRtspUrl")
            add(ch, "GetMdState")
            add(ch, "GetAiState")
            if self.is_nvr or not ch.name:
                add(ch, "GetOsd")
            if not known or ptz:
                add(ch, "GetZoomFocus", 1)
            if not known or _ver(a, "ptzPreset") > 0:
                add(ch, "GetPtzPreset")
                add(ch, "GetPtzPatrol")
            if not known or _ver(a, "GetPtzGuard") > 0:
                add(ch, "GetPtzGuard")
            if not known or _ver(a, "aiTrack") > 0:
                add(ch, "GetAiCfg", 1)
            if not known or _ver(a, "disableAutoFocus") > 0:
                add(ch, "GetAutoFocus")
            if not known or _ver(a, "ledControl") > 0:
                add(ch, "GetIrLights")
            if not known or _ver(a, "floodLight") > 0 or _ver(a, "supportFLswitch") > 0:
                add(ch, "GetWhiteLed")
            if not self.is_nvr and self._has_telephoto(ch, a):
                # A TrackMix on its own serves the telephoto lens as stream channel 1.
                tele = {"channel": ch.index + 1}
                add(ch, "GetEnc", param=tele, tag="tele:GetEnc")
                add(ch, "GetRtspUrl", param=tele, tag="tele:GetRtspUrl")
        results = self.client.execute(cmds) if cmds else []
        answered: dict[int, dict[str, dict]] = {}
        for (ch, tag), item in zip(owners, results):
            answered.setdefault(ch.index, {})[tag] = item
        for ch in channels:
            self._apply_channel(ch, answered.get(ch.index, {}), known)

    def _has_telephoto(self, ch: Channel, abilities: dict) -> bool:
        """Dual-lens tracking cameras (TrackMix family) have a second, telephoto stream.
        Some firmware does not report the ability, so the model name counts too."""
        if _ver(abilities, "supportAutoTrackStream") > 0:
            return True
        model = (ch.model or (self.info.model if not self.is_nvr else "")).lower().replace(" ", "")
        return any(name in model for name in TELEPHOTO_MODELS)

    def _apply_channel(self, ch: Channel, r: dict[str, dict], known: bool) -> None:
        a = self._chn_abilities(ch.index)
        g = self.abilities
        caps = Capabilities()
        if ok(r.get("GetEnc", {})):
            enc = r["GetEnc"]["value"].get("Enc", {})
            ch.main = _stream_info(enc.get("mainStream", {}) or {})
            ch.sub = _stream_info(enc.get("subStream", {}) or {})
        if ok(r.get("GetRtspUrl", {})):
            u = r["GetRtspUrl"]["value"].get("rtspUrl", {})
            ch.rtsp_main, ch.rtsp_sub = str(u.get("mainStream", "")), str(u.get("subStream", ""))
        if ok(r.get("tele:GetEnc", {})):
            enc = r["tele:GetEnc"]["value"].get("Enc", {})
            ch.tele_main = _stream_info(enc.get("mainStream", {}) or {})
            ch.tele_sub = _stream_info(enc.get("subStream", {}) or {})
        if ok(r.get("tele:GetRtspUrl", {})):
            u = r["tele:GetRtspUrl"]["value"].get("rtspUrl", {})
            ch.tele_rtsp_main, ch.tele_rtsp_sub = str(u.get("mainStream", "")), str(u.get("subStream", ""))
        if ok(r.get("GetOsd", {})) and not ch.name:
            ch.name = str(r["GetOsd"]["value"].get("Osd", {}).get("osdChannel", {}).get("name", ""))
        ch.name = ch.name or (f"Channel {ch.index + 1}" if self.is_nvr else self.info.name or self.info.model)

        ptz_ver = _ver(a, "ptzType")
        zf = r.get("GetZoomFocus", {})
        if ok(zf):
            value = zf.get("value", {}).get("ZoomFocus", {})
            rng = zf.get("range", {}).get("ZoomFocus", {})
            zoom_rng = rng.get("zoom", {}).get("pos", {})
            focus_rng = rng.get("focus", {}).get("pos", {})
            ch.zoom.zoom_pos = int(value.get("zoom", {}).get("pos", 0))
            ch.zoom.focus_pos = int(value.get("focus", {}).get("pos", 0))
            if zoom_rng:
                ch.zoom.zoom_min, ch.zoom.zoom_max = int(zoom_rng.get("min", 0)), int(zoom_rng.get("max", 0))
            if focus_rng:
                ch.zoom.focus_min, ch.zoom.focus_max = int(focus_rng.get("min", 0)), int(focus_rng.get("max", 0))
        has_zoom_range = ch.zoom.zoom_max > ch.zoom.zoom_min
        if known:
            caps.optical_zoom = (ptz_ver in (1, 2, 5) or _ver(a, "supportDigitalZoom") > 0) and has_zoom_range
            caps.pan_tilt = ptz_ver in (2, 3, 5, 6, 7)
        else:
            caps.optical_zoom = has_zoom_range
            caps.pan_tilt = ok(r.get("GetPtzPreset", {}))
        caps.focus = caps.optical_zoom and ch.zoom.focus_max > ch.zoom.focus_min
        caps.presets = ok(r.get("GetPtzPreset", {})) and (caps.pan_tilt or caps.optical_zoom
                                                          or _ver(a, "ptzPreset") > 0)
        if ok(r.get("GetPtzPreset", {})):
            ch.presets = [Preset(int(p["id"]), str(p.get("name") or f"Preset {p['id']}"))
                          for p in r["GetPtzPreset"]["value"].get("PtzPreset", []) if int(p.get("enable", 0)) == 1]
        if ok(r.get("GetPtzPatrol", {})):
            ch.patrols = [Preset(int(p["id"]), str(p.get("name") or f"Patrol {int(p['id']) + 1}"))
                          for p in r["GetPtzPatrol"]["value"].get("PtzPatrol", []) if int(p.get("enable", 0)) == 1]
            caps.patrol = caps.pan_tilt and bool(ch.patrols)
        if ok(r.get("GetPtzGuard", {})):
            guard = r["GetPtzGuard"]["value"].get("PtzGuard", {})
            caps.guard = caps.pan_tilt
            ch.guard_enabled = guard.get("benable") == 1 and guard.get("bexistPos") == 1
        caps.calibrate = caps.pan_tilt and (_ver(a, "supportPtzCalibration") > 0 or _ver(a, "supportPtzCheck") > 0)
        caps.ptz_speed = caps.pan_tilt and (not known or _ver(a, "supportPtzSpeed", 1) > 0)
        if ok(r.get("GetAiCfg", {})):
            cfg = r["GetAiCfg"]["value"]
            caps.auto_track = not known or _ver(a, "aiTrack") > 0
            ch.auto_track_key = "bSmartTrack" if "bSmartTrack" in cfg else "aiTrack"
            ch.auto_track_on = int(cfg.get(ch.auto_track_key, 0)) != 0
        if ok(r.get("GetAutoFocus", {})):
            caps.auto_focus = caps.focus
            ch.auto_focus_on = int(r["GetAutoFocus"]["value"].get("AutoFocus", {}).get("disable", 0)) == 0
        caps.telephoto = self._has_telephoto(ch, a)
        if ok(r.get("GetIrLights", {})):
            caps.ir_lights = True
            ch.lights.ir_state = str(r["GetIrLights"]["value"].get("IrLights", {}).get("state", ""))
        wl = r.get("GetWhiteLed", {})
        if ok(wl):
            v = wl["value"].get("WhiteLed", {})
            caps.spotlight = (not known) or _ver(g, "GetWhiteLed", 1) > 0
            ch.lights.spotlight_on = int(v.get("state", 0)) == 1
            ch.lights.spotlight_brightness = int(v.get("bright", 0)) if "bright" in v else None
            ch.lights.spotlight_mode = int(v["mode"]) if "mode" in v else None
        caps.siren = _ver(a, "alarmAudio") > 0 or _ver(a, "supportAudioAlarm") > 0 or (not known)
        caps.replay = _ver(a, "recReplay") > 0 or not known or self.is_nvr
        caps.main_h265 = _ver(a, "mainEncType") > 0 or ch.main.codec == "h265"
        caps.battery = _ver(a, "battery") > 0
        ai = r.get("GetAiState", {})
        if ok(ai):
            caps.ai_types = [k for k, v in ai["value"].items()
                             if k != "channel" and (isinstance(v, int) or (isinstance(v, dict) and v.get("support") == 1))]
            self._apply_ai(ch, ai["value"])
        if ok(r.get("GetMdState", {})):
            ch.detection.motion = int(r["GetMdState"]["value"].get("state", 0)) == 1
        ch.caps = caps

    @staticmethod
    def _apply_ai(ch: Channel, value: dict) -> None:
        for key, v in value.items():
            if key == "channel":
                continue
            if isinstance(v, int):
                ch.detection.ai[key] = v == 1
            elif isinstance(v, dict) and v.get("support") == 1:
                ch.detection.ai[key] = v.get("alarm_state") == 1

    def disconnect(self) -> None:
        self.client.logout()
        self.connected = False

    # ------------------------------------------------------------------ polling
    def poll(self, channels: list[int] | None = None, lights: bool = False) -> None:
        """Refresh motion/AI detection (and optionally light states) for the given channels."""
        cmds, owners = [], []
        for ch in self.channels:
            if not ch.online or (channels is not None and ch.index not in channels):
                continue
            for cmd in ("GetMdState", "GetAiState"):
                cmds.append({"cmd": cmd, "action": 0, "param": {"channel": ch.index}})
                owners.append(ch)
            if lights and ch.caps.spotlight:
                cmds.append({"cmd": "GetWhiteLed", "action": 0, "param": {"channel": ch.index}})
                owners.append(ch)
            if lights and ch.caps.ir_lights:
                cmds.append({"cmd": "GetIrLights", "action": 0, "param": {"channel": ch.index}})
                owners.append(ch)
            if lights and ch.caps.auto_track:
                cmds.append({"cmd": "GetAiCfg", "action": 0, "param": {"channel": ch.index}})
                owners.append(ch)
        if not cmds:
            return
        for ch, item in zip(owners, self.client.execute(cmds)):
            if not ok(item):
                continue
            cmd, value = item.get("cmd"), item.get("value", {})
            if cmd == "GetMdState":
                ch.detection.motion = int(value.get("state", 0)) == 1
            elif cmd == "GetAiState":
                self._apply_ai(ch, value)
            elif cmd == "GetWhiteLed":
                v = value.get("WhiteLed", {})
                ch.lights.spotlight_on = int(v.get("state", 0)) == 1
                if "bright" in v:
                    ch.lights.spotlight_brightness = int(v["bright"])
            elif cmd == "GetIrLights":
                ch.lights.ir_state = str(value.get("IrLights", {}).get("state", ""))
            elif cmd == "GetAiCfg":
                if ch.auto_track_key in value:
                    ch.auto_track_on = int(value.get(ch.auto_track_key, 0)) != 0

    # ------------------------------------------------------------------ streams
    def _rtsp_base(self) -> str:
        user = urllib.parse.quote(self.client.username, safe="")
        pw = urllib.parse.quote(self.client.password, safe="")
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"rtsp://{user}:{pw}@{host}:{self.ports['rtsp']}"

    def _flv_url(self, stream: str) -> str:
        c = self.client
        safe = "@$*~_-+=!?.,:;'()[]"
        user = urllib.parse.quote(c.username, safe=safe)
        pw = urllib.parse.quote(c.password, safe=safe)
        return (f"{c.base_url}/flv?port={self.ports['rtmp']}&app=bcs&stream={stream}"
                f"&user={user}&password={pw}")

    def _with_credentials(self, url: str) -> str:
        if not url.startswith("rtsp://") or "@" in url.split("/", 3)[2]:
            return url
        base = self._rtsp_base()
        rest = url[len("rtsp://"):].split("/", 1)
        path = "/" + rest[1] if len(rest) > 1 else "/"
        return base + path

    def expected_codec(self, index: int, lens: int, quality: str) -> str:
        """The stream's codec: measured by the RTSP probe, else from GetEnc, else a guess."""
        known = self._codecs.get((index, lens, quality))
        if known:
            return known
        ch = self.channel(index)
        if lens == TELE:
            info = ch.tele_main if quality == MAIN else ch.tele_sub
        else:
            info = ch.main if quality == MAIN else ch.sub
        if info.codec in ("h264", "h265"):
            return info.codec
        return "h265" if quality == MAIN and ch.caps.main_h265 else "h264"

    def stream_candidates(self, index: int, lens: int = WIDE, quality: str = MAIN,
                          protocol: str = PROTOCOL_RTSP) -> list[str]:
        """Stream URLs to try, best guess first.

        FLV (HTTP) is only offered for H.264 streams: Reolink cannot send H.265 over FLV,
        so the Clear stream of 4K cameras (Duo 2, TrackMix, RLC-8xx…) is RTSP only.
        """
        ch = self.channel(index)
        if lens == TELE:
            urls = self._tele_candidates(ch, quality, protocol)
        else:
            cc = f"{index + 1:02d}"
            base = self._rtsp_base()
            enc = self.expected_codec(index, lens, quality)
            other = "h264" if enc == "h265" else "h265"
            reported = ch.rtsp_main if quality == MAIN else ch.rtsp_sub
            rtsp_urls = ([self._with_credentials(reported)] if reported else []) + [
                f"{base}/{enc}Preview_{cc}_{quality}", f"{base}/Preview_{cc}_{quality}",
                f"{base}/{other}Preview_{cc}_{quality}"]
            flv = [self._flv_url(f"channel{index}_{quality}.bcs")]
            urls = flv + rtsp_urls if protocol == PROTOCOL_FLV else rtsp_urls + flv
        if self.expected_codec(index, lens, quality) == "h265":
            urls = [u for u in urls if not u.startswith("http")]
        return list(dict.fromkeys(urls))

    def _tele_candidates(self, ch: Channel, quality: str, protocol: str) -> list[str]:
        """Telephoto-lens streams. A TrackMix on its own serves them as the next channel
        (``Preview_02_main``); behind an NVR or Home Hub they are the camera channel's
        "autotrack" streams (the next NVR channel is another camera)."""
        base = self._rtsp_base()
        autotrack = f"{base}/Preview_{ch.index + 1:02d}_autotrack" + ("" if quality == MAIN else "_sub")
        flv_auto = self._flv_url(f"channel{ch.index}_autotrack_{quality}.bcs")
        if self.is_nvr:
            # The FLV form is the reliable one for the telephoto sub stream.
            return [flv_auto, autotrack] if quality == SUB or protocol == PROTOCOL_FLV else [autotrack, flv_auto]
        nc = f"{ch.index + 2:02d}"
        info = ch.tele_main if quality == MAIN else ch.tele_sub
        default = "h265" if quality == MAIN and ch.caps.main_h265 else "h264"
        enc = info.codec if info.codec in ("h264", "h265") else default
        other = "h264" if enc == "h265" else "h265"
        reported = ch.tele_rtsp_main if quality == MAIN else ch.tele_rtsp_sub
        rtsp_urls = ([self._with_credentials(reported)] if reported else []) + [
            f"{base}/{enc}Preview_{nc}_{quality}", f"{base}/Preview_{nc}_{quality}",
            f"{base}/{other}Preview_{nc}_{quality}", autotrack]
        flv = [self._flv_url(f"channel{ch.index + 1}_{quality}.bcs"), flv_auto]
        ordered = flv + rtsp_urls if protocol == PROTOCOL_FLV else rtsp_urls + flv
        return list(dict.fromkeys(ordered))

    def _tele_forms(self, index: int) -> list[tuple[int, dict]]:
        """(channel, extra parameters) addressing the telephoto lens in Snap/Search, likely first."""
        direct, logical = (index + 1, {}), (index, {"iLogicChannel": 1})
        return [logical] if self.is_nvr else [direct, logical]

    def stream_url(self, index: int, lens: int = WIDE, quality: str = MAIN,
                   protocol: str = PROTOCOL_RTSP) -> str:
        key = (index, lens, quality, protocol)
        with self._lock:
            cached = self._stream_urls.get(key)
        if cached:
            return cached
        return self.stream_candidates(index, lens, quality, protocol)[0]

    def stream_codec(self, index: int, lens: int, quality: str) -> str:
        return self._codecs.get((index, lens, quality), "")

    def probe_stream(self, index: int, lens: int, quality: str, protocol: str = PROTOCOL_RTSP) -> str:
        """Pick the URL to play: the first RTSP URL the camera confirms (DESCRIBE); cache it.

        When no URL can be confirmed (the check itself can fail on some firmware) an RTSP
        URL is still preferred; FLV is used only for H.264 streams, when RTSP is switched
        off on the camera or every RTSP path was answered "not found".
        """
        key = (index, lens, quality)
        candidates = self.stream_candidates(index, lens, quality, protocol)
        rtsp_urls = [u for u in candidates if u.startswith("rtsp://")]
        http_urls = [u for u in candidates if u.startswith("http")]
        results: list[tuple[str, rtsp.DescribeResult]] = []
        chosen, confirmed = "", False
        if candidates and candidates[0].startswith("http"):
            # FLV first by design: the FLV setting, or an NVR's telephoto sub stream.
            chosen = candidates[0]
        else:
            rtsp_off = not self.ports.get("rtsp_enabled", True)
            if not rtsp_off:
                for url in rtsp_urls:
                    result = rtsp.describe(url, self.client.username, self.client.password, timeout=5)
                    results.append((url, result))
                    if result.ok:
                        chosen, confirmed = url, True
                        if result.codec:
                            self._codecs[key] = result.codec
                        break
                    if result.refused:
                        rtsp_off = True
                        break
            if not chosen:
                all_missing = bool(results) and all(r.status in (404, 454) for _u, r in results)
                if http_urls and (rtsp_off or all_missing):
                    chosen = http_urls[0]
                else:
                    unanswered = [u for u, r in results if r.status not in (404, 454)]
                    chosen = (unanswered or rtsp_urls or candidates)[0]
        with self._lock:
            self._stream_urls[(index, lens, quality, protocol)] = chosen
            self.probe_log[key] = ProbeRecord(chosen, confirmed, [(u, r) for u, r in results])
        return chosen

    def probe_streams(self) -> None:
        jobs = [(ch.index, lens, quality) for ch in self.channels if ch.online
                for lens in ch.lenses for quality in (MAIN, SUB)]

        def probe(job):
            try:
                self.probe_stream(*job)
            except ReolinkError:
                pass

        # One RTSP connection at a time per camera (Reolink's RTSP server is easily
        # overwhelmed); an NVR's channels are checked a few at a time.
        workers = 3 if self.is_nvr else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(probe, jobs))

    def forget_stream(self, index: int, lens: int, quality: str, protocol: str = PROTOCOL_RTSP) -> None:
        with self._lock:
            self._stream_urls.pop((index, lens, quality, protocol), None)

    # ------------------------------------------------------------------ PTZ
    def ptz(self, index: int, op: str, speed: int | None = None, preset_id: int | None = None) -> None:
        if op not in PTZ_OPS and op not in ("ToPos", "StartPatrol", "StopPatrol"):
            raise ValueError(f"unknown PTZ operation {op}")
        ch = self.channel(index)
        param: dict = {"channel": index, "op": op}
        if speed is not None and op in PTZ_DIRECTIONS + ("Auto",) and ch.caps.ptz_speed:
            param["speed"] = max(1, min(64, int(speed)))
        if preset_id is not None:
            param["id"] = int(preset_id)
        self.client.command("PtzCtrl", param)

    def ptz_stop(self, index: int) -> None:
        self.client.command("PtzCtrl", {"channel": index, "op": "Stop"})

    def goto_preset(self, index: int, preset_id: int, speed: int | None = None) -> None:
        self.ptz(index, "ToPos", speed=speed, preset_id=preset_id)

    def save_preset(self, index: int, preset_id: int, name: str) -> None:
        self.client.command("SetPtzPreset", {"PtzPreset": {"channel": index, "enable": 1, "id": int(preset_id),
                                                           "name": name[:31]}})
        self.refresh_presets(index)

    def delete_preset(self, index: int, preset_id: int) -> None:
        self.client.command("SetPtzPreset", {"PtzPreset": {"channel": index, "enable": 0, "id": int(preset_id)}})
        self.refresh_presets(index)

    def refresh_presets(self, index: int) -> list[Preset]:
        ch = self.channel(index)
        value = self.client.command("GetPtzPreset", {"channel": index})
        ch.presets = [Preset(int(p["id"]), str(p.get("name") or f"Preset {p['id']}"))
                      for p in value.get("PtzPreset", []) if int(p.get("enable", 0)) == 1]
        return ch.presets

    def free_preset_id(self, index: int) -> int:
        used = {p.id for p in self.channel(index).presets}
        for i in range(1, 65):
            if i not in used:
                return i
        raise ReolinkError("all 64 preset slots are in use")

    def start_patrol(self, index: int, patrol_id: int) -> None:
        self.ptz(index, "StartPatrol", preset_id=patrol_id)

    def stop_patrol(self, index: int, patrol_id: int) -> None:
        self.ptz(index, "StopPatrol", preset_id=patrol_id)

    def set_zoom(self, index: int, position: int) -> None:
        ch = self.channel(index)
        pos = max(ch.zoom.zoom_min, min(ch.zoom.zoom_max, int(position)))
        self.client.command("StartZoomFocus", {"ZoomFocus": {"channel": index, "op": "ZoomPos", "pos": pos}})
        ch.zoom.zoom_pos = pos

    def set_focus(self, index: int, position: int) -> None:
        ch = self.channel(index)
        pos = max(ch.zoom.focus_min, min(ch.zoom.focus_max, int(position)))
        self.client.command("StartZoomFocus", {"ZoomFocus": {"channel": index, "op": "FocusPos", "pos": pos}})
        ch.zoom.focus_pos = pos

    def refresh_zoom(self, index: int) -> None:
        ch = self.channel(index)
        value = self.client.command("GetZoomFocus", {"channel": index}).get("ZoomFocus", {})
        ch.zoom.zoom_pos = int(value.get("zoom", {}).get("pos", ch.zoom.zoom_pos))
        ch.zoom.focus_pos = int(value.get("focus", {}).get("pos", ch.zoom.focus_pos))

    def set_auto_focus(self, index: int, enable: bool) -> None:
        self.client.command("SetAutoFocus", {"AutoFocus": {"channel": index, "disable": 0 if enable else 1}})
        self.channel(index).auto_focus_on = enable

    def set_auto_track(self, index: int, enable: bool) -> None:
        ch = self.channel(index)
        self.client.command("SetAiCfg", {"channel": index, ch.auto_track_key: 1 if enable else 0})
        ch.auto_track_on = enable

    def calibrate(self, index: int) -> None:
        self.client.command("PtzCheck", {"channel": index})

    def set_guard_here(self, index: int, enable: bool = True, timeout: int = 60) -> None:
        self.client.command("SetPtzGuard", {"PtzGuard": {"channel": index, "cmdStr": "setPos", "bSaveCurrentPos": 1,
                                                         "benable": 1 if enable else 0, "timeout": int(timeout)}})
        self.channel(index).guard_enabled = enable

    def goto_guard(self, index: int) -> None:
        self.client.command("SetPtzGuard", {"PtzGuard": {"channel": index, "cmdStr": "toPos"}})

    # ------------------------------------------------------------------ lights / siren
    def set_ir(self, index: int, state: str) -> None:
        if state not in ("Auto", "Off", "On"):
            raise ValueError(state)
        self.client.command("SetIrLights", {"IrLights": {"channel": index, "state": state}})
        self.channel(index).lights.ir_state = state

    def set_spotlight(self, index: int, on: bool | None = None, brightness: int | None = None) -> None:
        ch = self.channel(index)
        settings: dict = {"channel": index}
        if on is not None:
            settings["state"] = 1 if on else 0
        if brightness is not None:
            settings["bright"] = max(0, min(100, int(brightness)))
        self.client.command("SetWhiteLed", {"WhiteLed": settings})
        if on is not None:
            ch.lights.spotlight_on = on
        if brightness is not None:
            ch.lights.spotlight_brightness = settings["bright"]

    def siren(self, index: int, on: bool = True, times: int = 1) -> None:
        if on:
            param = {"alarm_mode": "times", "manual_switch": 0, "times": max(1, int(times)), "channel": index}
        else:
            param = {"alarm_mode": "manul", "manual_switch": 0, "channel": index}
        self.client.command("AudioAlarmPlay", param)

    # ------------------------------------------------------------------ snapshots / time
    def snapshot(self, index: int, lens: int = WIDE) -> bytes:
        forms = self._tele_forms(index) if lens == TELE else [(index, {})]
        last: Exception | None = None
        for channel, extra in forms:
            rs = "".join(random.choices(string.ascii_letters + string.digits, k=12))
            params = {"cmd": "Snap", "channel": channel, "rs": rs, "snapType": "main", **extra}
            try:
                data = self.client.get_bytes(params, timeout=20)
            except ApiError as exc:
                last = exc
                continue
            if data.startswith(b"\xff\xd8"):
                return data
            last = ApiError("Snap", None, "the camera did not return a JPEG image")
        raise last or ApiError("Snap", None, "no snapshot")

    def read_clock(self) -> dt.datetime | None:
        self._parse_time(self.client.command("GetTime"))
        return self.clock

    def sync_clock(self, moment: dt.datetime | None = None) -> None:
        moment = moment or dt.datetime.now()
        value = self.client.command("GetTime")
        t = dict(value.get("Time", {}))
        t.update(year=moment.year, mon=moment.month, day=moment.day, hour=moment.hour,
                 min=moment.minute, sec=moment.second)
        self.client.command("SetTime", {"Time": t})
        self._parse_time({"Time": t})

    def clock_offset(self) -> float | None:
        """Seconds the camera clock is ahead of this PC (None if unknown)."""
        if self.clock is None or self.clock_read_at is None:
            return None
        return (self.clock - self.clock_read_at).total_seconds()

    # ------------------------------------------------------------------ recordings
    def _search(self, index: int, start: dt.datetime, end: dt.datetime, only_status: bool,
                stream: str, lens: int) -> dict:
        forms = self._tele_forms(index) if lens == TELE else [(index, {})]
        last: Exception | None = None
        for channel, extra in forms:
            search = {"channel": channel, "onlyStatus": 1 if only_status else 0, "streamType": stream,
                      "StartTime": to_reolink_time(start), "EndTime": to_reolink_time(end), **extra}
            try:
                value = self.client.command("Search", {"Search": search})
            except ApiError as exc:
                last = exc
                continue
            return value.get("SearchResult", {}) or {}
        raise last or ApiError("Search", None, "search failed")

    def recording_days(self, index: int, year: int, month: int, stream: str = MAIN, lens: int = WIDE) -> set[int]:
        start = dt.datetime(year, month, 1)
        result = self._search(index, start, start + dt.timedelta(minutes=5), True, stream, lens)
        return status_days(result, year, month)

    def recordings(self, index: int, day: dt.date, stream: str = MAIN, lens: int = WIDE) -> list[Recording]:
        start = dt.datetime.combine(day, dt.time(0, 0, 0))
        end = dt.datetime.combine(day, dt.time(23, 59, 59))
        found: dict[str, Recording] = {}
        cursor = start
        for _ in range(40):          # the camera may truncate long lists; continue after the last file
            result = self._search(index, cursor, end, False, stream, lens)
            new = 0
            last_end = cursor
            for item in result.get("File", []) or []:
                rec = parse_file(item, index, lens)
                if rec is None:
                    continue
                if rec.name not in found:
                    found[rec.name] = rec
                    new += 1
                last_end = max(last_end, rec.end)
            if new == 0 or last_end <= cursor or last_end >= end:
                break
            cursor = last_end + dt.timedelta(seconds=1)
        return sorted(found.values(), key=lambda r: r.start)

    def _nvr_prepare(self, rec: Recording) -> str:
        value = self.client.command("NvrDownload", {"NvrDownload": {
            "channel": rec.channel, "iLogicChannel": 1 if rec.lens == TELE else 0, "streamType": rec.stream,
            "StartTime": to_reolink_time(rec.start), "EndTime": to_reolink_time(rec.end)}}, action=1)
        files = value.get("fileList", []) or []
        if not files:
            raise ApiError("NvrDownload", None, "the NVR did not prepare a file")
        best = max(files, key=lambda f: int(f.get("fileSize", 0) or 0))
        return str(best["fileName"])

    def playback_urls(self, rec: Recording) -> list[str]:
        """URLs mpv can play for a recording, most reliable first."""
        name = rec.name
        start = rec.start.strftime("%Y%m%d%H%M%S")
        output = os.path.basename(name) or f"{start}.mp4"
        urls = [self.client.url_with_token({"cmd": "Playback", "source": name, "output": output, "start": start}),
                self.client.url_with_token({"cmd": "Download", "source": name, "output": output})]
        stream = rec.stream if rec.stream in (MAIN, SUB) else MAIN
        channel = rec.channel
        if rec.lens == TELE and not self.is_nvr:
            channel, stype = rec.channel + 1, 0 if stream == MAIN else 1
        else:
            stype = {(WIDE, MAIN): 0, (WIDE, SUB): 1, (TELE, MAIN): 2, (TELE, SUB): 3}[(rec.lens, stream)]
        urls.append(self._flv_url("playback.bcs").replace(
            "stream=playback.bcs", f"stream=playback.bcs&channel={channel}&type={stype}&start={start}&seek=0"))
        return urls

    def download(self, rec: Recording, path: str, progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> int:
        """Download a recording to ``path`` (written to ``path + '.part'`` first)."""
        name = self._nvr_prepare(rec) if self.is_nvr else rec.name
        output = os.path.basename(name) or os.path.basename(path)
        tmp = path + ".part"
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        def report(done: int, total: int) -> None:
            if progress:
                progress(done, total or rec.size)

        try:
            with open(tmp, "wb") as fh:
                size = self.client.download({"cmd": "Download", "source": name, "output": output}, fh,
                                            report, cancelled)
            if size == 0:
                raise ApiError("Download", None, "the camera sent an empty file")
            os.replace(tmp, path)
            return size
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------ misc
    def storage_summary(self) -> str:
        parts = []
        for disk in self.hdd:
            cap = int(disk.get("capacity", 0) or 0)
            free = int(disk.get("size", 0) or 0)
            kind = "SD card" if int(disk.get("storageType", 2)) == 2 else "HDD"
            if not disk.get("mount") and not cap:
                parts.append(f"{kind}: not present")
                continue
            if cap:
                used = max(0, cap - free)
                parts.append(f"{kind}: {used / 1024:.1f} of {cap / 1024:.1f} GB used")
            else:
                parts.append(f"{kind}: {'formatted' if disk.get('format') else 'not formatted'}")
        return " · ".join(parts) if parts else "No SD card or disk"

    def reboot(self) -> None:
        self.client.command("Reboot")
