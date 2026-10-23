# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared UI kit of the ecosystem: design tokens, light/dark themes, fonts, icons and common components.

Every app of the project builds its interface on this module, so they share one visual language: warm neutral
surfaces, one accent color per app, few borders, soft depth only on floating elements, Inter for the interface
and Newsreader for long reading. The module has no user-visible text of its own: apps pass already
translated strings.

Usage:
    import ui_kit as kit
    C = kit.T                                   # current tokens, updated in place when the theme changes
    theme = kit.ThemeManager(app, "ember", preference, lambda t: kit.base_qss(t) + app_qss(t))
    kit.signals.changed.connect(...)            # to refresh what depends on colors
"""
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QGuiApplication, QIcon, QPainter, QPainterPath, QPalette,
                           QPen, QPixmap)
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPushButton, QSizePolicy, QStyle, QStyledItemDelegate,
                               QVBoxLayout, QWidget)

KIT_DIR = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(KIT_DIR, "fonts")

# ---------------------------------------------------------------------------
# Design tokens
# ---------------------------------------------------------------------------
# Warm, low-saturation neutrals (no blue cast). The sidebar is a notch dimmer than the content so navigation
# recedes; borders are soft and used sparingly: surfaces, not lines, give the structure.
NEUTRALS = {
    "light": {
        "bg": "#f6f4f1", "sidebar": "#efece7", "surface": "#ffffff", "surface2": "#faf9f7",
        "hover": "#eceae5", "border": "#e7e3dc", "border_strong": "#d3cdc3", "text": "#1d1b18",
        "muted": "#67625b", "faint": "#a39d94", "ok": "#2c7a47", "warn": "#8c5a00", "err": "#b3372b",
        "shadow": "#000000", "shadow_alpha": 0.10, "scrim": "#1d1b18",
    },
    "dark": {
        "bg": "#151413", "sidebar": "#111010", "surface": "#1d1c1a", "surface2": "#242321",
        "hover": "#2b2927", "border": "#2d2b28", "border_strong": "#3d3a36", "text": "#eeece8",
        "muted": "#a59f96", "faint": "#6d6861", "ok": "#62c584", "warn": "#e4a83f", "err": "#f2786a",
        "shadow": "#000000", "shadow_alpha": 0.45, "scrim": "#000000",
    },
}
# One accent per app. "accent_fg" is the text on accent-filled buttons, "accent_soft" the tint for
# selections and soft buttons, "accent_text" the accent used as text on surfaces.
ACCENTS = {
    # Lectern: warm ember / terracotta
    "ember": {
        "light": {"accent": "#b4471f", "accent_hover": "#963a18", "accent_fg": "#ffffff",
                  "accent_soft": "#f6e4da", "accent_text": "#a8421c"},
        "dark": {"accent": "#ef8a5c", "accent_hover": "#f5a07a", "accent_fg": "#1c0f08",
                 "accent_soft": "#3a2419", "accent_text": "#f29a71"},
    },
    # OVMS Manager: luminous electric blue
    "electric": {
        "light": {"accent": "#2552e0", "accent_hover": "#1c43bd", "accent_fg": "#ffffff",
                  "accent_soft": "#e2e9fd", "accent_text": "#2049c9"},
        "dark": {"accent": "#7da3ff", "accent_hover": "#9cb9ff", "accent_fg": "#0a1330",
                 "accent_soft": "#1c2546", "accent_text": "#8daeff"},
    },
}
# A "terminal" surface that stays dark in both themes (logs, consoles)
CONSOLE = {"console_bg": "#121110", "console_text": "#d9d5ce", "console_muted": "#8b857c", "console_ok": "#62c584",
           "console_warn": "#e4a83f", "console_err": "#f2786a", "console_cmd": "#8daeff", "console_head": "#ffffff"}
RADIUS = {"sm": 6, "md": 9, "lg": 12, "xl": 16}

T = {}  # current tokens: apps alias it (C = kit.T); updated in place, so painted widgets always see the theme


def tokens(scheme, accent):
    return {**NEUTRALS[scheme], **ACCENTS[accent][scheme], **CONSOLE, "scheme": scheme, "focus": None}


def alpha(color, a):
    c = QColor(color)
    c.setAlphaF(a)
    return c.name(QColor.NameFormat.HexArgb)


class _Signals(QObject):
    changed = Signal()  # the theme changed: refresh icons and inline styles


signals = _Signals()


def reduce_motion_default():
    """True if the desktop asks for fewer animations (GNOME's enable-animations), else False."""
    if not shutil.which("gsettings"):
        return False
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "enable-animations"],
                             capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    return out == "false"


class ThemeManager(QObject):
    """Applies the light or dark theme: tokens in T, QPalette and style sheet. `preference` is "system",
    "light" or "dark"; with "system" it follows the desktop live."""
    changed = Signal()

    def __init__(self, app, accent, preference, qss_builder):
        super().__init__(app)
        self.app, self.accent, self.qss_builder = app, accent, qss_builder
        self.preference = preference if preference in ("system", "light", "dark") else "system"
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(lambda *_: self.preference == "system" and self.apply())
        self.apply()

    @staticmethod
    def system_scheme():
        hints = QGuiApplication.styleHints()
        scheme = hints.colorScheme() if hasattr(hints, "colorScheme") else None
        return "dark" if scheme == Qt.ColorScheme.Dark else "light"

    def scheme(self):
        return self.preference if self.preference in ("light", "dark") else self.system_scheme()

    def set_preference(self, preference):
        self.preference = preference
        self.apply()

    def apply(self):
        T.clear()
        T.update(tokens(self.scheme(), self.accent))
        T["focus"] = T["accent"]
        self.app.setPalette(qpalette(T))
        self.app.setStyleSheet(self.qss_builder(T))
        signals.changed.emit()
        self.changed.emit()


def qpalette(t):
    pal = QPalette()
    R = QPalette.ColorRole
    for role, key in ((R.Window, "bg"), (R.WindowText, "text"), (R.Base, "surface"), (R.AlternateBase, "surface2"),
                      (R.Text, "text"), (R.Button, "surface"), (R.ButtonText, "text"), (R.Highlight, "accent_soft"),
                      (R.HighlightedText, "text"), (R.ToolTipBase, "surface"), (R.ToolTipText, "text"),
                      (R.PlaceholderText, "muted"), (R.Link, "accent_text"), (R.Light, "surface"),
                      (R.Midlight, "surface2"), (R.Mid, "border"), (R.Dark, "border_strong"), (R.Shadow, "shadow"),
                      (R.BrightText, "text")):
        pal.setColor(role, QColor(t[key]))
    for role in (R.Text, R.WindowText, R.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["faint"]))
    return pal


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
FONTS = {"ui": "", "reading": "", "mono": "monospace"}


def load_fonts(app, size=10):
    """Register the bundled Inter and Newsreader and set Inter as the application font. Fall back to an installed
    Inter, then to the usual desktop fonts, if the files are missing."""
    for name in ("InterVariable.ttf", "InterVariable-Italic.ttf", "Newsreader.ttf", "Newsreader-Italic.ttf"):
        path = os.path.join(FONT_DIR, name)
        if os.path.exists(path):
            QFontDatabase.addApplicationFont(path)
    families = set(QFontDatabase.families())
    FONTS["ui"] = next((f for f in ("Inter Variable", "Inter", "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans")
                        if f in families), app.font().family())
    FONTS["reading"] = next((f for f in ("Newsreader", "Source Serif 4", "Noto Serif", "DejaVu Serif")
                             if f in families), FONTS["ui"])
    FONTS["mono"] = next((f for f in ("JetBrains Mono", "Fira Code", "Cascadia Code", "DejaVu Sans Mono")
                          if f in families), "monospace")
    font = QFont(FONTS["ui"], size)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(font)
    return FONTS


def tabular(font):
    """Same font with tabular figures (all digits the same width): clocks and columns of numbers don't jitter."""
    font = QFont(font)
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
    except (AttributeError, TypeError):
        pass  # Qt < 6.7: proportional figures
    return font


# ---------------------------------------------------------------------------
# Base style sheet
# ---------------------------------------------------------------------------
def base_qss(t):
    """Rules shared by every app: controls, menus, scrollbars, tooltips and visible focus rings."""
    r = RADIUS
    return f"""
* {{ color: {t['text']}; font-size: 13px; }}
QToolTip {{ background: {t['surface']}; color: {t['text']}; border: 1px solid {t['border']}; padding: 6px 8px;
    border-radius: {r['sm']}px; }}
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox {{
    background: {t['surface']}; border: 1px solid {t['border_strong']}; border-radius: {r['md']}px;
    padding: 6px 10px; selection-background-color: {t['accent_soft']}; selection-color: {t['text']}; }}
QLineEdit:hover, QComboBox:hover, QDateEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {t['faint']}; }}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border: 2px solid {t['focus']}; padding: 5px 9px; }}
QLineEdit:disabled, QComboBox:disabled {{ color: {t['faint']}; background: {t['surface2']}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {t['surface']}; border: 1px solid {t['border']}; outline: none;
    padding: 4px; selection-background-color: {t['accent_soft']}; selection-color: {t['text']}; }}
QPushButton {{ background: {t['surface']}; border: 1px solid {t['border_strong']}; border-radius: {r['md']}px;
    padding: 7px 14px; font-weight: 600; }}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:focus {{ border: 2px solid {t['focus']}; padding: 6px 13px; }}
QPushButton:disabled {{ color: {t['faint']}; border-color: {t['border']}; }}
QPushButton#primary {{ background: {t['accent']}; border: none; color: {t['accent_fg']}; padding: 8px 16px; }}
QPushButton#primary:hover {{ background: {t['accent_hover']}; }}
QPushButton#primary:focus {{ border: 2px solid {t['accent_fg']}; padding: 6px 14px; }}
QPushButton#primary:disabled {{ background: {t['accent_soft']}; color: {t['faint']}; }}
QPushButton#soft {{ background: {t['accent_soft']}; border: none; color: {t['accent_text']}; }}
QPushButton#soft:hover {{ background: {alpha(t['accent'], 0.22)}; }}
QPushButton#soft:disabled {{ background: {t['hover']}; color: {t['faint']}; }}
QPushButton#danger {{ background: {alpha(t['err'], 0.12)}; border: none; color: {t['err']}; padding: 8px 16px; }}
QPushButton#danger:hover {{ background: {alpha(t['err'], 0.2)}; }}
QPushButton#ghost {{ background: transparent; border: none; color: {t['muted']}; padding: 6px 10px; }}
QPushButton#ghost:hover {{ background: {t['hover']}; color: {t['text']}; }}
QPushButton#icon {{ background: transparent; border: none; padding: 4px; min-width: 24px; min-height: 26px;
    border-radius: {r['sm']}px; }}
QPushButton#icon:hover {{ background: {t['hover']}; }}
QPushButton#chip {{ border-radius: 14px; padding: 5px 13px; font-weight: 600; color: {t['muted']};
    border: 1px solid {t['border']}; background: transparent; }}
QPushButton#chip:hover {{ background: {t['hover']}; }}
QPushButton#chip:checked {{ color: {t['accent_text']}; border-color: {t['accent']}; background: {t['accent_soft']}; }}
QPushButton::menu-indicator {{ width: 0; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox:focus {{ color: {t['accent_text']}; }}
QMenuBar {{ background: {t['bg']}; padding: 3px 10px; border-bottom: 1px solid {t['border']}; }}
QMenuBar::item {{ padding: 5px 10px; border-radius: {r['sm']}px; background: transparent; }}
QMenuBar::item:selected {{ background: {t['hover']}; }}
QMenu {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: {r['lg']}px; padding: 6px; }}
QMenu::item {{ padding: 6px 28px 6px 12px; border-radius: {r['sm']}px; }}
QMenu::item:selected {{ background: {t['accent_soft']}; color: {t['text']}; }}
QMenu::item:disabled {{ color: {t['faint']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 5px 8px; }}
QProgressBar {{ background: {t['hover']}; border: none; border-radius: 3px; max-height: 6px; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 3px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{ background: {t['border_strong']}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t['faint']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle:horizontal {{ background: {t['border_strong']}; border-radius: 3px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
#muted {{ color: {t['muted']}; }}
#hint {{ color: {t['muted']}; font-size: 12px; }}
#palette {{ background: {t['surface']}; border: 1px solid {t['border']}; border-radius: {r['xl']}px; }}
QLineEdit#paletteSearch {{ background: transparent; border: none; border-bottom: 1px solid {t['border']};
    border-radius: 0; padding: 14px 16px; font-size: 15px; }}
QListWidget#paletteList {{ background: transparent; border: none; outline: none; }}
"""


# ---------------------------------------------------------------------------
# Icons: drawn with QPainter, sharp at every scale, independent of emoji fonts
# ---------------------------------------------------------------------------
def _draw_icon(p, name, u, color):
    """Draw `name` on a 20×20 grid scaled by `u`."""
    P = lambda x, y: QPointF(x * u, y * u)  # noqa: E731
    R = lambda x, y, w, h: QRectF(x * u, y * u, w * u, h * u)  # noqa: E731

    def fill(path_or_rect, radius=0.0):
        p.save()
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        if isinstance(path_or_rect, QPainterPath):
            p.drawPath(path_or_rect)
        else:
            p.drawRoundedRect(path_or_rect, radius * u, radius * u)
        p.restore()

    def poly(*pts, closed=False):
        path = QPainterPath(P(*pts[0]))
        for pt in pts[1:]:
            path.lineTo(P(*pt))
        if closed:
            path.closeSubpath()
        p.drawPath(path)

    if name == "menu":
        for y in (6, 10, 14):
            p.drawLine(P(4, y), P(16, y))
    elif name == "close":
        p.drawLine(P(5, 5), P(15, 15))
        p.drawLine(P(15, 5), P(5, 15))
    elif name == "dots":
        for x in (5, 10, 15):
            fill(R(x - 1.6, 8.4, 3.2, 3.2), 1.6)
    elif name == "settings":  # two sliders
        for y, x in ((7, 8), (13, 13)):
            p.drawLine(P(3.5, y), P(16.5, y))
            p.save()
            p.setBrush(QColor(T.get("surface", "#ffffff")))
            p.drawEllipse(P(x, y), 2.4 * u, 2.4 * u)
            p.restore()
    elif name == "search":
        p.drawEllipse(P(8.5, 8.5), 5 * u, 5 * u)
        p.drawLine(P(12.2, 12.2), P(16.5, 16.5))
    elif name == "plus":
        p.drawLine(P(10, 4), P(10, 16))
        p.drawLine(P(4, 10), P(16, 10))
    elif name == "server":
        for y in (3.5, 11):
            p.drawRoundedRect(R(3, y, 14, 5.5), 1.6 * u, 1.6 * u)
            fill(R(5.2, y + 2, 1.6, 1.6), 0.8)
    elif name == "explore":  # compass
        p.drawEllipse(P(10, 10), 7 * u, 7 * u)
        poly((12.8, 7.2), (11, 11), (7.2, 12.8), (9, 9), closed=True)
    elif name == "library":  # books on a shelf
        p.drawRoundedRect(R(3.5, 4, 3.5, 12), 0.8 * u, 0.8 * u)
        p.drawRoundedRect(R(8.5, 4, 3.5, 12), 0.8 * u, 0.8 * u)
        poly((13.4, 5), (16.6, 4.2), (17.6, 15.2), (14.4, 16), closed=True)
    elif name == "converter":  # two arrows
        poly((4, 7), (15, 7))
        poly((12, 4), (15, 7), (12, 10))
        poly((16, 13), (5, 13))
        poly((8, 10), (5, 13), (8, 16))
    elif name == "refresh":
        path = QPainterPath()
        path.arcMoveTo(R(4, 4, 12, 12), 60)
        path.arcTo(R(4, 4, 12, 12), 60, 280)
        p.drawPath(path)
        poly((13.2, 2.8), (13.2, 5.3), (15.6, 5.6))
    elif name == "download":
        p.drawLine(P(10, 3.5), P(10, 12.5))
        poly((6.5, 9), (10, 12.5), (13.5, 9))
        poly((4, 13.5), (4, 16.5), (16, 16.5), (16, 13.5))
    elif name == "external":
        poly((11, 4), (16, 4), (16, 9))
        p.drawLine(P(16, 4), P(9.5, 10.5))
        poly((14, 12), (14, 16), (4, 16), (4, 6), (8, 6))
    elif name == "folder":
        poly((3, 6), (3, 15.5), (17, 15.5), (17, 7.5), (10, 7.5), (8.5, 5), (3, 5), (3, 6), closed=True)
    elif name == "delete":
        p.drawLine(P(4, 6), P(16, 6))
        poly((8, 6), (8, 4), (12, 4), (12, 6))
        poly((5.5, 6), (6.5, 16.5), (13.5, 16.5), (14.5, 6))
    elif name == "play":
        path = QPainterPath(P(6.5, 4.5))
        path.lineTo(P(15.5, 10))
        path.lineTo(P(6.5, 15.5))
        path.closeSubpath()
        fill(path)
    elif name == "stop":
        fill(R(5.5, 5.5, 9, 9), 1.8)
    elif name == "record":
        fill(R(5, 5, 10, 10), 5)
    elif name == "mic":
        p.drawRoundedRect(R(7.5, 2.8, 5, 9), 2.5 * u, 2.5 * u)
        path = QPainterPath()
        path.arcMoveTo(R(4.8, 4.2, 10.4, 10.4), 180)
        path.arcTo(R(4.8, 4.2, 10.4, 10.4), 180, 180)
        p.drawPath(path)
        p.drawLine(P(10, 14.6), P(10, 17))
    elif name == "file":
        poly((5, 3), (12, 3), (15.5, 6.5), (15.5, 17), (5, 17), closed=True)
        poly((12, 3), (12, 6.5), (15.5, 6.5))
    elif name == "lock":
        p.drawRoundedRect(R(4.5, 9, 11, 8), 1.8 * u, 1.8 * u)
        path = QPainterPath()
        path.moveTo(P(7, 9))
        path.lineTo(P(7, 6.5))
        path.arcTo(R(7, 3.5, 6, 6), 180, -180)
        path.lineTo(P(13, 9))
        p.drawPath(path)
    elif name in ("chip", "cpu"):
        p.drawRoundedRect(R(5, 5, 10, 10), 1.6 * u, 1.6 * u)
        p.drawRoundedRect(R(8, 8, 4, 4), 0.6 * u, 0.6 * u)
        for k in (8, 12):
            p.drawLine(P(k, 2.5), P(k, 5))
            p.drawLine(P(k, 15), P(k, 17.5))
            p.drawLine(P(2.5, k), P(5, k))
            p.drawLine(P(15, k), P(17.5, k))
    elif name == "gpu":
        p.drawRoundedRect(R(2.5, 5.5, 15, 9), 1.6 * u, 1.6 * u)
        p.drawEllipse(P(8, 10), 2.4 * u, 2.4 * u)
        p.drawLine(P(12.5, 8.5), P(15, 8.5))
        p.drawLine(P(12.5, 11.5), P(15, 11.5))
        p.drawLine(P(5, 14.5), P(5, 16.5))
    elif name == "spark":  # NPU / AI accelerator
        path = QPainterPath(P(10, 2.5))
        for x, y in ((11.8, 8.2), (17.5, 10), (11.8, 11.8), (10, 17.5), (8.2, 11.8), (2.5, 10), (8.2, 8.2)):
            path.lineTo(P(x, y))
        path.closeSubpath()
        p.drawPath(path)
    elif name == "layers":
        poly((10, 3.5), (17, 7.5), (10, 11.5), (3, 7.5), closed=True)
        poly((3, 11), (10, 15), (17, 11))
    elif name == "globe":
        p.drawEllipse(P(10, 10), 7 * u, 7 * u)
        p.drawEllipse(P(10, 10), 3 * u, 7 * u)
        p.drawLine(P(3, 10), P(17, 10))
    elif name == "theme":  # half moon in a circle
        p.drawEllipse(P(10, 10), 7 * u, 7 * u)
        path = QPainterPath(P(10, 3))
        path.arcTo(R(3, 3, 14, 14), 90, 180)
        path.closeSubpath()
        fill(path)
    elif name == "copy":
        p.drawRoundedRect(R(7, 7, 10, 10), 1.8 * u, 1.8 * u)
        poly((13, 4.8), (13, 3), (3, 3), (3, 13), (4.8, 13))
    elif name == "flask":
        poly((8, 3), (8, 8), (4, 16), (16, 16), (12, 8), (12, 3))
        p.drawLine(P(7, 3), P(13, 3))
        p.drawLine(P(6, 12), P(14, 12))
    elif name == "log":
        p.drawRoundedRect(R(4, 3, 12, 14), 1.6 * u, 1.6 * u)
        for y in (7, 10, 13):
            p.drawLine(P(7, y), P(13, y))
    elif name == "info":
        p.drawEllipse(P(10, 10), 7 * u, 7 * u)
        p.drawLine(P(10, 9), P(10, 14))
        fill(R(9.2, 5.6, 1.6, 1.6), 0.8)
    elif name == "chevron_up":
        poly((5.5, 12.5), (10, 8), (14.5, 12.5))
    elif name == "chevron_down":
        poly((5.5, 8), (10, 12.5), (14.5, 8))
    elif name == "check":
        poly((4.5, 10.5), (8.3, 14.2), (15.5, 6))
    elif name == "warning":
        poly((10, 3.5), (17.5, 16.5), (2.5, 16.5), closed=True)
        p.drawLine(P(10, 8), P(10, 11.5))
        fill(R(9.2, 13.2, 1.6, 1.6), 0.8)
    elif name == "memory":
        p.drawRoundedRect(R(2.5, 6, 15, 8), 1.4 * u, 1.4 * u)
        for x in (6, 10, 14):
            p.drawRect(R(x - 1, 8.5, 2, 3))
        for x in (5, 8, 11, 14):
            p.drawLine(P(x, 14), P(x, 16))
    elif name == "notebook":
        p.drawRoundedRect(R(4.5, 3, 11.5, 14), 1.8 * u, 1.8 * u)
        p.drawLine(P(7.5, 3), P(7.5, 17))
        p.drawLine(P(10, 7), P(13.5, 7))
    elif name == "text_size":
        f = QFont(FONTS["ui"] or "Sans")
        f.setPixelSize(int(11 * u))
        f.setBold(True)
        p.setFont(f)
        p.drawText(R(0, 0, 20, 20), Qt.AlignCenter, "Aa")
    else:  # unknown name: a small dot, visible enough to notice in development
        fill(R(8, 8, 4, 4), 2)


def icon(name, color=None, size=20):
    """Stroke icon `name` in `color` (default: the muted text color of the current theme)."""
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    color = QColor(color or T.get("muted", "#777777"))
    pen = QPen(color, 1.7 * size / 20)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    _draw_icon(p, name, size / 20, color)
    p.end()
    return QIcon(pm)


def dot_icon(color, size=10):
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(size / 2, size / 2, size, size))
    p.end()
    return QIcon(pm)


class Themed:
    """Mixin: call self.restyle() now and every time the theme changes."""

    def follow_theme(self):
        signals.changed.connect(self.restyle)
        self.restyle()

    def restyle(self):
        pass


class IconButton(QPushButton, Themed):
    """Push button with a drawn icon (and optional text) that is redrawn in the theme's colors.
    `color_key` picks the token used for the icon ("muted", "text", "accent_fg", "accent_text"…)."""

    def __init__(self, name, text="", kind=None, slot=None, tooltip="", color_key=None, size=18):
        super().__init__(text)
        self.icon_name, self.color_key, self.icon_px = name, color_key, size
        self.setObjectName(kind or ("icon" if not text else ""))
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(size, size))
        if tooltip:
            self.setToolTip(tooltip)
            self.setAccessibleName(tooltip)
        if slot:
            self.clicked.connect(slot)
        self.follow_theme()

    def set_icon_name(self, name, color_key=None):
        self.icon_name = name
        self.color_key = color_key or self.color_key
        self.restyle()

    def restyle(self):
        key = self.color_key or {"primary": "accent_fg", "soft": "accent_text", "danger": "err"}.get(
            self.objectName(), "text" if self.text() else "muted")
        self.setIcon(icon(self.icon_name, T.get(key), self.icon_px) if self.icon_name else QIcon())


class Chip(QWidget, Themed):
    """Small pill with an icon and a label: provenance, privacy, status. kind: neutral | accent | ok | warn | err."""

    def __init__(self, icon_name="", text="", kind="neutral", tooltip=""):
        super().__init__()
        self.icon_name, self.kind = icon_name, kind
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 10, 3)
        lay.setSpacing(5)
        self.pic = QLabel()
        self.label = QLabel(text)
        lay.addWidget(self.pic)
        lay.addWidget(self.label)
        if tooltip:
            self.setToolTip(tooltip)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)  # hugs its content
        self.follow_theme()

    def set(self, text=None, icon_name=None, kind=None, tooltip=None):
        if text is not None:
            self.label.setText(text)
        if icon_name is not None:
            self.icon_name = icon_name
        if kind is not None:
            self.kind = kind
        if tooltip is not None:
            self.setToolTip(tooltip)
        self.restyle()

    def restyle(self):
        fg = {"accent": T.get("accent_text"), "ok": T.get("ok"), "warn": T.get("warn"),
              "err": T.get("err")}.get(self.kind, T.get("muted"))
        bg = T.get("accent_soft") if self.kind == "accent" else alpha(fg, 0.10) if self.kind != "neutral" \
            else T.get("hover")
        self.setStyleSheet(f"Chip {{ background: {bg}; border-radius: 11px; }} "
                           f"QLabel {{ color: {fg}; font-size: 12px; font-weight: 600; background: transparent; }}")
        self.pic.setVisible(bool(self.icon_name))
        if self.icon_name:
            self.pic.setPixmap(icon(self.icon_name, fg, 14).pixmap(14, 14))


def elevate(widget, blur=30, dy=8):
    """Soft shadow for floating elements only (dock, palette, popovers); follows the theme."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)

    def update():
        effect.setColor(QColor(alpha(T.get("shadow", "#000000"), T.get("shadow_alpha", 0.15))))
    signals.changed.connect(update)
    update()
    widget.setGraphicsEffect(effect)
    return effect


# ---------------------------------------------------------------------------
# Command palette (Ctrl+K)
# ---------------------------------------------------------------------------
@dataclass
class Command:
    title: str
    run: object                 # callable without arguments
    section: str = ""
    hint: str = ""              # e.g. the keyboard shortcut, shown on the right
    icon: str = ""
    keywords: str = ""          # extra words that match the search but are not shown
    score: float = field(default=0.0, compare=False)


def fuzzy_score(query, text):
    """Score of `query` as a subsequence of `text` (higher is better), None if it does not match.
    Consecutive letters and letters at the start of words count more."""
    if not query:
        return 0.0
    q, t = query.lower(), text.lower()
    pos, score, last = 0, 0.0, -2
    for ch in q:
        if ch == " ":
            continue
        idx = t.find(ch, pos)
        if idx < 0:
            return None
        score += 1.0
        if idx == last + 1:
            score += 1.5
        if idx == 0 or not t[idx - 1].isalnum():
            score += 2.0
        last, pos = idx, idx + 1
    return score - 0.01 * len(t)


ROLE_COMMAND = Qt.UserRole + 20


class _PaletteDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(option.rect.width(), 40)

    def paint(self, p, option, index):
        cmd = index.data(ROLE_COMMAND)
        r = QRectF(option.rect).adjusted(6, 2, -6, -2)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if option.state & QStyle.State_Selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(T["accent_soft"]))
            p.drawRoundedRect(r, RADIUS["md"], RADIUS["md"])
        x = r.left() + 10
        if cmd.icon:
            icon(cmd.icon, T["accent_text"] if option.state & QStyle.State_Selected else T["muted"], 18).paint(
                p, int(x), int(r.center().y() - 9), 18, 18)
            x += 28
        font = QFont(option.font)
        p.setFont(font)
        hint_w = p.fontMetrics().horizontalAdvance(cmd.hint) + 16 if cmd.hint else 0
        sec_font = QFont(font)
        sec_font.setPointSizeF(max(7.5, font.pointSizeF() - 1))
        p.setFont(sec_font)
        sec_w = p.fontMetrics().horizontalAdvance(cmd.section) + 16 if cmd.section else 0
        p.setFont(font)
        p.setPen(QColor(T["text"]))
        title_r = QRectF(x, r.top(), r.right() - x - hint_w - sec_w, r.height())
        p.drawText(title_r, Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(cmd.title, Qt.ElideRight, int(title_r.width())))
        p.setPen(QColor(T["muted"]))
        if cmd.hint:
            p.drawText(r.adjusted(0, 0, -10, 0), Qt.AlignVCenter | Qt.AlignRight, cmd.hint)
        if cmd.section:
            p.setFont(sec_font)
            p.drawText(r.adjusted(0, 0, -10 - hint_w, 0), Qt.AlignVCenter | Qt.AlignRight, cmd.section)
        p.restore()


class CommandPalette(QDialog):
    """Search-as-you-type launcher for every action of the app. `provider(query)` returns the Command list;
    it is called at every keystroke, so it can add dynamic entries (e.g. "Search the catalog: <query>")."""

    def __init__(self, parent, provider, placeholder, empty_text):
        super().__init__(parent, Qt.FramelessWindowHint | Qt.Popup)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.provider, self.empty_text = provider, empty_text
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 16, 24, 32)  # room for the shadow
        frame = QFrame()
        frame.setObjectName("palette")
        elevate(frame, 40, 12)
        outer.addWidget(frame)
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 8)
        lay.setSpacing(4)
        self.search = QLineEdit()
        self.search.setObjectName("paletteSearch")
        self.search.setPlaceholderText(placeholder)
        self.search.textChanged.connect(self.refresh)
        self.search.installEventFilter(self)
        lay.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("paletteList")
        self.list.setItemDelegate(_PaletteDelegate(self.list))
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.itemActivated.connect(self._run_item)
        self.list.itemClicked.connect(self._run_item)
        lay.addWidget(self.list)
        self.empty = QLabel(empty_text)
        self.empty.setObjectName("hint")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setMinimumHeight(60)
        lay.addWidget(self.empty)

    def open_palette(self):
        parent = self.parentWidget()
        width = min(640, max(360, parent.width() - 80))
        self.resize(width + 48, 460)
        top_left = parent.mapToGlobal(parent.rect().topLeft())
        self.move(top_left.x() + (parent.width() - self.width()) // 2, top_left.y() + max(40, parent.height() // 8))
        self.search.clear()
        self.refresh("")
        self.show()
        self.activateWindow()
        self.search.setFocus()

    def refresh(self, query):
        query = query.strip()
        scored = []
        for cmd in self.provider(query):
            score = fuzzy_score(query, f"{cmd.title} {cmd.section} {cmd.keywords}")
            if score is not None or cmd.score > 0:
                cmd.score = max(cmd.score, score or 0)
                scored.append(cmd)
        if query:
            scored.sort(key=lambda c: -c.score)
        self.list.clear()
        for cmd in scored[:60]:
            item = QListWidgetItem(cmd.title)
            item.setData(ROLE_COMMAND, cmd)
            self.list.addItem(item)
        self.list.setVisible(bool(scored))
        self.empty.setVisible(not scored)
        if scored:
            self.list.setCurrentRow(0)

    def eventFilter(self, obj, event):
        if obj is self.search and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Up):
                row = self.list.currentRow() + (1 if key == Qt.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                self._run_item(self.list.currentItem())
                return True
            if key == Qt.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)

    def _run_item(self, item):
        if not item:
            return
        cmd = item.data(ROLE_COMMAND)
        self.close()
        cmd.run()


# ---------------------------------------------------------------------------
# Update notifications (GitHub releases)
# ---------------------------------------------------------------------------
PROJECT_REPO = "VarCode-ai/ai-app-local-openvino"
RELEASES_API = f"https://api.github.com/repos/{PROJECT_REPO}/releases/latest"


def parse_version(text):
    """'v1.2.3' / '1.2' -> (1, 2, 3) / (1, 2, 0); None if it is not a version number."""
    parts = (text or "").strip().lstrip("vV").split("-")[0].split(".")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    return tuple((numbers + [0, 0, 0])[:3]) if numbers else None


@dataclass
class Release:
    version: str
    name: str
    url: str


class UpdateChecker(QObject):
    """Checks the latest GitHub release at most once every `interval` seconds, in a background thread, and emits
    `found(Release)` if it is newer than `current` and not skipped. Network errors are ignored: the check
    must never bother someone working offline. `get(key, default)` / `put(key, value)` store its state in the
    app's settings; `enabled` turns it off."""
    found = Signal(object)
    finished = Signal(object)  # Release, or None if up to date / unreachable (for "check now")

    def __init__(self, parent, current, get, put, interval=24 * 3600, api_url=RELEASES_API):
        super().__init__(parent)
        self.current, self.get, self.put = current, get, put
        self.interval, self.api_url = interval, api_url

    @property
    def enabled(self):
        return str(self.get("update_check", "true")).lower() != "false"

    def set_enabled(self, on):
        self.put("update_check", "true" if on else "false")

    def skip(self, release):
        self.put("update_skip", release.version)

    def check(self, force=False):
        """Background check; with force=True also when disabled, recently checked or the version is skipped."""
        if not force and not self.enabled:
            return
        import threading
        import time
        try:
            last = float(self.get("update_last_check", 0) or 0)
        except (TypeError, ValueError):
            last = 0
        if not force and time.time() - last < self.interval:
            self._emit(self.get("update_latest", "") or "", "", "", force)  # what the last check found
            return
        threading.Thread(target=self._fetch, args=(force,), daemon=True).start()

    def _fetch(self, force):
        import json
        import time
        import urllib.request
        try:
            req = urllib.request.Request(self.api_url, headers={"Accept": "application/vnd.github+json",
                                                                "User-Agent": "ai-app-local-openvino"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
        except Exception:  # noqa: BLE001 - offline, rate limited, no release yet: silently nothing
            self.finished.emit(None)
            return
        self.put("update_last_check", str(time.time()))
        self.put("update_latest", data.get("tag_name", ""))
        self.put("update_latest_url", data.get("html_url", ""))
        self.put("update_latest_name", data.get("name", ""))
        self._emit(data.get("tag_name", ""), data.get("name", ""), data.get("html_url", ""), force)

    def _emit(self, tag, name, url, force):
        latest, current = parse_version(tag), parse_version(self.current)
        newer = bool(latest and current and latest > current)
        version = ".".join(map(str, latest)) if latest else ""
        skipped = version and version == self.get("update_skip", "")
        if not newer or (skipped and not force):
            self.finished.emit(None)
            return
        release = Release(version, name or self.get("update_latest_name", "") or tag,
                          url or self.get("update_latest_url", "") or f"https://github.com/{PROJECT_REPO}/releases")
        self.found.emit(release)
        self.finished.emit(release)


class UpdateBanner(QFrame, Themed):
    """Slim, dismissible banner announcing a new release. Texts come from the app (already translated):
    `message` with {version}, and the labels of the two buttons."""

    def __init__(self, message, whats_new, skip, close_tip, on_skip):
        super().__init__()
        self.setObjectName("updateBanner")
        self.message, self.release, self.on_skip = message, None, on_skip
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 6, 6, 6)
        lay.setSpacing(8)
        self.pic = QLabel()
        lay.addWidget(self.pic)
        self.label = QLabel()
        self.label.setObjectName("updateText")
        lay.addWidget(self.label, 1)
        self.btn_open = IconButton("external", whats_new, "soft", self._open, size=16)
        self.btn_skip = QPushButton(skip)
        self.btn_skip.setObjectName("ghost")
        self.btn_skip.setCursor(Qt.PointingHandCursor)
        self.btn_skip.clicked.connect(self._skip)
        lay.addWidget(self.btn_open)
        lay.addWidget(self.btn_skip)
        lay.addWidget(IconButton("close", tooltip=close_tip, slot=self.hide, size=16))
        self.hide()
        self.follow_theme()

    def show_release(self, release):
        self.release = release
        self.label.setText(self.message.format(version=release.version))
        self.show()

    def _open(self):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        if self.release:
            QDesktopServices.openUrl(QUrl(self.release.url))

    def _skip(self):
        if self.release:
            self.on_skip(self.release)
        self.hide()

    def restyle(self):
        self.setStyleSheet(f"#updateBanner {{ background: {T.get('accent_soft')}; border-radius: {RADIUS['lg']}px; }} "
                           f"#updateText {{ color: {T.get('text')}; font-weight: 600; }}")
        self.pic.setPixmap(icon("download", T.get("accent_text"), 18).pixmap(18, 18))
