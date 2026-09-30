"""Stroke icons drawn for this app (24×24, rendered in the theme's colours).

Same design language as the EZP2019Linux / FirmwareLab suite: 24×24 line icons
with round caps and joins, tinted at render time to the active palette.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

_PATHS: dict[str, str] = {
    # shared with the suite
    "open": '<path d="M3 7.5A1.5 1.5 0 0 1 4.5 6H9l2 2h8.5A1.5 1.5 0 0 1 21 9.5v9A1.5 1.5 0 0 1 19.5 20h-15A1.5 1.5 0 0 1 3 18.5z"/><path d="M3 11h18"/>',
    "save": '<path d="M5 4h11l4 4v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"/><path d="M8 4v4.5h7V4"/><rect x="7" y="13" width="10" height="7" rx="1"/>',
    "search": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.8-4.8"/>',
    "database": '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5v13c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8v-13"/><path d="M4.5 12c0 1.5 3.4 2.8 7.5 2.8s7.5-1.3 7.5-2.8"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5"/><path d="M12 7.6v.1"/>',
    "cancel": '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
    "chip": '<rect x="6" y="6" width="12" height="12" rx="1.5"/><rect x="9.5" y="9.5" width="5" height="5" rx=".6"/><path d="M9.5 2.5V6M14.5 2.5V6M9.5 18v3.5M14.5 18v3.5M2.5 9.5H6M2.5 14.5H6M18 9.5h3.5M18 14.5h3.5"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M2.5 12h2M19.5 12h2M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    "moon": '<path d="M20 14.6A8.2 8.2 0 0 1 9.4 4a8.2 8.2 0 1 0 10.6 10.6z"/>',
    "log": '<path d="m5 16.5 5-4.5-5-4.5"/><path d="M12.5 18H19"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "trash": '<path d="M4.5 7h15"/><path d="M10 11v5.5M14 11v5.5"/><path d="m6.5 7 .9 11.6a1.6 1.6 0 0 0 1.6 1.4h6a1.6 1.6 0 0 0 1.6-1.4L17.5 7"/><path d="M9.5 7V4.5h5V7"/>',
    "edit": '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17z"/><path d="m14.5 7.5 2 2"/>',
    "copy": '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="1.8"/><path d="M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19.5 7"/>',
    "warning": '<path d="M10.3 4.4 2.9 17.5A2 2 0 0 0 4.6 20.5h14.8a2 2 0 0 0 1.7-3L13.7 4.4a2 2 0 0 0-3.4 0z"/><path d="M12 9.5v4.5"/><path d="M12 17.2v.1"/>',
    "error": '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5.5"/><path d="M12 16.3v.1"/>',
    "refresh": '<path d="M20 11.5A8 8 0 0 0 5.6 7.2L4 9"/><path d="M4 4.5V9h4.5"/><path d="M4 12.5a8 8 0 0 0 14.4 4.3L20 15"/><path d="M20 19.5V15h-4.5"/>',
    "import": '<path d="M12 4v10"/><path d="m8 10 4 4 4-4"/><path d="M5 16v2.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V16"/>',
    "export": '<path d="M12 14V4"/><path d="m8 8 4-4 4 4"/><path d="M5 16v2.5A1.5 1.5 0 0 0 6.5 20h11a1.5 1.5 0 0 0 1.5-1.5V16"/>',
    "binary": '<path d="M6 3.5h8.5L19 8v11a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 19V5a1.5 1.5 0 0 1 1-1.4"/><path d="M14 3.5V8h5"/><rect x="8" y="11.5" width="3" height="5" rx="1.2"/><path d="M14.5 11.5v5"/>',
    "chevron_down": '<path d="m6.5 9.5 5.5 5.5 5.5-5.5"/>',
    "chevron_up": '<path d="m6.5 14.5 5.5-5.5 5.5 5.5"/>',
    "shield": '<path d="M12 3.2 19 6v5.6c0 4.3-2.9 7.6-7 9.2-4.1-1.6-7-4.9-7-9.2V6z"/><path d="M12 8.5v4"/><path d="M12 15.6v.1"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "close": '<path d="M6 6l12 12M18 6 6 18"/>',
    "goto": '<path d="M5 4v6.5A3.5 3.5 0 0 0 8.5 14H19"/><path d="m15 10 4 4-4 4"/>',
    "auto": '<path d="M13.5 2.5 5 13.5h6.5l-1 8 8.5-11h-6.5z"/>',
    "chevron_left": '<path d="m14.5 6.5-5.5 5.5 5.5 5.5"/>',
    "chevron_right": '<path d="m9.5 6.5 5.5 5.5-5.5 5.5"/>',
    # camera client
    "camera": '<path d="M3.5 8.5A1.5 1.5 0 0 1 5 7h2.3l1.5-2h6.4l1.5 2H19a1.5 1.5 0 0 1 1.5 1.5v9A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5z"/><circle cx="12" cy="13" r="3.6"/>',
    "cctv": '<path d="M3.5 7.2 15.6 4l1.7 6.4-12 3.2z"/><path d="m17.3 10.4 3.2-.9-1.2-4.4-3.2.9"/><path d="M8.5 12.3 9.6 16.5"/><path d="M3 20.5v-5.3M3 17.8h6.6v-1.3"/>',
    "grid": '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
    "grid1": '<rect x="4" y="4" width="16" height="16" rx="2"/>',
    "grid9": '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9.3 4v16M14.7 4v16M4 9.3h16M4 14.7h16"/>',
    "grid16": '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 4v16M12 4v16M16 4v16M4 8h16M4 12h16M4 16h16"/>',
    "film": '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><path d="M7.5 4.5v15M16.5 4.5v15M3.5 9h4M3.5 15h4M16.5 9h4M16.5 15h4"/>',
    "play": '<path d="M7.5 5.2v13.6a.8.8 0 0 0 1.2.7l10.6-6.8a.8.8 0 0 0 0-1.4L8.7 4.5a.8.8 0 0 0-1.2.7z"/>',
    "pause": '<rect x="6.5" y="5" width="3.6" height="14" rx="1"/><rect x="13.9" y="5" width="3.6" height="14" rx="1"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "record": '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.2"/>',
    "snapshot": '<path d="M4 8V6a2 2 0 0 1 2-2h2M16 4h2a2 2 0 0 1 2 2v2M20 16v2a2 2 0 0 1-2 2h-2M8 20H6a2 2 0 0 1-2-2v-2"/><circle cx="12" cy="12" r="3.5"/>',
    "fullscreen": '<path d="M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15"/>',
    "fullscreen_exit": '<path d="M9 4v3.5A1.5 1.5 0 0 1 7.5 9H4M20 9h-3.5A1.5 1.5 0 0 1 15 7.5V4M15 20v-3.5a1.5 1.5 0 0 1 1.5-1.5H20M4 15h3.5A1.5 1.5 0 0 1 9 16.5V20"/>',
    "expand": '<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>',
    "volume": '<path d="M4 9.5v5h3.5L12 19V5L7.5 9.5z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
    "mute": '<path d="M4 9.5v5h3.5L12 19V5L7.5 9.5z"/><path d="m16 9.5 5 5M21 9.5l-5 5"/>',
    "zoom_in": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.8-4.8M10.5 7.5v6M7.5 10.5h6"/>',
    "zoom_out": '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m20 20-4.8-4.8M7.5 10.5h6"/>',
    "focus": '<circle cx="12" cy="12" r="3"/><path d="M4 8V5.5A1.5 1.5 0 0 1 5.5 4H8M16 4h2.5A1.5 1.5 0 0 1 20 5.5V8M20 16v2.5a1.5 1.5 0 0 1-1.5 1.5H16M8 20H5.5A1.5 1.5 0 0 1 4 18.5V16"/>',
    "up": '<path d="m6 14.5 6-6 6 6"/>',
    "down": '<path d="m6 9.5 6 6 6-6"/>',
    "left": '<path d="m14.5 6-6 6 6 6"/>',
    "right": '<path d="m9.5 6 6 6-6 6"/>',
    "up_left": '<path d="M8 16V8h8"/>',
    "up_right": '<path d="M8 8h8v8"/>',
    "down_left": '<path d="M8 8v8h8"/>',
    "down_right": '<path d="M16 8v8H8"/>',
    "home": '<path d="M4 11.5 12 4.5l8 7"/><path d="M6.5 9.5V19a1 1 0 0 0 1 1H10v-5h4v5h2.5a1 1 0 0 0 1-1V9.5"/>',
    "target": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3.5"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/>',
    "bulb": '<path d="M9 17.5h6M10 20.5h4"/><path d="M12 3.5a6 6 0 0 0-3.6 10.8c.6.5 1 1.2 1 2v.2h5.2v-.2c0-.8.4-1.5 1-2A6 6 0 0 0 12 3.5z"/>',
    "infrared": '<circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M4.2 7.5l1.7 1M18.1 15.5l1.7 1M4.2 16.5l1.7-1M18.1 8.5l1.7-1"/><circle cx="12" cy="12" r="6.5" stroke-dasharray="2 2.6"/>',
    "siren": '<path d="M7 18v-6a5 5 0 0 1 10 0v6"/><path d="M5 18h14v2.5H5z"/><path d="M12 2.5v2M4.5 5l1.4 1.4M19.5 5l-1.4 1.4"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 14.6a1.5 1.5 0 0 0 .3 1.6l.1.1a1.8 1.8 0 1 1-2.6 2.6l-.1-.1a1.5 1.5 0 0 0-1.6-.3 1.5 1.5 0 0 0-.9 1.4v.2a1.8 1.8 0 1 1-3.6 0v-.1a1.5 1.5 0 0 0-1-1.4 1.5 1.5 0 0 0-1.6.3l-.1.1a1.8 1.8 0 1 1-2.6-2.6l.1-.1a1.5 1.5 0 0 0 .3-1.6 1.5 1.5 0 0 0-1.4-.9h-.2a1.8 1.8 0 1 1 0-3.6h.1a1.5 1.5 0 0 0 1.4-1 1.5 1.5 0 0 0-.3-1.6l-.1-.1a1.8 1.8 0 1 1 2.6-2.6l.1.1a1.5 1.5 0 0 0 1.6.3h.1a1.5 1.5 0 0 0 .9-1.4v-.2a1.8 1.8 0 1 1 3.6 0v.1a1.5 1.5 0 0 0 .9 1.4 1.5 1.5 0 0 0 1.6-.3l.1-.1a1.8 1.8 0 1 1 2.6 2.6l-.1.1a1.5 1.5 0 0 0-.3 1.6v.1a1.5 1.5 0 0 0 1.4.9h.2a1.8 1.8 0 1 1 0 3.6h-.1a1.5 1.5 0 0 0-1.4.9z"/>',
    "download": '<path d="M12 4v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M5 19.5h14"/>',
    "folder": '<path d="M3.5 7.5A1.5 1.5 0 0 1 5 6h4.2l2 2H19a1.5 1.5 0 0 1 1.5 1.5v8A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5z"/>',
    "calendar": '<rect x="4" y="5.5" width="16" height="14.5" rx="2"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
    "person": '<circle cx="12" cy="7.5" r="3.5"/><path d="M5 20.5a7 7 0 0 1 14 0"/>',
    "car": '<path d="M5 16.5V12l1.8-4.6A1.5 1.5 0 0 1 8.2 6.5h7.6a1.5 1.5 0 0 1 1.4.9L19 12v4.5"/><path d="M4 12h16v4.5H4z"/><path d="M6.5 16.5v2M17.5 16.5v2"/><circle cx="8" cy="14.2" r=".6"/><circle cx="16" cy="14.2" r=".6"/>',
    "paw": '<ellipse cx="12" cy="15.5" rx="4" ry="3.5"/><circle cx="6.5" cy="10.5" r="1.8"/><circle cx="17.5" cy="10.5" r="1.8"/><circle cx="9.5" cy="6.5" r="1.8"/><circle cx="14.5" cy="6.5" r="1.8"/>',
    "motion": '<path d="M4 12h3l2-5 3 10 2-6 1.5 3H20"/>',
    "bookmark": '<path d="M7 4h10a1 1 0 0 1 1 1v15l-6-4-6 4V5a1 1 0 0 1 1-1z"/>',
    "route": '<circle cx="6" cy="18" r="2.2"/><circle cx="18" cy="6" r="2.2"/><path d="M8.2 18H15a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h6.8"/>',
    "tele": '<circle cx="7" cy="15" r="3.8"/><circle cx="17" cy="15" r="3.8"/><path d="M10.8 15h2.4M4 12.5 6.5 5h2l1.5 6M20 12.5 17.5 5h-2L14 11"/>',
    "wide": '<path d="M3 6.5c6-1.8 12-1.8 18 0v11c-6-1.8-12-1.8-18 0z"/>',
    "split": '<rect x="3.5" y="5" width="17" height="14" rx="2"/><path d="M12 5v14"/>',
    "scan": '<circle cx="12" cy="12" r="8.5"/><path d="M12 12 18 6"/><circle cx="12" cy="12" r="4.5" stroke-dasharray="2.2 2.2"/>',
    "hdd": '<rect x="3.5" y="6" width="17" height="12" rx="2"/><path d="M3.5 13.5h17"/><circle cx="16.5" cy="15.8" r=".5"/>',
    "skip_back": '<path d="M5.5 5v14"/><path d="M18.5 5.8v12.4a.7.7 0 0 1-1.1.6L9 12.6a.7.7 0 0 1 0-1.2l8.4-6.2a.7.7 0 0 1 1.1.6z"/>',
    "skip_fwd": '<path d="M18.5 5v14"/><path d="M5.5 5.8v12.4a.7.7 0 0 0 1.1.6l8.4-6.2a.7.7 0 0 0 0-1.2L6.6 5.2a.7.7 0 0 0-1.1.6z"/>',
    "rewind10": '<path d="M4.5 12a7.5 7.5 0 1 0 2.3-5.4"/><path d="M4.5 4v4.5H9"/>',
    "forward10": '<path d="M19.5 12a7.5 7.5 0 1 1-2.3-5.4"/><path d="M19.5 4v4.5H15"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "eye": '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="3"/>',
    "cloud_off": '<path d="M3 3l18 18"/><path d="M7.5 7.8A5 5 0 0 0 6.5 17.5H17"/><path d="M11 5.2A6 6 0 0 1 18 11a4 4 0 0 1 2.2 6.2"/>',
    "power": '<path d="M12 3.5v8"/><path d="M7 6.5a7 7 0 1 0 10 0"/>',
    "sync": '<path d="M20 11.5A8 8 0 0 0 5.6 7.2L4 9"/><path d="M4 4.5V9h4.5"/><path d="M4 12.5a8 8 0 0 0 14.4 4.3L20 15"/><path d="M20 19.5V15h-4.5"/>',
    "sliders": '<path d="M4 8h8M16 8h4M4 16h4M12 16h8"/><circle cx="14" cy="8" r="2.2"/><circle cx="8" cy="16" r="2.2"/>',
    "wrench": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L4 16.8V20h3.2l5.3-5.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.5-.6-.6-2.5z"/>',
}
_PATHS["reconnect"] = _PATHS["refresh"]
_PATHS["recordings"] = _PATHS["film"]
_PATHS["live"] = _PATHS["grid"]

def svg(name: str, color: str, stroke: float = 1.9) -> str:
    body = _PATHS[name]
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="{stroke}" stroke-linecap="round" '
            f'stroke-linejoin="round">{body}</svg>')


def _render(svg_text: str, size: int, ratio: float = 2.0) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg_text.encode()))
    px = QPixmap(int(size * ratio), int(size * ratio))
    px.fill(Qt.transparent)
    painter = QPainter(px)
    painter.setRenderHint(QPainter.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px.width(), px.height()))
    painter.end()
    px.setDevicePixelRatio(ratio)
    return px


@lru_cache(maxsize=512)
def icon(name: str, color: str, disabled_color: str | None = None, size: int = 24) -> QIcon:
    ic = QIcon()
    ic.addPixmap(_render(svg(name, color), size), QIcon.Normal)
    if disabled_color:
        ic.addPixmap(_render(svg(name, disabled_color), size), QIcon.Disabled)
    return ic


def pixmap(name: str, color: str, size: int = 20) -> QPixmap:
    return _render(svg(name, color), size)


def write_svg_file(directory: Path, name: str, color: str, stroke: float = 2.4) -> Path:
    """Write an icon to disk so style sheets can reference it with url()."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}-{color.lstrip('#')}.svg"
    if not path.exists():
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(svg(name, color, stroke), encoding="utf-8")
        os.replace(tmp, path)
    return path
