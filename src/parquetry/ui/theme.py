"""Colours and appearance settings of the plots.

The plots follow the application's light or dark palette unless the user picks
one. The categorical series colours are a colour-vision-deficiency checked
palette with separate steps for light and dark surfaces. Colours are assigned
to parameters in a fixed order and stay with a parameter while it is shown,
unless the user gives a parameter a colour of its own (see :class:`PlotStyle`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields, replace

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class PlotTheme:
    dark: bool
    surface: str
    text: str
    text_muted: str
    axis: str
    series: tuple[str, ...]
    region: str
    selection: str

    def series_color(self, slot: int) -> QColor:
        return QColor(self.series[slot % len(self.series)])


LIGHT = PlotTheme(
    dark=False,
    surface="#fcfcfb",
    text="#0b0b0b",
    text_muted="#52514e",
    axis="#8a8983",
    series=("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"),
    region="#2a78d6",
    selection="#eb6834",
)

DARK = PlotTheme(
    dark=True,
    surface="#1a1a19",
    text="#ffffff",
    text_muted="#c3c2b7",
    axis="#8f8e86",
    series=("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"),
    region="#3987e5",
    selection="#d95926",
)


#: Plot colour schemes, with their labels.
THEMES = {"auto": "Follow the application", "light": "Light", "dark": "Dark"}


def current_theme() -> PlotTheme:
    app = QApplication.instance()
    if app is None:
        return LIGHT
    window = app.palette().color(QPalette.ColorRole.Window)
    return DARK if window.lightness() < 128 else LIGHT


def theme_named(name: str) -> PlotTheme:
    """The theme for a key of :data:`THEMES`."""
    return {"light": LIGHT, "dark": DARK}.get(name) or current_theme()


def parse_color(text: str | None) -> QColor | None:
    """The colour named by *text* (``#rrggbb`` or a colour name), or ``None``."""
    if not text:
        return None
    color = QColor(text)
    return color if color.isValid() else None


@dataclass
class PlotStyle:
    """How the plots look, as chosen in the plot settings."""

    theme: str = "auto"
    line_width: float = 1.0
    point_size: int = 5
    #: Opacity of the grid lines in percent; 0 hides the grid.
    grid: int = 12
    #: Show a legend in plots with several series.
    legend: bool = True
    #: Highlight colour of ranges without a colour of their own (empty: the theme's).
    range_color: str = ""
    #: Opacity of the range highlights in percent.
    range_opacity: int = 15
    #: Line colours by parameter (``#rrggbb``); other parameters use the palette.
    colors: dict[str, str] = field(default_factory=dict)
    #: Axis and legend labels by column name, used instead of the name.
    labels: dict[str, str] = field(default_factory=dict)
    #: Text above the plots. Not kept between sessions, like the next one.
    title: str = ""
    #: Y axis label of the overlay plot (all numeric parameters in one plot).
    overlay_label: str = ""

    def copy(self) -> PlotStyle:
        return replace(self, colors=dict(self.colors), labels=dict(self.labels))


#: Settings kept between sessions; the title and overlay label belong to one figure.
_SAVED = ("theme", "line_width", "point_size", "grid", "legend", "range_color", "range_opacity", "colors", "labels")
_STYLE_KEY = "plot/style"


def load_style(settings: QSettings) -> PlotStyle:
    """The plot style saved in *settings*; invalid entries keep their defaults."""
    style = PlotStyle()
    try:
        data = json.loads(settings.value(_STYLE_KEY, "") or "{}")
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        return style
    types = {f.name: type(getattr(style, f.name)) for f in fields(style)}
    for key in _SAVED:
        value = data.get(key)
        expected = types[key]
        if expected is float and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            continue
        if expected is dict:
            value = {str(k): str(v) for k, v in value.items() if isinstance(v, str) and v}
        setattr(style, key, value)
    if style.theme not in THEMES:
        style.theme = "auto"
    return style


def save_style(settings: QSettings, style: PlotStyle) -> None:
    settings.setValue(_STYLE_KEY, json.dumps({key: getattr(style, key) for key in _SAVED}))


def with_alpha(color: str | QColor, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c
