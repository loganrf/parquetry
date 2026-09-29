"""Interactive plot: stacked or overlaid series, crosshair readout and x ranges."""

from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass, field

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import QLabel, QMenu, QVBoxLayout, QWidget

from ..processing import PlotData, PlotSeries
from .theme import PlotTheme, current_theme, with_alpha

_MINMAX = {"min", "max"}
_LINE_STYLES = (Qt.PenStyle.SolidLine, Qt.PenStyle.DashLine, Qt.PenStyle.DotLine, Qt.PenStyle.DashDotLine)
AXIS_WIDTH = 72


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


@dataclass
class RangeEntry:
    lo: float
    hi: float
    label: str = ""
    regions: list[pg.LinearRegionItem] = field(default_factory=list)
    tag: pg.InfLineLabel | None = None


class RangeRegion(pg.LinearRegionItem):
    """A draggable x range with a right-click menu."""

    sigRemoveRequested = Signal(object)
    sigZoomRequested = Signal(object)

    def mouseClickEvent(self, ev):  # noqa: N802 - Qt naming
        if ev.button() == Qt.MouseButton.RightButton and not self.moving:
            ev.accept()
            menu = QMenu()
            zoom = menu.addAction("Zoom to range")
            remove = menu.addAction("Remove range")
            chosen = menu.exec(ev.screenPos().toPoint())
            if chosen is remove:
                self.sigRemoveRequested.emit(self)
            elif chosen is zoom:
                self.sigZoomRequested.emit(self)
            return
        super().mouseClickEvent(ev)


class SelectViewBox(pg.ViewBox):
    """ViewBox where left-drag selects an x range in range mode or with Shift."""

    def __init__(self, area: PlotArea) -> None:
        super().__init__()
        self._area = area

    def mouseDragEvent(self, ev, axis=None):  # noqa: N802 - Qt naming
        selecting = self._area.select_mode or bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme: PlotTheme = current_theme()
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
        self.auto_y = True
        self.select_mode = False
        self._plots: list[pg.PlotItem] = []
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

    def set_data(self, data: PlotData | None, keep_view: bool = True) -> None:
        view = self.view_range() if keep_view and self._plots else None
        self.data = data
        self._assign_colors()
        self._rebuild()
        if view is not None and self._plots:
            self.set_x_view(*view, padding=0)

    def clear(self) -> None:
        self._ranges.clear()
        self._slots.clear()
        self.set_data(None, keep_view=False)
        self.rangesChanged.emit()

    def set_stacked(self, stacked: bool) -> None:
        if stacked != self.stacked:
            self.stacked = stacked
            self.set_data(self.data)

    def set_auto_y(self, enabled: bool) -> None:
        self.auto_y = enabled
        for plot in self._plots:
            self._apply_y_mode(plot)

    def set_select_mode(self, enabled: bool) -> None:
        self.select_mode = enabled
        self.glw.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)

    def color_for(self, column: str) -> QColor:
        return self.theme.series_color(self._slots.get(column, 0))

    def _assign_colors(self) -> None:
        """Keep each parameter's colour while it stays on screen."""
        columns = list(dict.fromkeys(s.column for s in self.data.series)) if self.data else []
        self._slots = {c: s for c, s in self._slots.items() if c in columns}
        for column in columns:
            if column not in self._slots:
                used = set(self._slots.values())
                self._slots[column] = next(i for i in range(len(columns) + len(used) + 1) if i not in used)

    # ---------------------------------------------------------------- build
    def _groups(self) -> dict[str, list[PlotSeries]]:
        groups: dict[str, list[PlotSeries]] = {}
        for series in self.data.series:
            groups.setdefault(series.column if self.stacked else "", []).append(series)
        return groups

    def _rebuild(self) -> None:
        self.glw.clear()
        self._plots, self._vlines, self._previews = [], [], []
        self._placeholder = None
        for entry in self._ranges:
            entry.regions, entry.tag = [], None
        if not self.data or not self.data.series:
            self._show_placeholder("Select one or more parameters to plot")
            return
        groups = self._groups()
        for row, (column, series_list) in enumerate(groups.items()):
            last = row == len(groups) - 1
            plot = self._make_plot(row, last)
            if self.stacked:
                plot.setLabel("left", column, color=self.theme.text_muted)
            self._add_series(plot, series_list)
            self._plots.append(plot)
        first_vb = self._plots[0].getViewBox()
        first_vb.sigXRangeChanged.connect(self._on_x_range_changed)
        for plot in self._plots:
            plot.enableAutoRange(axis="x")
            self._apply_y_mode(plot)
        for number, entry in enumerate(self._ranges, 1):
            self._create_regions(entry, number)

    def _make_plot(self, row: int, last: bool) -> pg.PlotItem:
        vb = SelectViewBox(self)
        if self.kind == "datetime":
            bottom = pg.DateAxisItem(orientation="bottom", utcOffset=0)
        else:
            bottom = pg.AxisItem(orientation="bottom")
        plot = self.glw.addPlot(row=row, col=0, viewBox=vb, axisItems={"bottom": bottom})
        plot.showGrid(x=True, y=True, alpha=0.12)
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
            title = "(row index)" if self.data.x_name == "__row_index__" else self.data.x_name
            if self.kind == "datetime":
                title += " (UTC)" if self.data.x_tz else ""
            plot.setLabel("bottom", title, color=self.theme.text_muted)
        if self._plots:
            plot.setXLink(self._plots[0])
        vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(self.theme.text_muted, style=Qt.PenStyle.DashLine))
        vline.hide()
        plot.addItem(vline, ignoreBounds=True)
        self._vlines.append(vline)
        return plot

    def _series_label(self, series: PlotSeries, func: str | None = None) -> str:
        func = func or series.func
        if self.stacked:
            return func or series.column
        return f"{series.column} ({func})" if func else series.column

    def _add_series(self, plot: pg.PlotItem, series_list: list[PlotSeries]) -> None:
        columns = list(dict.fromkeys(s.column for s in series_list))
        needs_legend = len(series_list) > 1
        if needs_legend:
            legend = plot.addLegend(offset=(8, 6), labelTextColor=self.theme.text, brush=with_alpha(self.theme.surface, 200))
            legend.setZValue(20)
        for column in columns:
            members = [s for s in series_list if s.column == column]
            color = self.color_for(column)
            slot = self._slots.get(column, 0)
            base_style = _LINE_STYLES[(slot // len(self.theme.series)) % len(_LINE_STYLES)]
            other_funcs = [s.func for s in members if s.func not in _MINMAX]
            funcs = {s.func for s in members}
            band = _MINMAX <= funcs
            curves: dict[str | None, pg.PlotDataItem] = {}
            for series in members:
                name = self._series_label(series) if needs_legend else None
                if series.func in _MINMAX:
                    pen = pg.mkPen(with_alpha(color, 150), width=1)
                    if band:  # one legend entry for the shaded min–max band
                        name = self._series_label(series, "min–max") if name and series.func == "min" else None
                else:
                    index = other_funcs.index(series.func)
                    style = _LINE_STYLES[index % len(_LINE_STYLES)] if index else base_style
                    pen = pg.mkPen(color, width=1, style=style)
                curve = plot.plot(series.x, series.y, pen=pen, name=name, connect="finite")
                curve.setDownsampling(auto=True, method="peak")
                curve.setClipToView(True)
                curves[series.func] = curve
            if "min" in curves and "max" in curves:
                band = pg.FillBetweenItem(curves["min"], curves["max"], brush=with_alpha(color, 45))
                band.setZValue(-5)
                plot.addItem(band)

    def _apply_y_mode(self, plot: pg.PlotItem) -> None:
        vb = plot.getViewBox()
        vb.setMouseEnabled(x=True, y=not self.auto_y)
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
        for plot in self._plots:
            if self.auto_y:
                plot.getViewBox().enableAutoRange(axis="y")

    def reset_view(self) -> None:
        for plot in self._plots:
            plot.enableAutoRange()
            if self.auto_y:
                plot.getViewBox().setAutoVisible(y=True)

    def _on_x_range_changed(self, _vb, rng) -> None:
        self.viewChanged.emit(float(rng[0]), float(rng[1]))

    # ---------------------------------------------------------------- ranges
    def ranges(self) -> list[RangeEntry]:
        return list(self._ranges)

    def add_range(self, lo: float, hi: float, label: str = "", notify: bool = True) -> RangeEntry:
        entry = RangeEntry(min(lo, hi), max(lo, hi), label)
        self._ranges.append(entry)
        self._create_regions(entry, len(self._ranges))
        if notify:
            self.rangesChanged.emit()
        return entry

    def set_ranges(self, spans: list[tuple[float, float, str]]) -> None:
        self._remove_all_regions()
        self._ranges = []
        for lo, hi, label in spans:
            self.add_range(lo, hi, label, notify=False)
        self.rangesChanged.emit()

    def add_range_from_view(self) -> RangeEntry | None:
        view = self.view_range()
        if view is None:
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
        brush = with_alpha(self.theme.region, 38)
        hover = with_alpha(self.theme.region, 70)
        line_pen = pg.mkPen(with_alpha(self.theme.region, 200), width=1)
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

    # ------------------------------------------------------------- crosshair
    def _show_hint(self) -> None:
        self.readout.setText(
            f"<span style='color:{self.theme.text_muted}'>Scroll to zoom · drag to pan · "
            "<b>Shift+drag</b> or range mode (<b>R</b>) to select a range · right-click for options</span>"
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
        parts = [f"<b>{html.escape(format_plot_x(x, self.kind))}</b>"]
        for series in self.data.series:
            value = _value_at(series, x)
            swatch = self.color_for(series.column).name()
            label = f"{series.column} ({series.func})" if series.func else series.column
            parts.append(
                f"<span style='color:{swatch}'>■</span> {html.escape(label)} "
                f"<span style='font-family:monospace'>{format_value(value)}</span>"
            )
        self.readout.setText("&nbsp;&nbsp; ".join(parts))


def _value_at(series: PlotSeries, x: float) -> float | None:
    xs = series.x
    if len(xs) == 0:
        return None
    i = int(np.searchsorted(xs, x))
    if i <= 0:
        j = 0
    elif i >= len(xs):
        j = len(xs) - 1
    else:
        j = i if abs(xs[i] - x) < abs(x - xs[i - 1]) else i - 1
    return float(series.y[j])
