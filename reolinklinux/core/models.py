"""Plain data classes describing devices, channels and recordings."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import IntFlag, auto

# Stream qualities as Reolink names them.
MAIN = "main"   # "Clear": full resolution
SUB = "sub"     # "Fluent": low resolution, low bandwidth

WIDE = 0        # lens index: the wide (or only) lens
TELE = 1        # lens index: the telephoto lens of TrackMix-style cameras


@dataclass
class DeviceInfo:
    name: str = ""
    model: str = ""
    hardware: str = ""
    firmware: str = ""
    serial: str = ""
    uid: str = ""
    item_number: str = ""
    device_type: str = "IPC"
    channel_count: int = 1
    mac: str = ""
    build_day: str = ""

    @property
    def is_nvr(self) -> bool:
        return self.device_type.upper() in ("NVR", "WIFI_NVR", "HOMEHUB")

    @property
    def is_hub(self) -> bool:
        return self.device_type.upper() == "HOMEHUB"


@dataclass
class Capabilities:
    """What a channel can do, from ``GetAbility`` confirmed by the ``Get*`` answers."""

    pan_tilt: bool = False
    optical_zoom: bool = False
    focus: bool = False
    auto_focus: bool = False
    presets: bool = False
    patrol: bool = False
    guard: bool = False
    calibrate: bool = False
    ptz_speed: bool = False
    auto_track: bool = False
    telephoto: bool = False       # second (telephoto) lens stream, e.g. TrackMix
    ir_lights: bool = False
    spotlight: bool = False       # "WhiteLed": floodlight / spotlight
    siren: bool = False
    replay: bool = True           # SD card / HDD recordings
    motion: bool = True
    ai_types: list[str] = field(default_factory=list)   # people, vehicle, dog_cat, face, package
    main_h265: bool = False
    battery: bool = False

    @property
    def any_ptz(self) -> bool:
        return self.pan_tilt or self.optical_zoom or self.presets


@dataclass
class StreamInfo:
    width: int = 0
    height: int = 0
    fps: int = 0
    bitrate: int = 0           # kbps
    codec: str = ""            # h264 / h265

    @property
    def resolution(self) -> str:
        return f"{self.width}×{self.height}" if self.width and self.height else ""


@dataclass
class Preset:
    id: int
    name: str


@dataclass
class ZoomRange:
    zoom_min: int = 0
    zoom_max: int = 0
    zoom_pos: int = 0
    focus_min: int = 0
    focus_max: int = 0
    focus_pos: int = 0


@dataclass
class LightState:
    ir_state: str | None = None          # "Auto" / "Off" / "On"
    spotlight_on: bool | None = None
    spotlight_brightness: int | None = None
    spotlight_mode: int | None = None


@dataclass
class DetectionState:
    motion: bool = False
    ai: dict[str, bool] = field(default_factory=dict)   # people/vehicle/dog_cat/face/package

    @property
    def active(self) -> list[str]:
        out = [k for k, v in self.ai.items() if v]
        if self.motion and not out:
            out.append("motion")
        return out


@dataclass
class Channel:
    """One video channel: a camera, or a camera attached to an NVR / Home Hub."""

    index: int
    name: str = ""
    model: str = ""
    online: bool = True
    caps: Capabilities = field(default_factory=Capabilities)
    main: StreamInfo = field(default_factory=StreamInfo)
    sub: StreamInfo = field(default_factory=StreamInfo)
    rtsp_main: str = ""               # from GetRtspUrl, if the firmware reports it
    rtsp_sub: str = ""
    presets: list[Preset] = field(default_factory=list)
    patrols: list[Preset] = field(default_factory=list)
    zoom: ZoomRange = field(default_factory=ZoomRange)
    lights: LightState = field(default_factory=LightState)
    detection: DetectionState = field(default_factory=DetectionState)
    auto_track_on: bool | None = None
    auto_track_key: str = "bSmartTrack"
    auto_focus_on: bool | None = None
    guard_enabled: bool | None = None

    @property
    def lenses(self) -> list[int]:
        return [WIDE, TELE] if self.caps.telephoto else [WIDE]


class Trigger(IntFlag):
    NONE = 0
    TIMER = auto()
    MOTION = auto()
    PERSON = auto()
    VEHICLE = auto()
    ANIMAL = auto()
    FACE = auto()
    DOORBELL = auto()
    PACKAGE = auto()

    def labels(self) -> list[str]:
        names = {Trigger.PERSON: "Person", Trigger.VEHICLE: "Vehicle", Trigger.ANIMAL: "Animal",
                 Trigger.FACE: "Face", Trigger.PACKAGE: "Package", Trigger.DOORBELL: "Doorbell",
                 Trigger.MOTION: "Motion", Trigger.TIMER: "Scheduled"}
        return [label for flag, label in names.items() if self & flag]


@dataclass
class Recording:
    """A file on the camera's SD card (or the NVR's disk)."""

    channel: int
    name: str                   # the camera's file name, used for Playback/Download
    start: dt.datetime
    end: dt.datetime
    size: int = 0
    stream: str = MAIN
    lens: int = WIDE
    width: int = 0
    height: int = 0
    triggers: Trigger = Trigger.NONE

    @property
    def duration(self) -> dt.timedelta:
        return max(self.end - self.start, dt.timedelta(0))

    def contains(self, moment: dt.datetime) -> bool:
        return self.start <= moment < self.end

    def local_filename(self, camera_name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in camera_name).strip() or "camera"
        lens = "_tele" if self.lens == TELE else ""
        return f"{safe}_{self.start:%Y-%m-%d_%H-%M-%S}{lens}_{self.stream}.mp4"
