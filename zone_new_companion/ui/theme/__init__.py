"""App-wide look: Fusion style, a palette and one stylesheet built from design tokens.

Every color the UI uses lives in ``TOKENS``; widgets pick a look through dynamic
properties (``role``/``variant``) that ``style.qss`` styles, instead of carrying
their own stylesheets. ``mode`` is "system" (follow the OS), "light" or "dark".
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from string import Template

from PyQt6 import sip
from PyQt6.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QWidget

MODES = ("system", "light", "dark")

TOKENS: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0f1115",
        "surface": "#161a21",
        "surface_alt": "#1a1f27",
        "raised": "#212732",
        "hover": "#2a3140",
        "pressed": "#323a4b",
        "border": "#262c37",
        "border_strong": "#363e4d",
        "text": "#e6e9ef",
        "text_muted": "#8b93a3",
        "text_disabled": "#5a6170",
        "accent": "#4f8cff",
        "accent_hover": "#6b9fff",
        "accent_soft": "#1f3459",
        "on_accent": "#ffffff",
        "success": "#3fb950",
        "warning": "#e3a72f",
        "danger": "#f85149",
        "star": "#f5c518",
        "scroll": "#363e4d",
        "scroll_hover": "#4a5468",
        "toast_bg": "#262d3a",
        "xtream": "#58a6ff",
        "m3u": "#ffa657",
        "stalker": "#d2a8ff",
    },
    "light": {
        "bg": "#f4f5f8",
        "surface": "#ffffff",
        "surface_alt": "#f8f9fb",
        "raised": "#ffffff",
        "hover": "#eef1f6",
        "pressed": "#e2e7ef",
        "border": "#e3e6ec",
        "border_strong": "#cdd3dc",
        "text": "#1b2130",
        "text_muted": "#626b7c",
        "text_disabled": "#a3aab7",
        "accent": "#2563eb",
        "accent_hover": "#1d4fd8",
        "accent_soft": "#dbe7fe",
        "on_accent": "#ffffff",
        "success": "#1a7f37",
        "warning": "#9a6700",
        "danger": "#cf222e",
        "star": "#d4a106",
        "scroll": "#cdd3dc",
        "scroll_hover": "#aab2c0",
        "toast_bg": "#1b2130",
        "xtream": "#1f6feb",
        "m3u": "#c2410c",
        "stalker": "#8250df",
    },
}

# Font size of body text, in device-independent pixels.
BASE_FONT_PX = 13

_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3])) / "zone_new_companion" / "ui" / "theme"


class _ThemeManager(QObject):
    """Holds the active mode/tokens and tells widgets when they change."""

    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.mode = "system"
        self.name = "dark"
        self._icons: dict[tuple, QIcon] = {}
        self._bound: list[tuple[QWidget, str, str]] = []
        self._hooked = False

    @property
    def tokens(self) -> dict[str, str]:
        return TOKENS[self.name]

    def apply(self, app: QApplication, mode: str) -> None:
        self.mode = mode if mode in MODES else "system"
        hints = app.styleHints()
        if not self._hooked:
            self._hooked = True
            _load_fonts(app)
            app.setStyle("Fusion")
            hints.colorSchemeChanged.connect(lambda _scheme: self._follow_system(app))
        # Native title bars (Windows/macOS) follow this too.
        scheme = {"light": Qt.ColorScheme.Light, "dark": Qt.ColorScheme.Dark}.get(self.mode, Qt.ColorScheme.Unknown)
        hints.setColorScheme(scheme)
        self._render(app)

    def _follow_system(self, app: QApplication) -> None:
        if self.mode == "system":
            self._render(app)

    def _render(self, app: QApplication) -> None:
        if self.mode == "system":
            name = "light" if app.styleHints().colorScheme() == Qt.ColorScheme.Light else "dark"
        else:
            name = self.mode
        if name == self.name and app.styleSheet():
            return
        self.name = name
        self._icons.clear()
        app.setPalette(_palette(self.tokens))
        app.setStyleSheet(_stylesheet(self.tokens))
        self._bound = [entry for entry in self._bound if not sip.isdeleted(entry[0])]
        for widget, icon_name, token in self._bound:
            widget.setIcon(self.icon(icon_name, token))
        self.changed.emit()

    def color(self, token: str, alpha: float = 1.0) -> QColor:
        color = QColor(self.tokens[token])
        color.setAlphaF(alpha)
        return color

    def icon(self, name: str, token: str = "text", filled: bool = False) -> QIcon:
        key = (name, token, filled)
        if key not in self._icons:
            svg = (_DIR / "icons" / f"{name}.svg").read_text(encoding="utf-8")
            self._icons[key] = QIcon(_SvgIconEngine(
                _tint(svg, self.tokens[token], filled),
                _tint(svg, self.tokens["text_disabled"], filled),
            ))
        return self._icons[key]

    def bind_icon(self, widget: QWidget, name: str, token: str = "text") -> None:
        """Give a button/action an icon that is recolored when the theme changes."""
        widget.setIcon(self.icon(name, token))
        self._bound.append((widget, name, token))


theme = _ThemeManager()


def set_role(widget: QWidget, role: str) -> QWidget:
    """Style a widget through a ``role`` from style.qss (title, muted, card…)."""
    widget.setProperty("role", role)
    return widget


def set_variant(widget: QWidget, variant: str) -> QWidget:
    """Pick a button look from style.qss: primary, ghost, danger, disclosure."""
    widget.setProperty("variant", variant)
    return widget


class _SvgIconEngine(QIconEngine):
    """Draws an SVG at whatever size and pixel density the icon is asked for."""

    def __init__(self, normal: bytes, disabled: bytes) -> None:
        super().__init__()
        self._data = (normal, disabled)
        self._normal = QSvgRenderer(normal)
        self._disabled = QSvgRenderer(disabled)

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        renderer = self._disabled if mode == QIcon.Mode.Disabled else self._normal
        renderer.render(painter, QRectF(rect))

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:  # noqa: N802
        pixmap = QPixmap(size * scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paint(painter, QRect(QPoint(0, 0), pixmap.size()), mode, state)
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        return pixmap

    def clone(self) -> QIconEngine:
        return _SvgIconEngine(*self._data)


def _tint(svg: str, color: str, filled: bool) -> bytes:
    svg = svg.replace("currentColor", color)
    if filled:
        svg = svg.replace('fill="none"', f'fill="{color}"')
    return svg.encode("utf-8")


def _icon_file(name: str, color: str) -> str:
    """A recolored copy of an icon on disk, for stylesheet ``url()``s."""
    folder = Path(tempfile.gettempdir()) / "zone-new-companion-icons"
    folder.mkdir(exist_ok=True)
    path = folder / f"{name}-{color.lstrip('#')}.svg"
    if not path.exists():
        path.write_bytes(_tint((_DIR / "icons" / f"{name}.svg").read_text(encoding="utf-8"), color, False))
    return path.as_posix()


def _load_fonts(app: QApplication) -> None:
    for file in sorted((_DIR / "fonts").glob("*.ttf")):
        # Qt only loads application fonts from absolute paths.
        QFontDatabase.addApplicationFont(os.fspath(file.resolve()))
    font = QFont("Inter")
    font.setPixelSize(BASE_FONT_PX)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)


def _palette(t: dict[str, str]) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: t["bg"],
        QPalette.ColorRole.WindowText: t["text"],
        QPalette.ColorRole.Base: t["surface"],
        QPalette.ColorRole.AlternateBase: t["surface_alt"],
        QPalette.ColorRole.ToolTipBase: t["raised"],
        QPalette.ColorRole.ToolTipText: t["text"],
        QPalette.ColorRole.PlaceholderText: t["text_muted"],
        QPalette.ColorRole.Text: t["text"],
        QPalette.ColorRole.Button: t["raised"],
        QPalette.ColorRole.ButtonText: t["text"],
        QPalette.ColorRole.BrightText: t["danger"],
        QPalette.ColorRole.Light: t["hover"],
        QPalette.ColorRole.Midlight: t["border_strong"],
        QPalette.ColorRole.Mid: t["border"],
        QPalette.ColorRole.Dark: t["border"],
        QPalette.ColorRole.Shadow: "#000000",
        QPalette.ColorRole.Highlight: t["accent"],
        QPalette.ColorRole.HighlightedText: t["on_accent"],
        QPalette.ColorRole.Link: t["accent"],
        QPalette.ColorRole.LinkVisited: t["accent"],
        QPalette.ColorRole.Accent: t["accent"],
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["text_disabled"]))
    return palette


def _stylesheet(t: dict[str, str]) -> str:
    template = Template((_DIR / "style.qss").read_text(encoding="utf-8"))
    return template.substitute(
        t,
        chevron_down=_icon_file("chevron-down", t["text_muted"]),
        chevron_on_accent=_icon_file("chevron-down", t["on_accent"]),
        base_px=BASE_FONT_PX,
        title_px=BASE_FONT_PX + 4,
    )


def is_dark() -> bool:
    return theme.name == "dark"


__all__ = ["MODES", "TOKENS", "is_dark", "set_role", "set_variant", "theme"]
