"""Still images for the demo cameras, drawn with QPainter and cached as PNG files."""

from __future__ import annotations

import os
import random
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPolygonF, QRadialGradient

VERSION = 3


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / "reolinklinux" / "demo"


def scene_path(kind: str, lens: int) -> str:
    path = cache_dir() / f"{kind}_{lens}_v{VERSION}.png"
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            img = render(kind, lens)
            tmp = path.with_suffix(".tmp.png")
            img.save(str(tmp), "PNG")
            os.replace(tmp, path)
        except OSError:
            return ""
    return str(path)


def render(kind: str, lens: int) -> QImage:
    if kind == "duo2":
        return _driveway(2304, 864)
    if kind == "trackmix":
        return _garden_tele(1920, 1080) if lens == 1 else _garden(1920, 1080)
    return _porch_ir(1920, 1080)


def _grad(p: QPainter, rect: QRectF, stops: list[tuple[float, str]], vertical: bool = True) -> None:
    g = QLinearGradient(rect.topLeft(), rect.bottomLeft() if vertical else rect.topRight())
    for pos, color in stops:
        g.setColorAt(pos, QColor(color))
    p.fillRect(rect, QBrush(g))


def _glow(p: QPainter, center: QPointF, radius: float, color: str, alpha: int = 160) -> None:
    g = QRadialGradient(center, radius)
    c = QColor(color)
    c.setAlpha(alpha)
    g.setColorAt(0, c)
    c2 = QColor(color)
    c2.setAlpha(0)
    g.setColorAt(1, c2)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(g))
    p.drawEllipse(center, radius, radius)


def _tree(p: QPainter, x: float, base: float, h: float, color: str, rng: random.Random) -> None:
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1c1a17"))
    p.drawRect(QRectF(x - h * 0.03, base - h * 0.35, h * 0.06, h * 0.35))
    p.setBrush(QColor(color))
    for _ in range(7):
        r = h * rng.uniform(0.16, 0.26)
        p.drawEllipse(QPointF(x + rng.uniform(-h * 0.18, h * 0.18), base - h * rng.uniform(0.45, 0.85)), r, r)


def _driveway(w: int, h: int) -> QImage:
    rng = random.Random(7)
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    horizon = h * 0.42
    _grad(p, QRectF(0, 0, w, horizon), [(0, "#0b1630"), (0.55, "#2a3e6b"), (0.85, "#c9765a"), (1, "#f0a868")])
    for _ in range(60):
        p.setPen(QColor(255, 255, 255, rng.randint(60, 160)))
        p.drawPoint(QPointF(rng.uniform(0, w), rng.uniform(0, horizon * 0.5)))
    # distant houses
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1b2233"))
    x = 0.0
    while x < w:
        bw, bh = rng.uniform(120, 260), rng.uniform(40, 90)
        p.drawRect(QRectF(x, horizon - bh, bw, bh))
        roof = QPolygonF([QPointF(x - 6, horizon - bh), QPointF(x + bw / 2, horizon - bh - bh * 0.5),
                          QPointF(x + bw + 6, horizon - bh)])
        p.drawPolygon(roof)
        if rng.random() < 0.6:
            p.setBrush(QColor("#f4c46a"))
            p.drawRect(QRectF(x + bw * 0.3, horizon - bh * 0.6, 12, 12))
            p.setBrush(QColor("#1b2233"))
        x += bw + rng.uniform(40, 140)
    for _ in range(9):
        _tree(p, rng.uniform(0, w), horizon + 6, rng.uniform(140, 220), "#16241c", rng)
    # lawn
    _grad(p, QRectF(0, horizon, w, h - horizon), [(0, "#243623"), (1, "#101a10")])
    # driveway (wide-angle perspective)
    drive = QPolygonF([QPointF(w * 0.42, horizon + 8), QPointF(w * 0.58, horizon + 8), QPointF(w * 0.98, h),
                       QPointF(w * 0.02, h)])
    path = QPainterPath()
    path.addPolygon(drive)
    g = QLinearGradient(0, horizon, 0, h)
    g.setColorAt(0, QColor("#4a4d55"))
    g.setColorAt(1, QColor("#2a2c31"))
    p.fillPath(path, QBrush(g))
    p.setPen(QColor(255, 255, 255, 30))
    for i in range(1, 8):
        t = i / 8
        y = horizon + 8 + (h - horizon - 8) * t * t
        p.drawLine(QPointF(w * (0.42 - 0.40 * t * t), y), QPointF(w * (0.58 + 0.40 * t * t), y))
    # house on the left
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#2d3140"))
    p.drawRect(QRectF(0, horizon - h * 0.2, w * 0.2, h * 0.42))
    p.setBrush(QColor("#1d202a"))
    p.drawPolygon(QPolygonF([QPointF(-20, horizon - h * 0.2), QPointF(w * 0.1, horizon - h * 0.36),
                             QPointF(w * 0.22, horizon - h * 0.2)]))
    for wx in (0.03, 0.12):
        p.setBrush(QColor("#f6c56f"))
        p.drawRect(QRectF(w * wx, horizon - h * 0.12, w * 0.05, h * 0.09))
        _glow(p, QPointF(w * (wx + 0.025), horizon - h * 0.075), h * 0.12, "#f6c56f", 60)
    # garage door
    p.setBrush(QColor("#3b3f4d"))
    p.drawRect(QRectF(w * 0.2, horizon - h * 0.08, w * 0.12, h * 0.3))
    p.setPen(QColor(0, 0, 0, 60))
    for i in range(1, 6):
        y = horizon - h * 0.08 + i * h * 0.05
        p.drawLine(QPointF(w * 0.2, y), QPointF(w * 0.32, y))
    # car
    p.setPen(Qt.NoPen)
    cx, cy = w * 0.5, h * 0.74
    p.setBrush(QColor("#7b2530"))
    body = QPainterPath()
    body.addRoundedRect(QRectF(cx - w * 0.09, cy - h * 0.08, w * 0.18, h * 0.12), 18, 18)
    p.fillPath(body, QColor("#8a2a35"))
    cabin = QPolygonF([QPointF(cx - w * 0.055, cy - h * 0.08), QPointF(cx - w * 0.035, cy - h * 0.16),
                       QPointF(cx + w * 0.035, cy - h * 0.16), QPointF(cx + w * 0.055, cy - h * 0.08)])
    p.setBrush(QColor("#6d1f29"))
    p.drawPolygon(cabin)
    p.setBrush(QColor("#9fc3e6"))
    p.drawPolygon(QPolygonF([QPointF(cx - w * 0.045, cy - h * 0.085), QPointF(cx - w * 0.03, cy - h * 0.145),
                             QPointF(cx + w * 0.03, cy - h * 0.145), QPointF(cx + w * 0.045, cy - h * 0.085)]))
    p.setBrush(QColor("#111"))
    for dx in (-0.06, 0.06):
        p.drawEllipse(QPointF(cx + w * dx, cy + h * 0.04), h * 0.035, h * 0.035)
    for dx in (-0.075, 0.075):
        _glow(p, QPointF(cx + w * dx, cy - h * 0.03), h * 0.05, "#ffe6a8", 120)
    # street lamp
    p.setBrush(QColor("#15171c"))
    p.drawRect(QRectF(w * 0.8, horizon - h * 0.25, 8, h * 0.45))
    _glow(p, QPointF(w * 0.8 + 4, horizon - h * 0.25), h * 0.22, "#ffd28a", 110)
    p.setBrush(QColor("#fff2cf"))
    p.drawEllipse(QPointF(w * 0.8 + 4, horizon - h * 0.25), 10, 10)
    p.end()
    return img


def _garden(w: int, h: int) -> QImage:
    rng = random.Random(3)
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    horizon = h * 0.38
    _grad(p, QRectF(0, 0, w, horizon), [(0, "#6fa3d8"), (1, "#cfe3f2")])
    p.setPen(Qt.NoPen)
    for _ in range(5):
        cx, cy = rng.uniform(0, w), rng.uniform(h * 0.05, h * 0.2)
        p.setBrush(QColor(255, 255, 255, 170))
        for _ in range(5):
            p.drawEllipse(QPointF(cx + rng.uniform(-80, 80), cy + rng.uniform(-15, 15)), rng.uniform(40, 70), 26)
    for i in range(12):
        _tree(p, i * w / 11 + rng.uniform(-30, 30), horizon + 40, rng.uniform(260, 380), "#3d6b3a", rng)
    _grad(p, QRectF(0, horizon + 30, w, h - horizon), [(0, "#5f9a45"), (1, "#3f7a30")])
    # fence
    p.setBrush(QColor("#8a6a4a"))
    for i in range(0, w, 70):
        p.drawRect(QRectF(i, horizon - 30, 60, 90))
    p.setBrush(QColor("#6f5238"))
    p.drawRect(QRectF(0, horizon - 10, w, 12))
    p.drawRect(QRectF(0, horizon + 35, w, 12))
    # shed
    sx, sy = w * 0.68, horizon - 40
    p.setBrush(QColor("#a0714a"))
    p.drawRect(QRectF(sx, sy, w * 0.2, h * 0.3))
    p.setBrush(QColor("#5a3d2a"))
    p.drawPolygon(QPolygonF([QPointF(sx - 30, sy), QPointF(sx + w * 0.1, sy - h * 0.13), QPointF(sx + w * 0.2 + 30, sy)]))
    p.setBrush(QColor("#6f4a30"))
    p.drawRect(QRectF(sx + w * 0.07, sy + h * 0.1, w * 0.06, h * 0.2))
    p.setBrush(QColor("#d8ecf7"))
    p.drawRect(QRectF(sx + w * 0.015, sy + h * 0.07, w * 0.04, h * 0.06))
    # patio
    pat = QPolygonF([QPointF(0, h * 0.78), QPointF(w * 0.55, h * 0.72), QPointF(w * 0.62, h), QPointF(0, h)])
    p.setBrush(QColor("#b9b2a6"))
    p.drawPolygon(pat)
    p.setPen(QColor(0, 0, 0, 40))
    for i in range(1, 6):
        p.drawLine(QPointF(0, h * 0.78 + i * h * 0.045), QPointF(w * 0.6, h * 0.72 + i * h * 0.056))
    for i in range(1, 9):
        p.drawLine(QPointF(i * w * 0.07, h * 0.78 - i * 4), QPointF(i * w * 0.075, h))
    # table + chairs
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#f1efe8"))
    p.drawEllipse(QPointF(w * 0.28, h * 0.82), w * 0.08, h * 0.035)
    p.setBrush(QColor("#9a9a9a"))
    p.drawRect(QRectF(w * 0.277, h * 0.82, 10, h * 0.1))
    p.setBrush(QColor("#2f6f8f"))
    p.drawRect(QRectF(w * 0.16, h * 0.78, w * 0.04, h * 0.12))
    p.drawRect(QRectF(w * 0.36, h * 0.78, w * 0.04, h * 0.12))
    # flower bed
    for _ in range(80):
        p.setBrush(QColor(rng.choice(["#e0567a", "#f3c64f", "#ffffff", "#b06ad8"])))
        p.drawEllipse(QPointF(rng.uniform(w * 0.62, w), rng.uniform(h * 0.72, h * 0.8)), 7, 7)
    p.end()
    return img


def _garden_tele(w: int, h: int) -> QImage:
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    _grad(p, QRectF(0, 0, w, h * 0.3), [(0, "#9cc4e4"), (1, "#d7e9f5")])
    _grad(p, QRectF(0, h * 0.3, w, h * 0.7), [(0, "#6aa24d"), (1, "#4a8a36")])
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#a87650"))
    p.drawRect(QRectF(w * 0.12, h * 0.18, w * 0.76, h * 0.72))
    p.setPen(QColor(0, 0, 0, 45))
    for i in range(1, 16):
        x = w * 0.12 + i * w * 0.76 / 16
        p.drawLine(QPointF(x, h * 0.18), QPointF(x, h * 0.9))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#5a3d2a"))
    p.drawPolygon(QPolygonF([QPointF(w * 0.06, h * 0.2), QPointF(w * 0.5, -h * 0.1), QPointF(w * 0.94, h * 0.2)]))
    p.setBrush(QColor("#6c4630"))
    p.drawRect(QRectF(w * 0.4, h * 0.36, w * 0.22, h * 0.54))
    p.setBrush(QColor("#c8a24a"))
    p.drawEllipse(QPointF(w * 0.595, h * 0.64), 14, 14)
    p.setBrush(QColor("#2c2c2c"))
    p.drawRect(QRectF(w * 0.585, h * 0.6, 20, 60))
    p.setBrush(QColor("#dcecf6"))
    p.drawRect(QRectF(w * 0.17, h * 0.34, w * 0.16, h * 0.2))
    p.setBrush(QColor("#6c4630"))
    p.drawRect(QRectF(w * 0.249, h * 0.34, 10, h * 0.2))
    p.drawRect(QRectF(w * 0.17, h * 0.435, w * 0.16, 10))
    p.setBrush(QColor("#8a8f96"))
    p.drawRect(QRectF(w * 0.7, h * 0.7, w * 0.08, h * 0.22))
    p.setBrush(QColor("#4b7a3a"))
    p.drawEllipse(QPointF(w * 0.74, h * 0.66), w * 0.06, h * 0.08)
    p.end()
    return img


def _porch_ir(w: int, h: int) -> QImage:
    rng = random.Random(11)
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    _grad(p, QRectF(0, 0, w, h), [(0, "#2b2b2b"), (0.6, "#4a4a4a"), (1, "#6a6a6a")])
    p.setPen(Qt.NoPen)
    # brick wall
    for row in range(0, int(h * 0.75), 36):
        off = 0 if (row // 36) % 2 == 0 else 45
        for col in range(-45, w, 90):
            shade = rng.randint(60, 80)
            p.setBrush(QColor(shade, shade, shade))
            p.drawRect(QRectF(col + off, row, 86, 32))
    # door
    p.setBrush(QColor("#1e1e1e"))
    p.drawRect(QRectF(w * 0.4, h * 0.12, w * 0.2, h * 0.63))
    p.setBrush(QColor("#2e2e2e"))
    for i in range(3):
        p.drawRect(QRectF(w * 0.42, h * (0.16 + i * 0.19), w * 0.16, h * 0.15))
    p.setBrush(QColor("#bdbdbd"))
    p.drawEllipse(QPointF(w * 0.575, h * 0.46), 11, 11)
    # lamp
    p.setBrush(QColor("#9a9a9a"))
    p.drawRect(QRectF(w * 0.66, h * 0.2, 36, 60))
    _glow(p, QPointF(w * 0.66 + 18, h * 0.24), h * 0.25, "#ffffff", 90)
    # steps
    for i in range(3):
        c = 110 + i * 20
        p.setBrush(QColor(c, c, c))
        p.drawRect(QRectF(w * (0.34 - i * 0.04), h * (0.75 + i * 0.08), w * (0.32 + i * 0.08), h * 0.08))
    # plants
    for x in (0.25, 0.73):
        p.setBrush(QColor("#505050"))
        p.drawRect(QRectF(w * x - 50, h * 0.6, 100, h * 0.15))
        p.setBrush(QColor("#8d8d8d"))
        for _ in range(12):
            p.drawEllipse(QPointF(w * x + rng.uniform(-60, 60), h * 0.58 - rng.uniform(0, 120)), 30, 30)
    # doormat
    p.setBrush(QColor("#3a3a3a"))
    p.drawRect(QRectF(w * 0.42, h * 0.74, w * 0.16, h * 0.03))
    # vignette
    g = QRadialGradient(QPointF(w / 2, h / 2), w * 0.7)
    g.setColorAt(0.6, QColor(0, 0, 0, 0))
    g.setColorAt(1, QColor(0, 0, 0, 170))
    p.fillRect(QRectF(0, 0, w, h), QBrush(g))
    p.end()
    return img
