"""The explorer page: parameter selection, aggregation, plot, ranges, scaling and data table."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..config import ProcessingConfig, Scaling, TimeRange
from ..dataset import DatasetInfo, format_bytes
from ..processing import (
    PlotData,
    ProcessingError,
    TableData,
    data_bounds,
    format_x,
    load_plot_data,
    load_table_data,
    merge_plot_data,
    plot_to_x,
    resolve_ranges,
    x_to_plot,
)
from . import workers
from .data_table import DataPanel
from .image_export import ImageExportDialog
from .panels import AggregationPanel, ParameterPanel, RangePanel, ScalingPanel
from .plot_area import PlotArea, format_plot_x
from .plot_settings import PlotSettingsDialog
from .theme import load_style, save_style


def _plot_job(info: DatasetInfo, cfg: ProcessingConfig, pending: ProcessingConfig | None, view=None):
    """Background job: load plot data and, optionally, resolve configured ranges."""
    data = load_plot_data(info, cfg, view=view)
    spans = None
    if pending is not None and pending.ranges and data.bounds:
        resolved = resolve_ranges(pending, info)
        lo_all, hi_all = data.bounds
        spans = []
        for (lo, hi), rng in zip(resolved, pending.ranges):
            spans.append(
                (
                    lo_all if lo is None else x_to_plot(lo, data.x_kind),
                    hi_all if hi is None else x_to_plot(hi, data.x_kind),
                    rng.label,
                    rng.color,
                )
            )
    return data, spans


class ExplorerPage(QWidget):
    """Explore one Parquet or CSV file."""

    busy = Signal(bool, str)
    status = Signal(str)
    error = Signal(str, str)
    openRequested = Signal()

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.info: DatasetInfo | None = None
        self.cfg = ProcessingConfig()
        self.plot_data: PlotData | None = None
        self._overview: PlotData | None = None
        self._generation = 0
        self._pending: ProcessingConfig | None = None
        self._loaded_x: str | None = None
        self._table_generation = 0
        self._table_stale = True
        #: Scaling shown in the scaling panel when it last changed, and the x scaling of the plot's units.
        self._scaling: dict[str, Scaling] = {}
        self._x_scaling: Scaling | None = None
        #: x window to show once the next plot data arrives (after the x units changed).
        self._next_view: tuple[float, float] | None = None

        # -- widgets -----------------------------------------------------------
        self.file_label = QLabel()
        self.file_label.setTextFormat(Qt.TextFormat.RichText)
        self.file_label.setWordWrap(True)
        self.params = ParameterPanel()
        self.params.set_sort(settings.value("parameters/sort", "file"), settings.value("parameters/reverse", False, type=bool))
        self.params.sortChanged.connect(self._sort_changed)
        self.params.xChanged.connect(self._x_changed)
        self.params.yChanged.connect(self._y_changed)
        self.aggregation = AggregationPanel()
        self.aggregation.changed.connect(self.schedule_update)
        self.auto_update = QCheckBox("Auto update")
        self.auto_update.setChecked(settings.value("explorer/auto_update", True, type=bool))
        self.auto_update.setToolTip("Reload the plot whenever parameters or aggregation change")
        self.auto_update.toggled.connect(lambda on: settings.setValue("explorer/auto_update", on))
        self.update_button = QPushButton("Update plot")
        self.update_button.clicked.connect(self.update_plot)
        self.export_button = QPushButton("Export CSV…")
        self.export_button.setDefault(True)

        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(6, 6, 6, 6)
        side.addWidget(self.file_label)
        side.addWidget(self.params, 1)
        side.addWidget(self.aggregation)
        row = QHBoxLayout()
        row.addWidget(self.auto_update)
        row.addStretch(1)
        row.addWidget(self.update_button)
        side.addLayout(row)
        side.addWidget(self.export_button)
        sidebar.setMinimumWidth(280)

        self.plot = PlotArea(style=load_style(settings))
        self.plot.set_stacked(settings.value("plot/stacked", True, type=bool))
        self.plot.set_show_points(settings.value("plot/points", False, type=bool))
        self.plot.rangesChanged.connect(self._ranges_changed)
        self.plot.viewChanged.connect(lambda *_: self._schedule_table())
        self.ranges_panel = RangePanel()
        self.ranges_panel.addRequested.connect(self.plot.add_range_from_view)
        self.ranges_panel.removeRequested.connect(self.plot.remove_range)
        self.ranges_panel.clearRequested.connect(self.plot.clear_ranges)
        self.ranges_panel.zoomRequested.connect(self.plot.zoom_to_range)
        self.ranges_panel.zoomAllRequested.connect(self.plot.zoom_to_ranges)
        self.ranges_panel.editRequested.connect(self._range_edited)
        self.ranges_panel.colorChanged.connect(self.plot.set_range_color)
        self.scaling_panel = ScalingPanel()
        self.scaling_panel.changed.connect(self._scaling_changed)
        self.scaling_panel.invalid.connect(self.status.emit)

        self.detail_banner = QLabel()
        self.detail_banner.setWordWrap(True)
        self.detail_banner.setTextFormat(Qt.TextFormat.RichText)
        self.detail_banner.linkActivated.connect(lambda _: self.load_detail())
        self.detail_banner.setContentsMargins(6, 2, 6, 2)
        self.detail_banner.hide()

        self._build_actions()
        self.toolbar = QToolBar("Plot")
        self.toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        for action in (
            self.act_open,
            None,
            self.act_select,
            self.act_add_range,
            self.act_zoom_ranges,
            None,
            self.act_stacked,
            self.act_points,
            self.act_auto_y,
            self.act_reset,
            self.act_detail,
            self.act_plot_settings,
            None,
            self.act_load_config,
            self.act_save_config,
            self.act_export,
            self.act_export_image,
        ):
            if action is None:
                self.toolbar.addSeparator()
            else:
                self.toolbar.addAction(action)

        right = QSplitter(Qt.Orientation.Vertical)
        plot_box = QWidget()
        plot_layout = QVBoxLayout(plot_box)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.setSpacing(0)
        plot_layout.addWidget(self.detail_banner)
        plot_layout.addWidget(self.plot, 1)
        self.data_panel = DataPanel()
        self.bottom_tabs = QTabWidget()
        self.bottom_tabs.setDocumentMode(True)
        self.bottom_tabs.addTab(self.ranges_panel, "Ranges")
        self.bottom_tabs.addTab(self.data_panel, "Data")
        self.bottom_tabs.setTabToolTip(1, "The rows behind the visible part of the plot, including text columns")
        self.bottom_tabs.addTab(self.scaling_panel, "Scaling")
        self.bottom_tabs.setTabToolTip(2, "Scale and offset of values, e.g. to convert units or correct a sensor")
        self.bottom_tabs.setCurrentIndex(settings.value("explorer/bottom_tab", 0, type=int))
        self.bottom_tabs.currentChanged.connect(self._bottom_tab_changed)
        right.addWidget(plot_box)
        right.addWidget(self.bottom_tabs)
        right.setStretchFactor(0, 4)
        right.setStretchFactor(1, 1)
        right.setSizes([600, 200])
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(sidebar)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([320, 1000])
        state = settings.value("explorer/splitter")
        if state is not None:
            self.splitter.restoreState(state)
        self.splitter.splitterMoved.connect(lambda *_: settings.setValue("explorer/splitter", self.splitter.saveState()))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.splitter, 1)

        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(300)
        self._update_timer.timeout.connect(self.update_plot)
        self._table_timer = QTimer(self)
        self._table_timer.setSingleShot(True)
        self._table_timer.setInterval(250)
        self._table_timer.timeout.connect(self.update_table)

    # ----------------------------------------------------------------- actions
    def _build_actions(self) -> None:
        def action(text: str, shortcut: str | None = None, tip: str = "", checkable: bool = False) -> QAction:
            act = QAction(text, self)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
                act.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            if tip:
                act.setToolTip(f"{tip} ({shortcut})" if shortcut else tip)
                act.setStatusTip(tip)
            act.setCheckable(checkable)
            return act

        self.act_open = action("Open…", "Ctrl+O", "Choose another Parquet or CSV file")
        self.act_open.triggered.connect(self.openRequested)
        self.act_select = action("Range mode", "R", "Left-drag on the plot selects an x range (Shift+drag works anytime)", True)
        self.act_select.toggled.connect(self.plot.set_select_mode)
        self.act_add_range = action("Add range", "A", "Add a range covering the middle of the visible area")
        self.act_add_range.triggered.connect(self.plot.add_range_from_view)
        self.act_zoom_ranges = action("Zoom to ranges", "Z", "Zoom to the selected ranges")
        self.act_zoom_ranges.triggered.connect(self.plot.zoom_to_ranges)
        self.act_stacked = action("Stacked", "S", "One plot per parameter (off: overlay all parameters)", True)
        self.act_stacked.setChecked(self.plot.stacked)
        self.act_stacked.toggled.connect(self._set_stacked)
        self.act_points = action("Points", "P", "Mark every data point, so values between gaps (nulls) stay visible", True)
        self.act_points.setChecked(self.plot.show_points)
        self.act_points.toggled.connect(self._set_show_points)
        self.act_auto_y = action("Auto Y", "Y", "Fit the y axis to the visible data", True)
        self.act_auto_y.setChecked(True)
        self.act_auto_y.toggled.connect(self.plot.set_auto_y)
        self.act_reset = action("Reset zoom", "Ctrl+0", "Show all data")
        self.act_reset.triggered.connect(self.plot.reset_view)
        self.act_detail = action("Load detail", "D", "Load full-resolution data for the visible area")
        self.act_detail.triggered.connect(self.load_detail)
        self.act_detail.setEnabled(False)
        self.act_load_config = action("Load config…", "Ctrl+L", "Apply a saved processing configuration")
        self.act_save_config = action("Save config…", "Ctrl+S", "Save the current processing configuration")
        self.act_export = action("Export CSV…", "Ctrl+E", "Export the selected data to CSV")
        self.export_button.clicked.connect(self.act_export.trigger)
        self.act_plot_settings = action(
            "Plot settings…", "Ctrl+Shift+P", "Colours, line widths, labels, title and axis ranges of the plots"
        )
        self.act_plot_settings.triggered.connect(self.open_plot_settings)
        self.act_export_image = action("Export image…", "Ctrl+Shift+E", "Save the plots as a PNG, JPEG, SVG or PDF image")
        self.act_export_image.triggered.connect(self.export_image)
        self.act_copy_image = action("Copy plot image", "Ctrl+Shift+C", "Copy the plots to the clipboard as an image")
        self.act_copy_image.triggered.connect(self.copy_image)
        self.plot.set_menu_actions([self.act_plot_settings, self.act_export_image, self.act_copy_image])

    def _set_stacked(self, on: bool) -> None:
        self.settings.setValue("plot/stacked", on)
        self.plot.set_stacked(on)

    def _set_show_points(self, on: bool) -> None:
        self.settings.setValue("plot/points", on)
        self.plot.set_show_points(on)

    def _sort_changed(self) -> None:
        key, reverse = self.params.sort_order()
        self.settings.setValue("parameters/sort", key)
        self.settings.setValue("parameters/reverse", reverse)

    def update_actions(self) -> None:
        """Enable the actions that suit the x axis and the plot (while the page is shown)."""
        if not self.toolbar.isEnabled():
            return  # the main window disables everything while the file browser is shown
        ranges = self.plot.allow_ranges
        for act in (self.act_select, self.act_add_range, self.act_zoom_ranges):
            act.setEnabled(ranges)
        data = self._overview
        self.act_detail.setEnabled(bool(data and data.reduced and data.x_kind != "category"))
        for act in (self.act_export_image, self.act_copy_image):
            act.setEnabled(self.plot.has_plots())

    # ----------------------------------------------------------------- dataset
    def set_dataset(self, info: DatasetInfo, cfg: ProcessingConfig | None = None) -> list[str]:
        """Show *info*; optionally apply a configuration. Returns warnings."""
        self.info = info
        self._overview = None
        self._loaded_x = None
        self.plot.clear()
        self.plot.clear_y_limits()
        self.scaling_panel.set_scaling({})  # values of another file are used as they are
        self._scaling, self._x_scaling, self._next_view = {}, None, None
        self._table_generation += 1  # drop rows still loading for the previous file
        self.data_panel.set_message("Loading…")
        self._table_stale = True
        details = f"{info.num_rows:,} rows · {len(info.columns)} columns · {format_bytes(info.size_bytes)}"
        self.file_label.setText(f"<b>{info.path.name}</b><br><span style='color:gray'>{details}</span>")
        self.file_label.setToolTip(str(info.path))
        x = info.guess_x()
        base = ProcessingConfig(x=x, y=info.default_y(x))
        base.csv, base.output = self.cfg.csv, self.cfg.output  # keep export preferences between files
        self.cfg = base
        warnings: list[str] = []
        self.params.set_dataset(info, base.x, base.y)
        self._configure_x()
        self._refresh_scaling_rows()
        if cfg is not None:
            warnings = self.apply_config(cfg, reload=False)
        self.update_plot()
        return warnings

    def apply_config(self, cfg: ProcessingConfig, reload: bool = True) -> list[str]:
        """Apply a saved configuration to the open file. Returns warnings."""
        assert self.info is not None
        warnings = []
        if not self.info.has_column(cfg.x):
            raise ProcessingError(f"X column {cfg.x!r} of the configuration is not in {self.info.path.name}")
        missing = [y for y in cfg.y if not self.info.has_column(y) or not self.info.column(y).plottable]
        if missing:
            warnings.append(f"Parameters not found in this file: {', '.join(missing)}")
        self.cfg = cfg.copy()
        self.cfg.y = [y for y in cfg.y if y not in missing]
        self.params.set_x(cfg.x)
        self._configure_x()
        self.params.set_selected_y(self.cfg.y)
        self.scaling_panel.set_scaling(self.cfg.scaling)
        self._scaling = self.scaling_panel.scaling()
        self._refresh_scaling_rows()
        self.plot.clear_y_limits()
        self._apply_x_scaling(self.cfg.scaling_for(cfg.x))
        self.aggregation.set_config(cfg.aggregation)
        self.ranges_panel.set_mode(cfg.range_mode)
        self.plot.clear_ranges()
        self._pending = self.cfg.copy() if cfg.ranges else None
        self._update_timer.stop()
        if reload:
            self.update_plot()
        return warnings

    def _configure_x(self) -> None:
        info = self.info
        x = self.params.x()
        col = info.column(x)
        kind = info.x_kind(x)
        self.aggregation.set_x_kind(kind, col.kind == "duration")
        self.ranges_panel.set_x_kind(kind, col.time_zone)
        self.plot.set_allow_ranges(kind != "category")
        self.update_actions()

    def _x_changed(self, x: str) -> None:
        if self.info is None:
            return
        old_kind = self.info.x_kind(self._loaded_x) if self._loaded_x else None
        self._configure_x()
        self._refresh_scaling_rows()
        # Ranges kept below are absolute times; the new column's scaling defines the units from now on.
        self._x_scaling = self.scaling_panel.scaling().get(x)
        self._next_view = None
        if old_kind != self.info.x_kind(x) or old_kind != "datetime":
            if self.plot.ranges():
                self.status.emit("Ranges cleared because the x axis changed")
            self.plot.clear_ranges()
            # The old data is in different x units; don't let ranges be drawn on it.
            self._overview = self.plot_data = None
            self.plot.set_data(None, keep_view=False, message="Loading…")
            self._update_detail_banner()
        self.schedule_update()

    def _y_changed(self) -> None:
        self._refresh_scaling_rows()
        self.schedule_update()

    # ---------------------------------------------------------------- scaling
    def _refresh_scaling_rows(self) -> None:
        if self.info is None:
            return
        self.scaling_panel.set_columns(self.info, self.params.x(), self.params.selected_y())
        self._update_scaling_tab()

    def _update_scaling_tab(self) -> None:
        count = self.scaling_panel.scaled_count()
        index = self.bottom_tabs.indexOf(self.scaling_panel)
        self.bottom_tabs.setTabText(index, f"Scaling ({count})" if count else "Scaling")

    def _scaling_changed(self) -> None:
        scaling = self.scaling_panel.scaling()
        changed = {c for c in {*scaling, *self._scaling} if scaling.get(c) != self._scaling.get(c)}
        self._scaling = scaling
        self._update_scaling_tab()
        x = self.params.x()
        if changed - {x}:
            # Fixed y ranges of plots whose values changed no longer fit them.
            self.plot.clear_y_limits([*(changed - {x}), None])
        self._apply_x_scaling(scaling.get(x))
        self.schedule_update()

    def _apply_x_scaling(self, new: Scaling | None) -> None:
        """Move the ranges and the view into the x units of a new x scaling.

        The plot is cleared until data in the new units arrives, so that
        nothing is drawn or selected in a mix of old and new units.
        """
        old = self._x_scaling
        if new == old:
            return
        self._x_scaling = new

        def convert(value: float) -> float:
            raw = old.invert(value) if old else value
            return new.apply(raw) if new else raw

        view = self.plot.view_range() or self._next_view
        ranges = self.plot.ranges()
        if ranges:
            self.plot.set_ranges([(convert(e.lo), convert(e.hi), e.label, e.color) for e in ranges])
        self._next_view = tuple(sorted(convert(v) for v in view)) if view else None
        self._overview = self.plot_data = None
        waiting = "Loading…" if self.auto_update.isChecked() else "Press Update plot to show the new x values"
        self.plot.set_data(None, keep_view=False, message=waiting)
        self._update_detail_banner()

    # ------------------------------------------------------------------- plot
    def current_config(self) -> ProcessingConfig:
        """The configuration shown in the UI, including ranges."""
        cfg = self.cfg.copy()
        cfg.x = self.params.x()
        cfg.y = self.params.selected_y()
        cfg.scaling = self.scaling_panel.scaling()
        cfg.aggregation = self.aggregation.config()
        cfg.range_mode = self.ranges_panel.mode()
        cfg.ranges = self.config_ranges(cfg.range_mode)
        if self.info is not None:
            cfg.source = str(self.info.path)
        return cfg

    def config_ranges(self, mode: str) -> list[TimeRange]:
        if self.info is None:
            return []
        x = self.params.x()
        kind = self.info.x_kind(x)
        if kind == "category":
            return []
        tz = self.info.column(x).time_zone
        start = self._data_start(x) if mode == "relative" else None
        out = []
        for entry in self.plot.ranges():
            if mode == "relative" and start is not None:
                out.append(TimeRange(round(entry.lo - start, 6), round(entry.hi - start, 6), entry.label, entry.color))
            elif kind == "datetime":
                out.append(
                    TimeRange(
                        format_x(plot_to_x(entry.lo, kind, tz), kind),
                        format_x(plot_to_x(entry.hi, kind, tz), kind),
                        entry.label,
                        entry.color,
                    )
                )
            else:
                out.append(TimeRange(entry.lo, entry.hi, entry.label, entry.color))
        return out

    def _data_start(self, x: str) -> float | None:
        """First x value in plot units (from the plot if it shows *x*)."""
        data = self.plot.data
        if data is not None and data.x_name == x and data.bounds:
            return data.bounds[0]
        bounds = data_bounds(self.info, x, self.scaling_panel.scaling().get(x))
        return x_to_plot(bounds.lo, self.info.x_kind(x)) if bounds else None

    def schedule_update(self) -> None:
        if self.info is None:
            return
        if self.auto_update.isChecked():
            self._update_timer.start()
        else:
            self.update_button.setStyleSheet("font-weight: bold;")

    def update_plot(self) -> None:
        if self.info is None:
            return
        self._update_timer.stop()
        self.update_button.setStyleSheet("")
        if not self.aggregation.is_valid():
            self.status.emit("Fix the aggregation interval to update the plot")
            return
        cfg = self.current_config()
        cfg.ranges = []
        self._generation += 1
        generation = self._generation
        pending = self._pending
        self._pending = None
        self.busy.emit(True, "Loading plot data…")
        info = self.info
        workers.submit(
            lambda: _plot_job(info, cfg, pending),
            on_done=lambda result: self._plot_loaded(generation, cfg, result),
            on_error=lambda exc: self._plot_failed(generation, exc),
        )

    def _plot_loaded(self, generation: int, cfg: ProcessingConfig, result) -> None:
        if generation != self._generation:
            return
        data, spans = result
        self.busy.emit(False, "")
        x_changed = self._loaded_x != cfg.x
        self._loaded_x = cfg.x
        self._overview = data
        self.plot_data = data
        self.plot.set_data(data, keep_view=not x_changed)
        view, self._next_view = self._next_view, None
        if view is not None and not x_changed:
            self.plot.set_x_view(*view, padding=0)
        if spans is not None:
            self.plot.set_ranges(spans)
        self._update_detail_banner()
        self._schedule_table()
        rows = f"{data.rows:,}"
        if not data.series:
            self.aggregation.set_estimate("")
            self.status.emit("Select parameters to plot")
        elif data.reduced:
            self.aggregation.set_estimate(f"≈ {rows} rows after aggregation")
            self.status.emit(f"{len(data.series)} series · ≈ {rows} rows (display reduced to a min/max envelope)")
        else:
            self.aggregation.set_estimate(f"{rows} rows after aggregation")
            self.status.emit(f"{len(data.series)} series · {rows} rows")

    def _plot_failed(self, generation: int, exc: BaseException) -> None:
        if generation != self._generation:
            return
        self.busy.emit(False, "")
        self.error.emit("Could not load plot data", str(exc))

    def _update_detail_banner(self, detail_view=None) -> None:
        data = self._overview
        reduced = bool(data and data.reduced)
        self.update_actions()
        if not reduced:
            self.detail_banner.hide()
            return
        if data.x_kind == "category":
            text = (
                f"This view shows the lowest and highest values of {data.rows:,} rows per category; "
                "exports always use the full data. Aggregate per x value to see fewer points."
            )
        elif detail_view:
            lo, hi = detail_view
            text = (
                f"Full detail loaded for {format_plot_x(lo, data.x_kind, False)} – "
                f"{format_plot_x(hi, data.x_kind, False)}. <a href='#'>Load detail for current view</a> (D)"
            )
        else:
            text = (
                f"This view shows a min/max envelope of {data.rows:,} rows so that peaks stay visible; "
                "exports always use the full data. Zoom in and <a href='#'>load full detail for the "
                "current view</a> (D)."
            )
        self.detail_banner.setText(text)
        self.detail_banner.show()

    def load_detail(self) -> None:
        view = self.plot.view_range()
        if self.info is None or view is None or self._overview is None:
            return
        cfg = self.current_config()
        cfg.ranges = []
        self._generation += 1
        generation = self._generation
        self.busy.emit(True, "Loading detail…")
        info = self.info
        workers.submit(
            lambda: _plot_job(info, cfg, None, view=view),
            on_done=lambda result: self._detail_loaded(generation, result[0], view),
            on_error=lambda exc: self._plot_failed(generation, exc),
        )

    def _detail_loaded(self, generation: int, detail: PlotData, view) -> None:
        if generation != self._generation or self._overview is None:
            return
        self.busy.emit(False, "")
        merged = merge_plot_data(self._overview, detail)
        self.plot_data = merged
        self.plot.set_data(merged, keep_view=True)
        self._update_detail_banner(view)
        self.status.emit(f"Loaded {detail.points:,} points for the visible area")

    # ------------------------------------------------------------------ table
    def _table_shown(self) -> bool:
        return self.bottom_tabs.currentWidget() is self.data_panel and self.data_panel.isVisible()

    def _bottom_tab_changed(self, index: int) -> None:
        self.settings.setValue("explorer/bottom_tab", index)
        if self._table_stale and self._table_shown():
            self.update_table()

    def _schedule_table(self) -> None:
        """Reload the table soon if it is shown, otherwise when it is shown next."""
        self._table_stale = True
        if self.info is not None and self._table_shown():
            self._table_timer.start()

    def update_table(self) -> None:
        self._table_timer.stop()
        if self.info is None or not self.aggregation.is_valid():
            return
        cfg = self.current_config()
        cfg.ranges = []
        data = self.plot.data
        # Rows of the visible x window, when the plot shows the current x axis.
        follow = data is not None and data.series and data.x_name == cfg.x and data.x_kind != "category"
        view = self.plot.view_range() if follow else None
        self._table_stale = False
        self._table_generation += 1
        generation = self._table_generation
        info = self.info
        workers.submit(
            lambda: load_table_data(info, cfg, view=view),
            on_done=lambda table: self._table_loaded(generation, table),
            on_error=lambda exc: self._table_failed(generation, exc),
        )

    def _table_loaded(self, generation: int, table: TableData) -> None:
        if generation == self._table_generation:
            self.data_panel.set_table(table)

    def _table_failed(self, generation: int, exc: BaseException) -> None:
        if generation == self._table_generation:
            self.data_panel.set_message(f"Could not load the rows: {exc}")

    # ----------------------------------------------------------------- ranges
    def _ranges_changed(self) -> None:
        ranges = self.plot.ranges()
        self.ranges_panel.refresh(ranges, self.plot.default_range_color())
        self.bottom_tabs.setTabText(0, f"Ranges ({len(ranges)})" if ranges else "Ranges")

    def _range_edited(self, index: int, lo, hi, label) -> None:
        if index >= len(self.plot.ranges()):
            return
        if lo is None and hi is None and label is None:
            self.ranges_panel.refresh(self.plot.ranges(), self.plot.default_range_color())
            self.status.emit("Invalid value - use an ISO date/time such as 2024-01-31 12:00:00 or a number")
            return
        self.plot.update_range(index, lo, hi, label)

    # ------------------------------------------------------------ plot output
    def open_plot_settings(self) -> None:
        if self.info is None:
            return
        data = self.plot.data
        if data is not None and data.series:
            x, columns = data.x_name, [s.column for s in data.series]
        else:
            x, columns = self.params.x(), self.params.selected_y()
        dialog = PlotSettingsDialog(self.plot, x, columns, parent=self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.applied.connect(self._plot_style_applied)
        dialog.open()

    def _plot_style_applied(self) -> None:
        save_style(self.settings, self.plot.style)
        self._ranges_changed()  # range colours may follow a new default

    def export_image(self) -> None:
        if self.info is None or not self.plot.has_plots():
            return
        dialog = ImageExportDialog(self.plot, self.settings, self.info.path, parent=self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.saved.connect(lambda path: self.status.emit(f"Saved the plot to {path}"))
        dialog.open()

    def copy_image(self) -> None:
        if self.plot.has_plots():
            self.plot.copy_image()
            self.status.emit("Copied the plot to the clipboard")

    # ------------------------------------------------------------------ misc
    def remember_export_settings(self, cfg: ProcessingConfig) -> None:
        """Keep CSV/output preferences chosen in the export dialog."""
        self.cfg.csv = cfg.csv
        self.cfg.output = cfg.output
        self.cfg.sort = cfg.sort

    @property
    def path(self) -> Path | None:
        return self.info.path if self.info else None
