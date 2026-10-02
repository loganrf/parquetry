"""Interactive plot: stacked or overlaid series, crosshair readout and x ranges.

Text is drawn by position: a text x axis shows one bar or marker column per
category, and text values are step lines on an axis labelled with the
category names. The plots can be saved as PNG, JPEG, SVG or PDF images.
"""

from __future__ import annotations

import datetime as dt
import html
import math
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from pyqtgraph.exporters import ImageExporter
from PySide6.QtCore import QMarginsF, QPointF, QRect, QRectF, QSize, QSizeF, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QGuiApplication,
    QImage,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QPicture,
)
from PySide6.QtWidgets import QColorDialog, QGraphicsItem, QLabel, QMenu, QVBoxLayout, QWidget

from ..dataset import ROW_INDEX, ROW_INDEX_LABEL
from ..processing import PlotData, PlotSeries
from .theme import PlotStyle, PlotTheme, current_theme, parse_color, theme_named, with_alpha

_MINMAX = {"min", "max"}
_LINE_STYLES = (Qt.PenStyle.SolidLine, Qt.PenStyle.DashLine, Qt.PenStyle.DotLine, Qt.PenStyle.DashDotLine)
_BAR_ALPHA = (220, 150, 95, 60)  # functions of one parameter, side by side
AXIS_WIDTH = 72
#: Image formats by file suffix: raster formats are saved by Qt, SVG and PDF are vector graphics.
IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".svg": "SVG", ".pdf": "PDF"}
#: Formats that can have a transparent background.
TRANSPARENT_FORMATS = {".png", ".svg", ".pdf"}
OVERLAY_TITLE = "All parameters (overlay)"


def format_plot_x(value: float, kind: str, precise: bool = True) -> str:
    if kind == "datetime":
        try:
            stamp = dt.datetime(1970, 1, 1) + dt.timedelta(microseconds=round(value * 1e6))
        except (OverflowError, ValueError):
            return f"{value:g}"
        text = stamp.isoformat(sep=" ", timespec="microseconds" if precise else "seconds")
        return text[:-3] if precise else text
    return f"{value:.10g}"


def format_value(value: float) -> str:
    if value is None or not np.isfinite(value):
        return "–"
    if value != 0 and (abs(value) >= 1e7 or abs(value) < 1e-3):
        return f"{value:.4e}"
    return f"{value:.6g}"


def category_name(categories: list[str], position: float | None) -> str:
    """The category at a plot position, or a dash outside the categories."""
    if position is None or not np.isfinite(position):
        return "–"
    index = round(position)
    return categories[index] if 0 <= index < len(categories) else "–"


def shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class CategoryAxis(pg.AxisItem):
    """Axis labelled with category names at the integer positions 0, 1, 2, ..."""

    def __init__(self, orientation: str, categories: list[str]) -> None:
        super().__init__(orientation=orientation)
        self.enableAutoSIPrefix(False)
        self.categories = categories
        vertical = orientation in ("left", "right")
        self._chars = 10 if vertical else 18
        longest = min(max((len(c) for c in categories), default=1), self._chars)
        # Room along the axis for one label: a line of text, or the label's width.
        self._label_px = 16 if vertical else 7 * longest + 14

    def tickValues(self, minVal, maxVal, size):  # noqa: N802 - pyqtgraph naming
        lo = max(0, math.ceil(min(minVal, maxVal)))
        hi = min(len(self.categories) - 1, math.floor(max(minVal, maxVal)))
        if hi < lo or size <= 0:
            return []
        step = max(1, math.ceil((hi - lo + 1) * self._label_px / size))
        first = math.ceil(lo / step) * step
        return [(float(step), [float(v) for v in range(first, hi + 1, step)])]

    def tickStrings(self, values, scale, spacing):  # noqa: N802 - pyqtgraph naming
        return [shorten(category_name(self.categories, v), self._chars) for v in values]


@dataclass
class RangeEntry:
    lo: float
    hi: float
    label: str = ""
    #: Highlight colour; empty for the default range colour of the plot style.
    color: str = ""
    regions: list[pg.LinearRegionItem] = field(default_factory=list)
    tag: pg.InfLineLabel | None = None


class RangeRegion(pg.LinearRegionItem):
    """A draggable x range with a right-click menu."""

    sigRemoveRequested = Signal(object)
    sigZoomRequested = Signal(object)
    sigColorRequested = Signal(object)

    def mouseClickEvent(self, ev):  # noqa: N802 - Qt naming
        if ev.button() == Qt.MouseButton.RightButton and not self.moving:
            ev.accept()
            menu = QMenu()
            zoom = menu.addAction("Zoom to range")
            color = menu.addAction("Set colour…")
            remove = menu.addAction("Remove range")
            chosen = menu.exec(ev.screenPos().toPoint())
            if chosen is remove:
                self.sigRemoveRequested.emit(self)
            elif chosen is zoom:
                self.sigZoomRequested.emit(self)
            elif chosen is color:
                self.sigColorRequested.emit(self)
            return
        super().mouseClickEvent(ev)


class SelectViewBox(pg.ViewBox):
    """ViewBox where left-drag selects an x range in range mode or with Shift."""

    def __init__(self, area: PlotArea) -> None:
        super().__init__()
        self._area = area

    def mouseDragEvent(self, ev, axis=None):  # noqa: N802 - Qt naming
        selecting = self._area.allow_ranges and (
            self._area.select_mode or bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        )
        if axis is None and ev.button() == Qt.MouseButton.LeftButton and selecting:
            ev.accept()
            x0 = self.mapSceneToView(ev.buttonDownScenePos()).x()
            x1 = self.mapSceneToView(ev.scenePos()).x()
            self._area._drag_select(min(x0, x1), max(x0, x1), ev.isFinish())
            return
        super().mouseDragEvent(ev, axis)


class PlotArea(QWidget):
    """Plots of :class:`PlotData` with interactive range selection."""

    rangesChanged = Signal()
    viewChanged = Signal(float, float)

    def __init__(self, parent: QWidget | None = None, style: PlotStyle | None = None) -> None:
        super().__init__(parent)
        self.style = style.copy() if style else PlotStyle()
        self.theme: PlotTheme = theme_named(self.style.theme)
        self.glw = pg.GraphicsLayoutWidget()
        self.glw.setBackground(self.theme.surface)
        self.glw.ci.setSpacing(4)
        self.readout = QLabel()
        self.readout.setTextFormat(Qt.TextFormat.RichText)
        self.readout.setWordWrap(True)
        self.readout.setMinimumHeight(22)
        self.readout.setContentsMargins(6, 2, 6, 2)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.glw, 1)
        layout.addWidget(self.readout)

        self.data: PlotData | None = None
        self.stacked = True
        self.show_points = False
        self.auto_y = True
        self.select_mode = False
        #: Whether ranges can be selected (not on a text x axis).
        self.allow_ranges = True
        self._plots: list[pg.PlotItem] = []
        #: The parameter each plot shows (None for the overlay of all numeric parameters).
        self._keys: list[str | None] = []
        #: Fixed y ranges, by plot key; other plots follow the automatic y scaling.
        self._y_limits: dict[str | None, tuple[float, float]] = {}
        self._message: str | None = None
        self._exporting = False
        self._vlines: list[pg.InfiniteLine] = []
        self._previews: list[pg.LinearRegionItem] = []
        self._ranges: list[RangeEntry] = []
        self._slots: dict[str, int] = {}
        self._syncing = False
        self._placeholder: pg.TextItem | None = None
        self._mouse_proxy = pg.SignalProxy(self.glw.scene().sigMouseMoved, rateLimit=40, slot=self._on_mouse_moved)
        self._show_hint()
        self._show_placeholder("Select one or more parameters to plot")

    # ------------------------------------------------------------------ data
    @property
    def kind(self) -> str:
        return self.data.x_kind if self.data else "numeric"

    def set_data(self, data: PlotData | None, keep_view: bool = True, message: str | None = None) -> None:
        view = self.view_range() if keep_view and self._plots else None
        if self.data is not None and data is not None and self.data.x_categories != data.x_categories:
            view = None  # positions stand for other categories now
        self.data = data
        self._message = message
        self._assign_colors()
        self._rebuild(message)
        if view is not None and self._plots:
            self.set_x_view(*view, padding=0)
        elif self._category_span():
            self.set_x_view(*self._category_span(), padding=0.01)

    def _category_span(self) -> tuple[float, float] | None:
        """Every category with room for its bars, which automatic ranging would cut in half."""
        if self.kind != "category" or not self.data.x_categories or not self._plots:
            return None
        return -0.5, len(self.data.x_categories) - 0.5

    def clear(self) -> None:
        self._ranges.clear()
        self._slots.clear()
        self.set_data(None, keep_view=False)
        self.rangesChanged.emit()

    def set_stacked(self, stacked: bool) -> None:
        if stacked != self.stacked:
            self.stacked = stacked
            self.set_data(self.data)

    def set_show_points(self, enabled: bool) -> None:
        if enabled != self.show_points:
            self.show_points = enabled
            self.set_data(self.data)

    def set_auto_y(self, enabled: bool) -> None:
        self.auto_y = enabled
        for plot, key in zip(self._plots, self._keys):
            self._apply_y_mode(plot, key)

    def set_select_mode(self, enabled: bool) -> None:
        self.select_mode = enabled
        self.glw.setCursor(Qt.CursorShape.CrossCursor if enabled and self.allow_ranges else Qt.CursorShape.ArrowCursor)

    def set_allow_ranges(self, allowed: bool) -> None:
        self.allow_ranges = allowed
        self.set_select_mode(self.select_mode)
        self._show_hint()

    def set_style(self, style: PlotStyle) -> None:
        """Apply new appearance settings; the view stays where it is."""
        self.style = style.copy()
        self.theme = theme_named(style.theme)
        self.glw.setBackground(self.theme.surface)
        self.set_data(self.data, message=self._message)

    def color_for(self, column: str) -> QColor:
        return parse_color(self.style.colors.get(column)) or self.auto_color_for(column)

    def auto_color_for(self, column: str, theme: PlotTheme | None = None) -> QColor:
        """The palette colour of *column*, used when it has no colour of its own."""
        return (theme or self.theme).series_color(self._slots.get(column, 0))

    def label_for(self, column: str) -> str:
        """Axis and legend text of *column*: its label from the plot style, or its name."""
        return self.style.labels.get(column) or (ROW_INDEX_LABEL if column == ROW_INDEX else column)

    def has_plots(self) -> bool:
        return bool(self._plots)

    def _assign_colors(self) -> None:
        """Keep each parameter's colour while it stays on screen."""
        columns = list(dict.fromkeys(s.column for s in self.data.series)) if self.data else []
        self._slots = {c: s for c, s in self._slots.items() if c in columns}
        for column in columns:
            if column not in self._slots:
                used = set(self._slots.values())
                self._slots[column] = next(i for i in range(len(columns) + len(used) + 1) if i not in used)

    # ---------------------------------------------------------------- build
    def _groups(self) -> dict[tuple[str | None, bool], list[PlotSeries]]:
        """Series per plot, keyed by (parameter or None for the overlay, text).

        Stacked plots show one parameter each; the overlay shows all numbers
        together. Text needs its own category axis, so it is never overlaid.
        """
        groups: dict[tuple[str | None, bool], list[PlotSeries]] = {}
        for series in self.data.series:
            text = series.categories is not None
            key = (series.column if self.stacked or text else None, text)
            groups.setdefault(key, []).append(series)
        return groups

    def _rebuild(self, message: str | None = None) -> None:
        self.glw.clear()
        self._plots, self._keys, self._vlines, self._previews = [], [], [], []
        self._placeholder = None
        for entry in self._ranges:
            entry.regions, entry.tag = [], None
        if not self.data or not self.data.series:
            self._show_placeholder(message or "Select one or more parameters to plot")
            return
        top = 0
        if self.style.title:
            self.glw.addLabel(html.escape(self.style.title), row=0, col=0, color=self.theme.text, size="12pt", bold=True)
            top = 1
        groups = self._groups()
        for row, ((column, _text), series_list) in enumerate(groups.items()):
            last = row == len(groups) - 1
            plot = self._make_plot(top + row, last, series_list[0].categories)
            label = self.label_for(column) if column is not None else self.style.overlay_label
            if label:
                plot.setLabel("left", html.escape(label), color=self.theme.text_muted)
            self._add_series(plot, series_list)
            self._plots.append(plot)
            self._keys.append(column)
        first_vb = self._plots[0].getViewBox()
        first_vb.sigXRangeChanged.connect(self._on_x_range_changed)
        for plot, key in zip(self._plots, self._keys):
            plot.enableAutoRange(axis="x")
            self._apply_y_mode(plot, key)
        for number, entry in enumerate(self._ranges, 1):
            self._create_regions(entry, number)

    def _make_plot(self, row: int, last: bool, categories: list[str] | None = None) -> pg.PlotItem:
        vb = SelectViewBox(self)
        if self.kind == "datetime":
            bottom = pg.DateAxisItem(orientation="bottom", utcOffset=0)
        elif self.kind == "category":
            bottom = CategoryAxis("bottom", self.data.x_categories or [])
        else:
            bottom = pg.AxisItem(orientation="bottom")
        axes = {"bottom": bottom}
        if categories is not None:
            axes["left"] = CategoryAxis("left", categories)
        plot = self.glw.addPlot(row=row, col=0, viewBox=vb, axisItems=axes)
        grid = self.style.grid > 0
        plot.showGrid(x=grid, y=grid, alpha=self.style.grid / 100)
        plot.setMenuEnabled(True)
        plot.hideButtons()
        for name in ("left", "bottom"):
            axis = plot.getAxis(name)
            axis.setPen(pg.mkPen(self.theme.axis))
            axis.setTextPen(pg.mkPen(self.theme.text_muted))
        plot.getAxis("left").setWidth(AXIS_WIDTH)
        if not last and self.stacked:
            plot.getAxis("bottom").setStyle(showValues=False)
            plot.getAxis("bottom").setHeight(6)
        if last and self.data:
            title = self.style.labels.get(self.data.x_name)
            if not title:
                title = self.label_for(self.data.x_name)
                if self.kind == "datetime" and self.data.x_tz:
                    title += " (UTC)"
            plot.setLabel("bottom", html.escape(title), color=self.theme.text_muted)
        if self._plots:
            plot.setXLink(self._plots[0])
        vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(self.theme.text_muted, style=Qt.PenStyle.DashLine))
        vline.hide()
        plot.addItem(vline, ignoreBounds=True)
        self._vlines.append(vline)
        return plot

    def _series_label(self, series: PlotSeries, func: str | None = None) -> str:
        func = func or series.func
        name = self.label_for(series.column)
        if self.stacked:
            return func or name
        return f"{name} ({func})" if func else name

    def _as_bars(self, series: PlotSeries) -> bool:
        """Numbers on a text x axis with one value per category are drawn as bars."""
        if self.kind != "category" or series.categories is not None:
            return False
        return len(np.unique(series.x)) == len(series.x)

    def _add_series(self, plot: pg.PlotItem, series_list: list[PlotSeries]) -> None:
        columns = list(dict.fromkeys(s.column for s in series_list))
        needs_legend = len(series_list) > 1 and self.style.legend
        width = self.style.line_width
        if needs_legend:
            legend = plot.addLegend(offset=(8, 6), labelTextColor=self.theme.text, brush=with_alpha(self.theme.surface, 200))
            legend.setZValue(20)
        bars = [id(s) for s in series_list if self._as_bars(s)]
        for column in columns:
            members = [s for s in series_list if s.column == column]
            color = self.color_for(column)
            slot = self._slots.get(column, 0)
            base_style = _LINE_STYLES[(slot // len(self.theme.series)) % len(_LINE_STYLES)]
            other_funcs = [s.func for s in members if s.func not in _MINMAX]
            funcs = {s.func for s in members}
            # A shaded min–max band, unless the values are text or bars.
            band = _MINMAX <= funcs and members[0].categories is None and self.kind != "category"
            curves: dict[str | None, pg.PlotDataItem] = {}
            for number, series in enumerate(members):
                name = html.escape(self._series_label(series)) if needs_legend else None
                if id(series) in bars:
                    brush = with_alpha(color, _BAR_ALPHA[number % len(_BAR_ALPHA)])
                    self._add_bars(plot, series, bars.index(id(series)), len(bars), brush, name)
                    continue
                if band and series.func in _MINMAX:
                    pen = pg.mkPen(with_alpha(color, 150), width=width)
                    name = html.escape(self._series_label(series, "min–max")) if name and series.func == "min" else None
                else:
                    index = other_funcs.index(series.func) if series.func in other_funcs else number
                    style = _LINE_STYLES[index % len(_LINE_STYLES)] if index else base_style
                    pen = pg.mkPen(color, width=width, style=style)
                if self.kind == "category":
                    # Several values per category: markers, since lines between categories mean nothing.
                    self._add_points(plot, series, pen.color(), name)
                    continue
                x, y = (series.x, series.y) if series.categories is None else _steps(series.x, series.y)
                curve = plot.plot(x, y, pen=pen, name=name, connect="finite")
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                curves[series.func] = curve
                if self.show_points:
                    self._add_points(plot, series, pen.color())
            if band and "min" in curves and "max" in curves:
                band = pg.FillBetweenItem(curves["min"], curves["max"], brush=with_alpha(color, 45))
                band.setZValue(-5)
                plot.addItem(band)

    def _add_points(self, plot: pg.PlotItem, series: PlotSeries, color: QColor, name: str | None = None) -> None:
        """Mark every sample, so values between gaps (nulls) show without a line."""
        # Only finite samples: downsampling turns every chunk containing a NaN into NaN.
        finite = np.isfinite(series.x) & np.isfinite(series.y)
        points = plot.plot(
            series.x[finite], series.y[finite], pen=None, symbol="o", symbolSize=self.style.point_size, symbolPen=None,
            symbolBrush=color, name=name,
        )
        if self.kind != "category":  # downsampling assumes x is sorted
            points.setDownsampling(auto=True, method="peak")
        points.setClipToView(True)

    def _add_bars(self, plot: pg.PlotItem, series: PlotSeries, index: int, count: int, brush: QColor, name: str | None) -> None:
        """One bar per category; several series share each category's slot side by side."""
        finite = np.isfinite(series.x) & np.isfinite(series.y)
        width = 0.8 / count
        offset = (index - (count - 1) / 2) * width
        bars = pg.BarGraphItem(
            x=series.x[finite] + offset, height=series.y[finite], width=width * 0.92, brush=brush,
            pen=pg.mkPen(with_alpha(brush, 255), width=1), name=name,
        )
        plot.addItem(bars)

    def _apply_y_mode(self, plot: pg.PlotItem, key: str | None) -> None:
        vb = plot.getViewBox()
        vb.setMouseEnabled(x=True, y=not self.auto_y)
        limits = self._y_limits.get(key)
        if limits is not None:
            vb.setAutoVisible(y=False)
            vb.setYRange(*limits, padding=0)  # also turns off automatic y ranging
            return
        vb.setAutoVisible(y=self.auto_y)
        if self.auto_y:
            vb.enableAutoRange(axis="y")

    def _show_placeholder(self, text: str) -> None:
        plot = self.glw.addPlot(row=0, col=0)
        plot.hideAxis("left")
        plot.hideAxis("bottom")
        plot.setMouseEnabled(False, False)
        plot.hideButtons()
        item = pg.TextItem(text, color=self.theme.text_muted, anchor=(0.5, 0.5))
        plot.addItem(item)
        item.setPos(0.5, 0.5)
        plot.setRange(xRange=(0, 1), yRange=(0, 1), padding=0)
        self._placeholder = item

    # ----------------------------------------------------------------- view
    def view_range(self) -> tuple[float, float] | None:
        if not self._plots:
            return None
        lo, hi = self._plots[0].getViewBox().viewRange()[0]
        return float(lo), float(hi)

    def set_x_view(self, lo: float, hi: float, padding: float = 0.02) -> None:
        if not self._plots or hi <= lo:
            return
        self._plots[0].getViewBox().setXRange(lo, hi, padding=padding)
        for plot, key in zip(self._plots, self._keys):
            if self.auto_y and key not in self._y_limits:
                plot.getViewBox().enableAutoRange(axis="y")

    def reset_view(self) -> None:
        """Show all data, also in plots with a fixed y range."""
        self._y_limits.clear()
        for plot, key in zip(self._plots, self._keys):
            plot.enableAutoRange()
            self._apply_y_mode(plot, key)
        if self._category_span():
            self.set_x_view(*self._category_span(), padding=0.01)

    def _on_x_range_changed(self, _vb, rng) -> None:
        if not self._exporting:
            self.viewChanged.emit(float(rng[0]), float(rng[1]))

    # --------------------------------------------------------------- y ranges
    def y_axes(self) -> list[tuple[str | None, str, tuple[float, float]]]:
        """``(key, title, shown y range)`` of every plot with a numeric y axis."""
        out = []
        for plot, key in zip(self._plots, self._keys):
            if isinstance(plot.getAxis("left"), CategoryAxis):
                continue
            lo, hi = plot.getViewBox().viewRange()[1]
            out.append((key, OVERLAY_TITLE if key is None else self.label_for(key), (float(lo), float(hi))))
        return out

    def y_limits(self) -> dict[str | None, tuple[float, float]]:
        """Fixed y ranges by plot key (a parameter, or None for the overlay)."""
        return dict(self._y_limits)

    def set_y_limits(self, limits: dict[str | None, tuple[float, float] | None]) -> None:
        """Fix the y range of plots; ``None`` returns a plot to automatic y scaling."""
        released = set()
        for key, value in limits.items():
            if value is None:
                if self._y_limits.pop(key, None) is not None:
                    released.add(key)
            else:
                lo, hi = value
                if hi > lo:
                    self._y_limits[key] = (float(lo), float(hi))
        for plot, key in zip(self._plots, self._keys):
            if key in released:
                plot.getViewBox().enableAutoRange(axis="y")
            self._apply_y_mode(plot, key)

    def clear_y_limits(self, keys: Iterable[str | None] | None = None) -> None:
        keys = list(self._y_limits) if keys is None else list(keys)
        self.set_y_limits({key: None for key in keys})

    # ---------------------------------------------------------------- ranges
    def ranges(self) -> list[RangeEntry]:
        return list(self._ranges)

    def add_range(self, lo: float, hi: float, label: str = "", notify: bool = True, color: str = "") -> RangeEntry:
        entry = RangeEntry(min(lo, hi), max(lo, hi), label, color if parse_color(color) else "")
        self._ranges.append(entry)
        self._create_regions(entry, len(self._ranges))
        if notify:
            self.rangesChanged.emit()
        return entry

    def set_ranges(self, spans: Sequence[tuple]) -> None:
        """Replace the ranges with ``(lo, hi, label)`` or ``(lo, hi, label, color)`` spans."""
        self._remove_all_regions()
        self._ranges = []
        for lo, hi, label, *color in spans:
            self.add_range(lo, hi, label, notify=False, color=color[0] if color else "")
        self.rangesChanged.emit()

    def add_range_from_view(self) -> RangeEntry | None:
        view = self.view_range()
        if view is None or not self.allow_ranges:
            return None
        lo, hi = view
        if self.data and self.data.bounds:
            lo, hi = max(lo, self.data.bounds[0]), min(hi, self.data.bounds[1])
        width = hi - lo
        return self.add_range(lo + width * 0.25, hi - width * 0.25)

    def update_range(self, index: int, lo: float | None = None, hi: float | None = None, label: str | None = None) -> None:
        entry = self._ranges[index]
        if lo is not None:
            entry.lo = lo
        if hi is not None:
            entry.hi = hi
        if entry.hi < entry.lo:
            entry.lo, entry.hi = entry.hi, entry.lo
        if label is not None:
            entry.label = label
        self._syncing = True
        try:
            for region in entry.regions:
                region.setRegion((entry.lo, entry.hi))
        finally:
            self._syncing = False
        self._update_tags()
        self.rangesChanged.emit()

    def range_color(self, entry: RangeEntry) -> QColor:
        return parse_color(entry.color) or self.default_range_color()

    def default_range_color(self) -> QColor:
        return parse_color(self.style.range_color) or QColor(self.theme.region)

    def set_range_color(self, index: int, color: str) -> None:
        """Give range *index* its own highlight colour (empty for the default)."""
        entry = self._ranges[index]
        entry.color = color if parse_color(color) else ""
        self._remove_regions(entry)
        self._create_regions(entry, index + 1)
        self.rangesChanged.emit()

    def _choose_range_color(self, entry: RangeEntry) -> None:
        if entry not in self._ranges:
            return
        chosen = QColorDialog.getColor(self.range_color(entry), self, "Range colour")
        if chosen.isValid() and entry in self._ranges:
            self.set_range_color(self._ranges.index(entry), chosen.name())

    def remove_range(self, index: int) -> None:
        entry = self._ranges.pop(index)
        self._remove_regions(entry)
        self._update_tags()
        self.rangesChanged.emit()

    def clear_ranges(self) -> None:
        self._remove_all_regions()
        self._ranges.clear()
        self.rangesChanged.emit()

    def zoom_to_range(self, index: int) -> None:
        entry = self._ranges[index]
        self.set_x_view(entry.lo, entry.hi, padding=0.05)

    def zoom_to_ranges(self) -> None:
        if self._ranges:
            self.set_x_view(min(e.lo for e in self._ranges), max(e.hi for e in self._ranges), padding=0.05)

    def _create_regions(self, entry: RangeEntry, number: int) -> None:
        color = self.range_color(entry)
        alpha = round(self.style.range_opacity * 2.55)
        brush = with_alpha(color, alpha)
        hover = with_alpha(color, min(255, alpha + 32))
        line_pen = pg.mkPen(with_alpha(color, 200), width=1)
        for i, plot in enumerate(self._plots):
            region = RangeRegion(
                (entry.lo, entry.hi), brush=brush, hoverBrush=hover, pen=line_pen, swapMode="sort"
            )
            region.setZValue(-10)
            plot.addItem(region, ignoreBounds=True)
            region.sigRegionChanged.connect(lambda r, e=entry: self._on_region_changed(e, r))
            region.sigRegionChangeFinished.connect(lambda _r: self.rangesChanged.emit())
            region.sigRemoveRequested.connect(lambda _r, e=entry: self._remove_entry(e))
            region.sigZoomRequested.connect(lambda _r, e=entry: self.set_x_view(e.lo, e.hi, padding=0.05))
            region.sigColorRequested.connect(lambda _r, e=entry: self._choose_range_color(e))
            entry.regions.append(region)
            if i == 0:
                entry.tag = pg.InfLineLabel(
                    region.lines[0], text="", position=0.92, anchors=[(0, 0), (0, 0)], color=self.theme.text
                )
        self._update_tags()

    def _update_tags(self) -> None:
        for number, entry in enumerate(self._ranges, 1):
            if entry.tag is not None:
                entry.tag.setFormat(f" {entry.label or number}")

    def _remove_entry(self, entry: RangeEntry) -> None:
        if entry in self._ranges:
            self.remove_range(self._ranges.index(entry))

    def _remove_regions(self, entry: RangeEntry) -> None:
        for region in entry.regions:
            vb = region.getViewBox()
            if vb is not None:
                vb.removeItem(region)
        entry.regions, entry.tag = [], None

    def _remove_all_regions(self) -> None:
        for entry in self._ranges:
            self._remove_regions(entry)

    def _on_region_changed(self, entry: RangeEntry, source: pg.LinearRegionItem) -> None:
        if self._syncing:
            return
        lo, hi = source.getRegion()
        entry.lo, entry.hi = float(lo), float(hi)
        self._syncing = True
        try:
            for region in entry.regions:
                if region is not source:
                    region.setRegion((lo, hi))
        finally:
            self._syncing = False

    def _drag_select(self, lo: float, hi: float, finished: bool) -> None:
        if not self._previews:
            pen = pg.mkPen(self.theme.selection, width=1)
            for plot in self._plots:
                preview = pg.LinearRegionItem((lo, hi), movable=False, brush=with_alpha(self.theme.selection, 50), pen=pen)
                preview.setZValue(-8)
                plot.addItem(preview, ignoreBounds=True)
                self._previews.append(preview)
        for preview in self._previews:
            preview.setRegion((lo, hi))
        if finished:
            for preview in self._previews:
                vb = preview.getViewBox()
                if vb is not None:
                    vb.removeItem(preview)
            self._previews = []
            view = self.view_range()
            min_width = (view[1] - view[0]) * 1e-4 if view else 0
            if hi - lo > min_width:
                self.add_range(lo, hi)

    # ---------------------------------------------------------------- images
    def set_menu_actions(self, actions: Sequence[QAction]) -> None:
        """Actions added to the right-click menu of every plot (in place of pyqtgraph's own export)."""
        self.glw.scene().contextMenu = list(actions)

    def image_size(self) -> QSize:
        """Size of the plots on screen, in logical pixels."""
        rect = self.glw.ci.geometry()
        return QSize(max(1, round(rect.width())), max(1, round(rect.height())))

    def render_image(self, size: QSize | None = None, resolution: float = 1.0, transparent: bool = False) -> QImage:
        """The plots laid out at *size* (default: as on screen), rendered at *resolution* pixels per pixel."""
        with self._export_layout(size) as source:
            width = max(1, round(source.width() * resolution))
            height = max(1, round(source.height() * resolution))
            image = QImage(width, height, QImage.Format.Format_ARGB32)
            image.fill(QColor(0, 0, 0, 0) if transparent else QColor(self.theme.surface))
            painter = QPainter(image)
            try:
                self._paint(painter, QRectF(0, 0, width, height), source, resolution)
            finally:
                painter.end()
        return image

    def save_image(self, path: str | Path, size: QSize | None = None, resolution: float = 1.0,
                   transparent: bool = False) -> Path:
        """Save the plots as PNG, JPEG, SVG or PDF (chosen by the file suffix).

        *size* is the layout size in logical pixels; raster images have
        *resolution* times as many pixels, so that text and lines keep their
        proportions. Raises OSError or ValueError when the image cannot be written.
        """
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix not in IMAGE_FORMATS:
            raise ValueError(f"Unsupported image format {suffix or '(none)'}; use {', '.join(IMAGE_FORMATS)}")
        if not self._plots:
            raise ValueError("There is no plot to save")
        transparent = transparent and suffix in TRANSPARENT_FORMATS
        if suffix == ".svg":
            self._save_svg(path, size, transparent)
        elif suffix == ".pdf":
            self._save_pdf(path, size, transparent)
        else:
            image = self.render_image(size, resolution, transparent)
            if not image.save(str(path), IMAGE_FORMATS[suffix], 95):
                raise OSError(f"Could not write {path}")
        return path

    def copy_image(self, resolution: float = 2.0) -> None:
        """Put an image of the plots on the clipboard."""
        if self._plots:
            QGuiApplication.clipboard().setImage(self.render_image(resolution=resolution))

    def _save_svg(self, path: Path, size: QSize | None, transparent: bool) -> None:
        from PySide6.QtSvg import QSvgGenerator

        with self._export_layout(size) as source:
            generator = QSvgGenerator()
            generator.setFileName(str(path))
            generator.setTitle(self.style.title or path.stem)
            # Axes replay pictures recorded at the screen's resolution; any other would scale them.
            generator.setResolution(QPicture().logicalDpiX())
            generator.setSize(QSize(round(source.width()), round(source.height())))
            generator.setViewBox(QRect(0, 0, round(source.width()), round(source.height())))
            self._paint_vector(generator, source, transparent)
        if not path.exists():
            raise OSError(f"Could not write {path}")

    def _save_pdf(self, path: Path, size: QSize | None, transparent: bool) -> None:
        with self._export_layout(size) as source:
            dpi = QPicture().logicalDpiX()
            writer = QPdfWriter(str(path))
            writer.setTitle(self.style.title or path.stem)
            writer.setCreator("Parquetry")
            points = QSizeF(source.width() * 72 / dpi, source.height() * 72 / dpi)
            page = QPageSize(points, QPageSize.Unit.Point, "", QPageSize.SizeMatchPolicy.ExactMatch)
            writer.setPageLayout(QPageLayout(page, QPageLayout.Orientation.Portrait, QMarginsF(0, 0, 0, 0)))
            writer.setResolution(dpi)
            self._paint_vector(writer, source, transparent)
        if not path.exists() or path.stat().st_size == 0:
            raise OSError(f"Could not write {path}")

    def _paint_vector(self, device, source: QRectF, transparent: bool) -> None:
        painter = QPainter()
        if not painter.begin(device):
            raise OSError("Could not start writing the image")
        try:
            target = QRectF(0, 0, source.width(), source.height())
            if not transparent:
                painter.fillRect(target, QColor(self.theme.surface))
            self._paint(painter, target, source, 1.0)
        finally:
            painter.end()

    def _paint(self, painter: QPainter, target: QRectF, source: QRectF, resolution: float) -> None:
        # pyqtgraph's exporters tell items that they are being exported (antialiasing, symbol scaling).
        exporter = ImageExporter(self.glw.ci)
        exporter.setExportMode(True, {
            "antialias": True, "background": None, "painter": painter, "resolutionScale": resolution,
        })
        # Legends ignore transformations, so they would keep their screen size in a larger image.
        ignores = QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations
        legends = [plot.legend for plot in self._plots if plot.legend is not None and plot.legend.flags() & ignores]
        for legend in legends:
            legend.setFlag(ignores, False)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
            self.glw.scene().render(painter, target, source)
        finally:
            for legend in legends:
                legend.setFlag(ignores, True)
            exporter.setExportMode(False)

    @contextmanager
    def _export_layout(self, size: QSize | None) -> Iterator[QRectF]:
        """Lay the plots out at *size* while exporting, without the crosshair; yields the scene area."""
        layout = self.glw.ci
        original = QRectF(layout.geometry())
        resize = size is not None and (size.width(), size.height()) != (round(original.width()), round(original.height()))
        lines = [line for line in self._vlines if line.isVisible()]
        for line in lines:
            line.hide()
        self._exporting = True
        try:
            if resize:
                layout.setGeometry(QRectF(0, 0, max(1, size.width()), max(1, size.height())))
                layout.layout.activate()
            yield layout.mapRectToScene(layout.boundingRect())
        finally:
            if resize:
                layout.setGeometry(original)
                layout.layout.activate()
                for plot in self._plots:  # automatic ranges pad by size; settle them now, not at the next paint
                    vb = plot.getViewBox()
                    if any(vb.state["autoRange"]):
                        vb.updateAutoRange()
            for line in lines:
                line.show()
            self._exporting = False

    # ------------------------------------------------------------- crosshair
    def _show_hint(self) -> None:
        ranges = " · <b>Shift+drag</b> or range mode (<b>R</b>) to select a range" if self.allow_ranges else ""
        self.readout.setText(
            f"<span style='color:{current_theme().text_muted}'>Scroll to zoom · drag to pan{ranges} · "
            "right-click for options</span>"
        )

    def _on_mouse_moved(self, event) -> None:
        pos: QPointF = event[0]
        if not self.data or not self._plots:
            return
        inside = None
        for plot in self._plots:
            if plot.sceneBoundingRect().contains(pos):
                inside = plot
                break
        if inside is None:
            for line in self._vlines:
                line.hide()
            self._show_hint()
            return
        x = inside.getViewBox().mapSceneToView(pos).x()
        for line in self._vlines:
            line.setPos(x)
            line.show()
        if self.kind == "category":
            x_text = category_name(self.data.x_categories or [], x)
        else:
            x_text = format_plot_x(x, self.kind)
        parts = [f"<b>{html.escape(x_text)}</b>"]
        for series in self.data.series:
            swatch = self.color_for(series.column).name()
            name = self.label_for(series.column)
            label = f"{name} ({series.func})" if series.func else name
            parts.append(
                f"<span style='color:{swatch}'>■</span> {html.escape(label)} "
                f"<span style='font-family:monospace'>{html.escape(self._value_text(series, x))}</span>"
            )
        self.readout.setText("&nbsp;&nbsp; ".join(parts))

    def _value_text(self, series: PlotSeries, x: float) -> str:
        def text(value: float | None) -> str:
            return format_value(value) if series.categories is None else category_name(series.categories, value)

        if self.kind != "category":
            return text(_value_at(series, x))
        values = series.y[(series.x == round(x)) & np.isfinite(series.y)]
        if len(values) <= 1:
            return text(values[0] if len(values) else None)
        if series.categories is not None:
            return f"{len(values)} values"
        return f"{text(values.min())} … {text(values.max())} ({len(values)} values)"


def _steps(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vertices of a line that holds each value until the next sample."""
    if len(x) < 2:
        return x, y
    return np.repeat(x, 2)[1:], np.repeat(y, 2)[:-1]


def _value_at(series: PlotSeries, x: float) -> float | None:
    xs = series.x
    if len(xs) == 0:
        return None
    if series.categories is not None:  # text holds its value until the next sample
        j = int(np.searchsorted(xs, x, side="right")) - 1
        return float(series.y[j]) if j >= 0 else None
    i = int(np.searchsorted(xs, x))
    if i <= 0:
        j = 0
    elif i >= len(xs):
        j = len(xs) - 1
    else:
        j = i if abs(xs[i] - x) < abs(x - xs[i - 1]) else i - 1
    return float(series.y[j])
