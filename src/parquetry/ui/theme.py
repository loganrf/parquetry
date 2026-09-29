"""Colours for plots, following the application's light or dark palette.

The categorical series colours are a colour-vision-deficiency checked
palette with separate steps for light and dark surfaces. Colours are assigned
to parameters in a fixed order and stay with a parameter while it is shown.
"""

from __future__ import annotations

from dataclasses import dataclass

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


def current_theme() -> PlotTheme:
    app = QApplication.instance()
    if app is None:
        return LIGHT
    window = app.palette().color(QPalette.ColorRole.Window)
    return DARK if window.lightness() < 128 else LIGHT


def with_alpha(color: str | QColor, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c
