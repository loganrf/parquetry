"""Plot settings dialog: appearance, colours, labels and axis ranges of the plots."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import parse_number
from ..dataset import ROW_INDEX, ROW_INDEX_LABEL
from ..processing import ProcessingError, parse_x_value, x_to_plot
from .plot_area import PlotArea, format_plot_x
from .theme import THEMES, PlotStyle, theme_named
from .widgets import ColorButton


def _muted(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(placeholder-text);")
    return label


class PlotSettingsDialog(QDialog):
    """Edit the :class:`PlotStyle` of a plot area, its x range and the y ranges of its plots."""

    applied = Signal()

    def __init__(self, area: PlotArea, x: str, columns: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Plot settings")
        self.area = area
        self.x = x
        self.columns = [c for c in dict.fromkeys(columns) if c != x]
        style = area.style
        self.tabs = QTabWidget()
        self.tabs.addTab(self._general_tab(style), "General")
        self.tabs.addTab(self._series_tab(style), "Series")
        self.tabs.addTab(self._axes_tab(style), "Axes")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)
        defaults = buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults)
        defaults.setToolTip("Default appearance, colours and labels; automatic y ranges")
        defaults.clicked.connect(self.restore_defaults)
        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(buttons)
        self.resize(560, 520)
        self._theme_changed()

    # ------------------------------------------------------------------ tabs
    def _general_tab(self, style: PlotStyle) -> QWidget:
        self.title_edit = QLineEdit(style.title)
        self.title_edit.setPlaceholderText("No title")
        self.title_edit.setToolTip("Shown above the plots and in exported images")
        self.theme_combo = QComboBox()
        for key, label in THEMES.items():
            self.theme_combo.addItem(label, key)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(style.theme)))
        self.theme_combo.setToolTip("Light colours suit printed reports, whatever the application looks like")
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        self.line_spin = QDoubleSpinBox()
        self.line_spin.setRange(0.5, 8.0)
        self.line_spin.setSingleStep(0.5)
        self.line_spin.setDecimals(1)
        self.line_spin.setSuffix(" px")
        self.line_spin.setValue(style.line_width)
        self.line_spin.setToolTip("Lines wider than 1 px draw more slowly with millions of points")
        self.point_spin = QSpinBox()
        self.point_spin.setRange(1, 20)
        self.point_spin.setSuffix(" px")
        self.point_spin.setValue(style.point_size)
        self.point_spin.setToolTip("Size of the point markers (View ▸ Points)")
        self.grid_spin = QSpinBox()
        self.grid_spin.setRange(0, 100)
        self.grid_spin.setSingleStep(5)
        self.grid_spin.setSuffix(" %")
        self.grid_spin.setSpecialValueText("Off")
        self.grid_spin.setValue(style.grid)
        self.grid_spin.setToolTip("Opacity of the grid lines")
        self.legend_check = QCheckBox("Show a legend in plots with several series")
        self.legend_check.setChecked(style.legend)
        self.range_color = ColorButton(style.range_color, title="Range highlight colour")
        self.range_opacity = QSpinBox()
        self.range_opacity.setRange(5, 100)
        self.range_opacity.setSingleStep(5)
        self.range_opacity.setSuffix(" % opacity")
        self.range_opacity.setValue(style.range_opacity)
        range_row = QHBoxLayout()
        range_row.addWidget(self.range_color)
        range_row.addWidget(self.range_opacity)
        range_row.addStretch(1)

        widget = QWidget()
        form = QFormLayout(widget)
        form.addRow("Title", self.title_edit)
        form.addRow("Colours", self.theme_combo)
        form.addRow("Line width", self.line_spin)
        form.addRow("Point size", self.point_spin)
        form.addRow("Grid", self.grid_spin)
        form.addRow("", self.legend_check)
        form.addRow("Ranges", range_row)
        form.addRow(_muted("Ranges without a colour of their own use this one; set a range's colour in the "
                           "Ranges table or by right-clicking it on the plot."))
        return widget

    def _series_tab(self, style: PlotStyle) -> QWidget:
        rows = [self.x, *self.columns]
        self.series_table = QTableWidget(len(rows), 3)
        self.series_table.setHorizontalHeaderLabels(["Column", "Colour", "Label"])
        self.series_table.verticalHeader().hide()
        self.series_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.series_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        #: (column, colour button or None for the x axis, label editor)
        self._series: list[tuple[str, ColorButton | None, QLineEdit]] = []
        for row, column in enumerate(rows):
            default = ROW_INDEX_LABEL if column == ROW_INDEX else column
            name = QTableWidgetItem(f"{default}  (x)" if column == self.x else default)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name.setToolTip(column)
            self.series_table.setItem(row, 0, name)
            button = None
            if column != self.x:
                button = ColorButton(style.colors.get(column, ""), title=f"Colour of {column}")
                button.setAutoRaise(True)
                self.series_table.setCellWidget(row, 1, button)
            else:
                axis = QTableWidgetItem("–")
                axis.setFlags(axis.flags() & ~Qt.ItemFlag.ItemIsEditable)
                axis.setToolTip("The x axis has no line colour")
                self.series_table.setItem(row, 1, axis)
            edit = QLineEdit(style.labels.get(column, ""))
            edit.setPlaceholderText(default)
            edit.setFrame(False)
            edit.setToolTip("Axis and legend text (empty: the column name)")
            self.series_table.setCellWidget(row, 2, edit)
            self._series.append((column, button, edit))
        reset = QPushButton("Automatic colours")
        reset.setToolTip("Give every parameter its palette colour again")
        reset.clicked.connect(lambda: [b.set_color("") for _, b, _ in self._series if b is not None])
        buttons = QHBoxLayout()
        buttons.addWidget(_muted("Colours and labels are remembered by column name, also for other files."), 1)
        buttons.addWidget(reset)
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(self.series_table, 1)
        layout.addLayout(buttons)
        return widget

    def _axes_tab(self, style: PlotStyle) -> QWidget:
        area = self.area
        self._kind = area.kind
        view = area.view_range()
        self.x_min = QLineEdit()
        self.x_max = QLineEdit()
        example = "2024-01-31 12:00:00" if self._kind == "datetime" else "a number"
        for edit in (self.x_min, self.x_max):
            edit.setToolTip(f"Shown x range: {example}" + (" (UTC)" if area.data and area.data.x_tz else ""))
        self._x_text = ("", "")
        if view is not None:
            self._x_text = (self._format_x(view[0]), self._format_x(view[1]))
            self.x_min.setText(self._x_text[0])
            self.x_max.setText(self._x_text[1])
        fit = QPushButton("All data")
        fit.setToolTip("Fill in the whole x range of the data")
        fit.clicked.connect(self._fit_x)
        fit.setEnabled(bool(area.data and area.data.bounds))
        x_row = QHBoxLayout()
        x_row.addWidget(self.x_min, 1)
        x_row.addWidget(QLabel("to"))
        x_row.addWidget(self.x_max, 1)
        x_row.addWidget(fit)
        x_box = QGroupBox("X axis")
        x_form = QFormLayout(x_box)
        x_form.addRow("Range", x_row)
        x_box.setEnabled(view is not None)

        self.overlay_edit = QLineEdit(style.overlay_label)
        self.overlay_edit.setPlaceholderText("none")
        self.overlay_edit.setToolTip("Y axis label of the overlay plot (Stacked off), where parameters share one axis")
        y_box = QGroupBox("Y axes")
        y_layout = QVBoxLayout(y_box)
        y_form = QFormLayout()
        y_form.addRow("Overlay label", self.overlay_edit)
        y_layout.addLayout(y_form)
        axes = area.y_axes()
        limits = area.y_limits()
        self.y_table = QTableWidget(len(axes), 4)
        self.y_table.setHorizontalHeaderLabels(["Plot", "Automatic", "Minimum", "Maximum"])
        self.y_table.verticalHeader().hide()
        self.y_table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        header = self.y_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        #: (plot key, automatic checkbox, minimum, maximum)
        self._y_rows: list[tuple[str | None, QCheckBox, QLineEdit, QLineEdit]] = []
        for row, (key, title, (lo, hi)) in enumerate(axes):
            name = QTableWidgetItem(title)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.y_table.setItem(row, 0, name)
            automatic = QCheckBox()
            automatic.setChecked(key not in limits)
            automatic.setToolTip("Follow the data (Auto Y) instead of a fixed range")
            cell = QWidget()
            cell_layout = QHBoxLayout(cell)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell_layout.addWidget(automatic)
            self.y_table.setCellWidget(row, 1, cell)
            edits = []
            for col, value in ((2, lo), (3, hi)):
                edit = QLineEdit(f"{value:.6g}")
                edit.setFrame(False)
                edit.textEdited.connect(lambda _text, box=automatic: box.setChecked(False))
                self.y_table.setCellWidget(row, col, edit)
                edits.append(edit)
            self._y_rows.append((key, automatic, *edits))
        y_layout.addWidget(self.y_table, 1)
        if not axes:
            y_layout.addWidget(_muted("No plot with a numeric y axis is shown."))
        y_layout.addWidget(_muted("Fixed ranges stay while you zoom along x; Reset zoom (Ctrl+0) returns every "
                                  "plot to automatic y ranges."))

        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(x_box)
        layout.addWidget(y_box, 1)
        return widget

    # ------------------------------------------------------------- reactions
    def _theme_changed(self) -> None:
        theme = theme_named(self.theme_combo.currentData())
        self.range_color.set_automatic(theme.region)
        for column, button, _ in self._series:
            if button is not None:
                button.set_automatic(self.area.auto_color_for(column, theme))

    def _format_x(self, value: float) -> str:
        return format_plot_x(value, self._kind) if self._kind == "datetime" else f"{value:.10g}"

    def _parse_x(self, text: str) -> float:
        if self._kind == "datetime":
            return x_to_plot(parse_x_value(text, "datetime", None), "datetime")
        return parse_number(text)

    def _fit_x(self) -> None:
        data = self.area.data
        if data is None or data.bounds is None:
            return
        lo, hi = data.bounds
        if data.x_categories:
            lo, hi = -0.5, len(data.x_categories) - 0.5
        self.x_min.setText(self._format_x(lo))
        self.x_max.setText(self._format_x(hi))

    def restore_defaults(self) -> None:
        default = PlotStyle()
        self.title_edit.clear()
        self.theme_combo.setCurrentIndex(self.theme_combo.findData(default.theme))
        self.line_spin.setValue(default.line_width)
        self.point_spin.setValue(default.point_size)
        self.grid_spin.setValue(default.grid)
        self.legend_check.setChecked(default.legend)
        self.range_color.set_color(default.range_color)
        self.range_opacity.setValue(default.range_opacity)
        for _, button, edit in self._series:
            if button is not None:
                button.set_color("")
            edit.clear()
        self.overlay_edit.clear()
        for _, automatic, _, _ in self._y_rows:
            automatic.setChecked(True)

    # ----------------------------------------------------------------- apply
    def style(self) -> PlotStyle:
        """The plot style described by the dialog."""
        style = self.area.style.copy()
        style.title = self.title_edit.text().strip()
        style.theme = self.theme_combo.currentData()
        style.line_width = self.line_spin.value()
        style.point_size = self.point_spin.value()
        style.grid = self.grid_spin.value()
        style.legend = self.legend_check.isChecked()
        style.range_color = self.range_color.color()
        style.range_opacity = self.range_opacity.value()
        style.overlay_label = self.overlay_edit.text().strip()
        for column, button, edit in self._series:
            if button is not None:
                if button.color():
                    style.colors[column] = button.color()
                else:
                    style.colors.pop(column, None)
            if edit.text().strip():
                style.labels[column] = edit.text().strip()
            else:
                style.labels.pop(column, None)
        return style

    def x_view(self) -> tuple[float, float] | None:
        """The x range to show, or None when it was not changed. Raises ValueError."""
        text = (self.x_min.text().strip(), self.x_max.text().strip())
        if text == self._x_text or not all(text):
            return None
        try:
            lo, hi = (self._parse_x(t) for t in text)
        except (ValueError, ProcessingError) as exc:
            raise ValueError(f"X axis range: {exc}") from None
        if hi <= lo:
            raise ValueError("X axis range: the maximum must be larger than the minimum")
        return lo, hi

    def y_limits(self) -> dict[str | None, tuple[float, float] | None]:
        """Fixed y range (or None for automatic) by plot key. Raises ValueError."""
        out: dict[str | None, tuple[float, float] | None] = {}
        for row, (key, automatic, lo_edit, hi_edit) in enumerate(self._y_rows):
            if automatic.isChecked():
                out[key] = None
                continue
            title = self.y_table.item(row, 0).text()
            try:
                lo, hi = parse_number(lo_edit.text()), parse_number(hi_edit.text())
            except ValueError as exc:
                raise ValueError(f"Y range of {title}: {exc}") from None
            if hi <= lo:
                raise ValueError(f"Y range of {title}: the maximum must be larger than the minimum")
            out[key] = (lo, hi)
        return out

    def apply(self) -> bool:
        try:
            x_view = self.x_view()
            y_limits = self.y_limits()
        except ValueError as exc:
            QMessageBox.warning(self, "Plot settings", str(exc))
            return False
        self.area.set_style(self.style())
        if x_view is not None:
            self.area.set_x_view(*x_view, padding=0)
            self._x_text = (self.x_min.text().strip(), self.x_max.text().strip())
        self.area.set_y_limits(y_limits)
        self.applied.emit()
        return True

    def _ok(self) -> None:
        if self.apply():
            self.accept()
