"""The explorer page: parameter selection, aggregation, plot and ranges."""

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
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..config import ProcessingConfig, TimeRange
from ..dataset import DatasetInfo, format_bytes
from ..processing import (
    PlotData,
    ProcessingError,
    data_bounds,
    format_x,
    load_plot_data,
    merge_plot_data,
    plot_to_x,
    resolve_ranges,
    x_to_plot,
)
from . import workers
from .panels import AggregationPanel, ParameterPanel, RangePanel
from .plot_area import PlotArea, format_plot_x


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
                )
            )
    return data, spans


class ExplorerPage(QWidget):
    """Explore one Parquet file."""

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

        # -- widgets -----------------------------------------------------------
        self.file_label = QLabel()
        self.file_label.setTextFormat(Qt.TextFormat.RichText)
        self.file_label.setWordWrap(True)
        self.params = ParameterPanel()
        self.params.xChanged.connect(self._x_changed)
        self.params.yChanged.connect(self.schedule_update)
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

        self.plot = PlotArea()
        self.plot.set_stacked(settings.value("plot/stacked", True, type=bool))
        self.plot.set_show_points(settings.value("plot/points", False, type=bool))
        self.plot.rangesChanged.connect(self._ranges_changed)
        self.ranges_panel = RangePanel()
        self.ranges_panel.addRequested.connect(self.plot.add_range_from_view)
        self.ranges_panel.removeRequested.connect(self.plot.remove_range)
        self.ranges_panel.clearRequested.connect(self.plot.clear_ranges)
        self.ranges_panel.zoomRequested.connect(self.plot.zoom_to_range)
        self.ranges_panel.zoomAllRequested.connect(self.plot.zoom_to_ranges)
        self.ranges_panel.editRequested.connect(self._range_edited)

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
            None,
            self.act_load_config,
            self.act_save_config,
            self.act_export,
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
        right.addWidget(plot_box)
        right.addWidget(self.ranges_panel)
        right.setStretchFactor(0, 4)
        right.setStretchFactor(1, 1)
        right.setSizes([600, 170])
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

        self.act_open = action("Open…", "Ctrl+O", "Choose another Parquet file")
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

    def _set_stacked(self, on: bool) -> None:
        self.settings.setValue("plot/stacked", on)
        self.plot.set_stacked(on)

    def _set_show_points(self, on: bool) -> None:
        self.settings.setValue("plot/points", on)
        self.plot.set_show_points(on)

    # ----------------------------------------------------------------- dataset
    def set_dataset(self, info: DatasetInfo, cfg: ProcessingConfig | None = None) -> list[str]:
        """Show *info*; optionally apply a configuration. Returns warnings."""
        self.info = info
        self._overview = None
        self._loaded_x = None
        self.plot.clear()
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

    def _x_changed(self, x: str) -> None:
        if self.info is None:
            return
        old_kind = self.info.x_kind(self._loaded_x) if self._loaded_x else None
        self._configure_x()
        if old_kind != self.info.x_kind(x) or old_kind != "datetime":
            if self.plot.ranges():
                self.status.emit("Ranges cleared because the x axis changed")
            self.plot.clear_ranges()
            # The old data is in different x units; don't let ranges be drawn on it.
            self._overview = self.plot_data = None
            self.plot.set_data(None, keep_view=False, message="Loading…")
            self._update_detail_banner()
        self.schedule_update()

    # ------------------------------------------------------------------- plot
    def current_config(self) -> ProcessingConfig:
        """The configuration shown in the UI, including ranges."""
        cfg = self.cfg.copy()
        cfg.x = self.params.x()
        cfg.y = self.params.selected_y()
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
        tz = self.info.column(x).time_zone
        start = self._data_start(x) if mode == "relative" else None
        out = []
        for entry in self.plot.ranges():
            if mode == "relative" and start is not None:
                out.append(TimeRange(round(entry.lo - start, 6), round(entry.hi - start, 6), entry.label))
            elif kind == "datetime":
                out.append(
                    TimeRange(
                        format_x(plot_to_x(entry.lo, kind, tz), kind),
                        format_x(plot_to_x(entry.hi, kind, tz), kind),
                        entry.label,
                    )
                )
            else:
                out.append(TimeRange(entry.lo, entry.hi, entry.label))
        return out

    def _data_start(self, x: str) -> float | None:
        """First x value in plot units (from the plot if it shows *x*)."""
        data = self.plot.data
        if data is not None and data.x_name == x and data.bounds:
            return data.bounds[0]
        bounds = data_bounds(self.info, x)
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
        if spans is not None:
            self.plot.set_ranges(spans)
        self._update_detail_banner()
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
        self.act_detail.setEnabled(reduced)
        if not reduced:
            self.detail_banner.hide()
            return
        if detail_view:
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

    # ----------------------------------------------------------------- ranges
    def _ranges_changed(self) -> None:
        self.ranges_panel.refresh(self.plot.ranges())

    def _range_edited(self, index: int, lo, hi, label) -> None:
        if index >= len(self.plot.ranges()):
            return
        if lo is None and hi is None and label is None:
            self.ranges_panel.refresh(self.plot.ranges())
            self.status.emit("Invalid value - use an ISO date/time such as 2024-01-31 12:00:00 or a number")
            return
        self.plot.update_range(index, lo, hi, label)

    # ------------------------------------------------------------------ misc
    def remember_export_settings(self, cfg: ProcessingConfig) -> None:
        """Keep CSV/output preferences chosen in the export dialog."""
        self.cfg.csv = cfg.csv
        self.cfg.output = cfg.output
        self.cfg.sort = cfg.sort

    @property
    def path(self) -> Path | None:
        return self.info.path if self.info else None
