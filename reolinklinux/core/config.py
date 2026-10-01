"""Saved cameras and preferences (``~/.config/reolinklinux/config.json``).

Passwords go to the desktop keyring (Secret Service: GNOME Keyring, KWallet, …)
through the optional ``keyring`` module when it is installed and working; otherwise
they are stored in the config file, which is created readable by your user only.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

KEYRING_SERVICE = "ReolinkLinux"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "reolinklinux"


def _xdg_user_dir(key: str, fallback: str) -> Path:
    """Resolve an XDG user directory (``VIDEOS``, ``PICTURES``) from user-dirs.dirs."""
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "user-dirs.dirs"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"XDG_{key}_DIR="):
                value = line.split("=", 1)[1].strip().strip('"')
                value = value.replace("$HOME", str(Path.home()))
                if value and value != str(Path.home()):
                    return Path(value)
    except OSError:
        pass
    return Path.home() / fallback


def default_video_dir() -> Path:
    return _xdg_user_dir("VIDEOS", "Videos") / "ReolinkLinux"


def default_picture_dir() -> Path:
    return _xdg_user_dir("PICTURES", "Pictures") / "ReolinkLinux"


@dataclass
class CameraConfig:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = ""
    host: str = ""
    port: int | None = None
    https: bool | None = None
    username: str = "admin"
    password: str = ""                 # empty when kept in the keyring
    demo: str = ""                     # demo kind ("duo2", "trackmix", …) for simulated cameras
    hidden_channels: list[int] = field(default_factory=list)
    continuous_record: bool = False    # record the stream to disk whenever the app runs
    software_decode: bool = False      # never use the GPU video decoder for this camera

    @property
    def label(self) -> str:
        return self.name or self.host


@dataclass
class Settings:
    theme: str = "system"
    grid_quality: str = "auto"         # grid stream: auto (Clear for up to 4 videos) | main | sub
    focus_quality: str = "main"        # stream shown when one camera is enlarged
    protocol: str = "rtsp"             # rtsp | flv
    hwdec: str = "no"                  # mpv --hwdec; GPU decoders garble Reolink H.265 (see CLAUDE.md)
    low_latency: bool = True
    grid_audio: bool = False           # play audio in the grid (otherwise only when enlarged)
    fill_tiles: bool = False           # crop video to fill tiles instead of letterboxing
    both_lenses: bool = True           # show both lenses of TrackMix-style cameras in the grid
    video_dir: str = ""
    picture_dir: str = ""
    record_format: str = "mp4"         # mp4 | mkv
    segment_minutes: int = 15          # continuous recording file length
    retention_days: int = 7            # continuous recording: delete older files (0 = keep)
    ptz_speed: int = 32
    show_detection: bool = True
    remember_layout: bool = True
    layout: str = "auto"
    use_keyring: bool = True

    def videos(self) -> Path:
        return Path(self.video_dir) if self.video_dir else default_video_dir()

    def pictures(self) -> Path:
        return Path(self.picture_dir) if self.picture_dir else default_picture_dir()


class _Keyring:
    """Thin wrapper around the optional ``keyring`` module."""

    def __init__(self) -> None:
        self._mod = None
        self._checked = False

    def module(self):
        if self._checked:
            return self._mod
        self._checked = True
        if os.environ.get("REOLINKLINUX_NO_KEYRING"):
            return None
        try:
            import keyring  # type: ignore

            backend = keyring.get_keyring()
            name = f"{type(backend).__module__}.{type(backend).__name__}".lower()
            # No usable backend, or a plaintext one (the 0600 config file is no worse).
            if "fail" in name or "null" in name or name.startswith("keyrings.alt"):
                return None
            if getattr(backend, "priority", 1) <= 0:
                return None
            self._mod = keyring
        except Exception:  # noqa: BLE001 - keyring is optional and can fail in many ways
            self._mod = None
        return self._mod

    @property
    def available(self) -> bool:
        return self.module() is not None

    def get(self, key: str) -> str | None:
        mod = self.module()
        if not mod:
            return None
        try:
            return mod.get_password(KEYRING_SERVICE, key)
        except Exception:  # noqa: BLE001
            return None

    def set(self, key: str, value: str) -> bool:
        mod = self.module()
        if not mod:
            return False
        try:
            mod.set_password(KEYRING_SERVICE, key, value)
            return True
        except Exception:  # noqa: BLE001
            return False

    def delete(self, key: str) -> None:
        mod = self.module()
        if not mod:
            return
        try:
            mod.delete_password(KEYRING_SERVICE, key)
        except Exception:  # noqa: BLE001
            pass


keyring = _Keyring()


CONFIG_VERSION = 3


def _migrate(settings: dict, version: int) -> None:
    """Move settings that were saved with an old default to the new default."""
    if version < 2 and settings.get("grid_quality") == "sub":
        settings["grid_quality"] = "auto"     # 1.0 always showed the grid in Fluent quality
    # 1.0 and 1.0.1-1.0.2 decoded on the GPU by default, which drew coloured dots and lines over
    # the H.265 Clear streams of a Duo 2 and a TrackMix (RTX 4090; the CPU decodes them cleanly).
    if version < 3 and settings.get("hwdec") in ("auto-safe", "auto-copy-safe"):
        settings["hwdec"] = "no"


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "config.json"
        self.cameras: list[CameraConfig] = []
        self.settings = Settings()
        self.window: dict = {}
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        known = {f for f in Settings.__dataclass_fields__}
        settings = {k: v for k, v in (data.get("settings") or {}).items() if k in known}
        version = int(data.get("version", 1) or 1)
        if version < CONFIG_VERSION:
            _migrate(settings, version)
        self.settings = Settings(**settings)
        cam_fields = {f for f in CameraConfig.__dataclass_fields__}
        self.cameras = [CameraConfig(**{k: v for k, v in c.items() if k in cam_fields})
                        for c in data.get("cameras", []) if isinstance(c, dict)]
        self.window = data.get("window") or {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": CONFIG_VERSION,
            "settings": asdict(self.settings),
            "cameras": [asdict(c) for c in self.cameras],
            "window": self.window,
        }
        fd, tmp = tempfile.mkstemp(prefix=".config-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- cameras
    def camera(self, cam_id: str) -> CameraConfig | None:
        return next((c for c in self.cameras if c.id == cam_id), None)

    def password(self, cam: CameraConfig) -> str:
        if cam.password:
            return cam.password
        return keyring.get(cam.id) or ""

    def set_password(self, cam: CameraConfig, password: str) -> None:
        if self.settings.use_keyring and keyring.set(cam.id, password):
            cam.password = ""
        else:
            cam.password = password

    def add(self, cam: CameraConfig, password: str) -> None:
        self.set_password(cam, password)
        self.cameras.append(cam)
        self.save()

    def remove(self, cam_id: str) -> None:
        self.cameras = [c for c in self.cameras if c.id != cam_id]
        keyring.delete(cam_id)
        self.save()

    def move(self, cam_id: str, offset: int) -> None:
        idx = next((i for i, c in enumerate(self.cameras) if c.id == cam_id), None)
        if idx is None:
            return
        new = max(0, min(len(self.cameras) - 1, idx + offset))
        cam = self.cameras.pop(idx)
        self.cameras.insert(new, cam)
        self.save()
