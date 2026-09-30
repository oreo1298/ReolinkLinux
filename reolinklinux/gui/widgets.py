"""Small reusable widgets shared with the suite: cards, key/value grids, tiles, toasts,
plus theme-aware button helpers."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QObject, QPropertyAnimation, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import icons
from .theme import theme


def scaled_font(widget: QWidget, factor: float, bold: bool = False):
    font = widget.font()
    font.setPointSizeF(max(6.0, font.pointSizeF() * factor))
    if bold:
        font.setBold(True)
    return font


class Card(QFrame):
    """A rounded surface with an optional uppercase title, icon and trailing widgets."""

    def __init__(self, title: str | None = None, icon: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 12, 14, 14)
        self._layout.setSpacing(9)
        self.header = QHBoxLayout()
        self.header.setSpacing(8)
        self._icon_name = icon
        self.icon_label = QLabel()
        self.title_label = QLabel((title or "").upper())
        self.title_label.setObjectName("CardTitle")
        self.title_label.setFont(scaled_font(self.title_label, 0.82, bold=True))
        if title:
            if icon:
                self.header.addWidget(self.icon_label)
            self.header.addWidget(self.title_label)
            self.header.addStretch(1)
            self._layout.addLayout(self.header)
        theme.changed.connect(self._retheme)
        self._retheme(theme.palette)

    def _retheme(self, palette) -> None:
        if self._icon_name:
            self.icon_label.setPixmap(icons.pixmap(self._icon_name, palette.text_muted, 16))

    def add_header_widget(self, widget: QWidget) -> QWidget:
        self.header.addWidget(widget)
        return widget

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self._layout.addWidget(widget, stretch)
        return widget

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)


class KeyValueGrid(QWidget):
    """Two-column label/value list."""

    def __init__(self, columns: int = 1, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(6)
        self.columns = columns
        self._count = 0

    def clear(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._count = 0

    def add_row(self, label: str, value: str = "—") -> QLabel:
        row, col = divmod(self._count, self.columns)
        k = QLabel(label)
        k.setObjectName("Muted")
        k.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        v = QLabel(value)
        v.setObjectName("Value")
        v.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.setWordWrap(True)
        self.grid.addWidget(k, row, col * 2)
        self.grid.addWidget(v, row, col * 2 + 1)
        self.grid.setColumnStretch(col * 2 + 1, 1)
        self._count += 1
        return v


class StatTile(QFrame):
    """A compact value-over-caption tile."""

    def __init__(self, caption: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Inset")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(1)
        self.value = QLabel("—")
        self.value.setObjectName("Value")
        self.value.setFont(scaled_font(self.value, 1.12, bold=True))
        self.value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.caption = QLabel(caption)
        self.caption.setObjectName("Muted")
        self.caption.setFont(scaled_font(self.caption, 0.85))
        lay.addWidget(self.value)
        lay.addWidget(self.caption)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set(self, value: str, tooltip: str = "") -> None:
        self.value.setText(value)
        self.setToolTip(tooltip)


class StatusDot(QWidget):
    """A small filled circle with an optional soft halo."""

    def __init__(self, size: int = 10, parent=None):
        super().__init__(parent)
        self._color = QColor("#888")
        self._halo = True
        self.setFixedSize(QSize(size + 8, size + 8))

    def set_color(self, color: str, halo: bool = True) -> None:
        self._color = QColor(color)
        self._halo = halo
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        if self._halo:
            halo = QColor(self._color)
            halo.setAlpha(55)
            p.setBrush(halo)
            p.setPen(Qt.NoPen)
            p.drawEllipse(r)
        inner = r.adjusted(4, 4, -4, -4)
        p.setBrush(self._color)
        p.setPen(Qt.NoPen)
        p.drawEllipse(inner)


class Toast(QWidget):
    """A transient notification floating near the bottom right of its parent."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._icon = QLabel()
        self._text = QLabel()
        self._text.setWordWrap(True)
        self._text.setMaximumWidth(460)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 16, 10)
        lay.setSpacing(10)
        lay.addWidget(self._icon, 0, Qt.AlignTop)
        lay.addWidget(self._text, 1)
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._timer = QTimer(self, singleShot=True)
        self._timer.timeout.connect(self._fade_out)
        self._accent = QColor("#5b8cff")
        self._fading = False
        self._anim.finished.connect(self._on_anim_finished)
        self.hide()

    def _on_anim_finished(self) -> None:
        if self._fading:
            self._fading = False
            self.hide()

    def show_message(self, text: str, kind: str = "success", timeout_ms: int = 3800) -> None:
        pal = theme.palette
        color = {"success": pal.success, "warning": pal.warning, "error": pal.danger,
                 "info": pal.accent}.get(kind, pal.accent)
        name = {"success": "check", "warning": "warning", "error": "error"}.get(kind, "info")
        self._accent = QColor(color)
        self._icon.setPixmap(icons.pixmap(name, color, 18))
        self._text.setText(text)
        self.adjustSize()
        self._place()
        self.raise_()
        self.show()
        self._anim.stop()
        self._fading = False
        self._anim.setDuration(180)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()
        self._timer.start(timeout_ms)

    def _fade_out(self) -> None:
        self._anim.stop()
        self._fading = True
        self._anim.setDuration(400)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.start()

    def _place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        size = self.sizeHint()
        x = parent.width() - size.width() - 22
        y = parent.height() - size.height() - 24
        self.setGeometry(QRect(x, y, size.width(), size.height()))

    def mousePressEvent(self, _event) -> None:  # noqa: N802
        self._timer.stop()
        self._fade_out()

    def paintEvent(self, _event) -> None:  # noqa: N802
        pal = theme.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.fillPath(path, QColor(pal.raised))
        p.setPen(QColor(pal.border_strong))
        p.drawPath(path)
        bar = QPainterPath()
        bar.addRoundedRect(rect.adjusted(0, 0, -(rect.width() - 4), 0), 2, 2)
        p.fillPath(bar, self._accent)


class SegmentedControl(QFrame):
    """A row of mutually exclusive toggle buttons."""

    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("Segmented")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(3, 3, 3, 3)
        self._layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._values: list[object] = []
        self._group.idClicked.connect(lambda i: self.changed.emit(self._values[i]))

    def add(self, label: str, value: object, tooltip: str = "") -> None:
        btn = QToolButton()
        btn.setObjectName("Segment")
        btn.setText(label)
        btn.setToolTip(tooltip)
        btn.setCheckable(True)
        btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        btn.setCursor(Qt.PointingHandCursor)
        self._group.addButton(btn, len(self._values))
        self._values.append(value)
        self._layout.addWidget(btn)

    def value(self) -> object:
        i = self._group.checkedId()
        return self._values[i] if i >= 0 else None

    def set_value(self, value: object) -> None:
        if value in self._values:
            self._group.button(self._values.index(value)).setChecked(True)


class _IconBinder(QObject):
    """Re-tints a widget's icon whenever the theme changes (lives as the widget's child)."""

    def __init__(self, target, name: str, token: str, size: int | None = None):
        super().__init__(target)
        self._target = target
        self.name = name
        self.token = token
        self.size = size
        theme.changed.connect(self.apply)
        self.apply(theme.palette)

    def set_name(self, name: str, token: str | None = None) -> None:
        self.name = name
        if token:
            self.token = token
        self.apply(theme.palette)

    def apply(self, _palette=None) -> None:
        if isinstance(self._target, QLabel):
            p = theme.palette
            self._target.setPixmap(icons.pixmap(self.name, getattr(p, self.token), self.size or 18))
        else:
            self._target.setIcon(theme.icon(self.name, self.token))


def bind_icon(widget, name: str, token: str = "text", size: int | None = None) -> _IconBinder:
    return _IconBinder(widget, name, token, size)


def tool_button(text: str, icon: str, tooltip: str = "", checkable: bool = False,
                under: bool = True, icon_size: int = 22) -> QToolButton:
    btn = QToolButton()
    btn.setText(text)
    btn.setToolTip(tooltip or text)
    btn.setCheckable(checkable)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setIconSize(QSize(icon_size, icon_size))
    btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon if under else
                           (Qt.ToolButtonTextBesideIcon if text else Qt.ToolButtonIconOnly))
    btn._binder = bind_icon(btn, icon)
    return btn


def flat_button(icon: str, tooltip: str, checkable: bool = False, size: int = 18) -> QToolButton:
    btn = QToolButton()
    btn.setObjectName("Flat")
    btn.setToolTip(tooltip)
    btn.setCheckable(checkable)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setIconSize(QSize(size, size))
    btn._binder = bind_icon(btn, icon, "text_muted")
    return btn


def chip(text: str, checked: bool = False) -> QToolButton:
    btn = QToolButton()
    btn.setObjectName("Chip")
    btn.setText(text)
    btn.setCheckable(True)
    btn.setChecked(checked)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


def set_prop(widget: QWidget, name: str, value) -> None:
    """Set a dynamic property used by the style sheet and re-polish the widget."""
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
