"""Side panels of the explorer: parameters, aggregation and ranges."""

from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from ..config import AGG_FUNCTIONS, AGG_METHOD_LABELS, AGG_METHODS, AggregationConfig
from ..dataset import ROW_INDEX, ROW_INDEX_LABEL, DatasetInfo
from ..durations import format_seconds, is_duration
from ..processing import ProcessingError, parse_x_value, x_to_plot
from .plot_area import RangeEntry, format_plot_x

_KIND_LABEL = {
    "datetime": "time", "date": "date", "numeric": "number", "duration": "duration", "boolean": "bool", "text": "text",
}
#: Orders of the parameter list, with their labels.
PARAMETER_SORTS = {"file": "File order", "name": "Name", "type": "Type"}
_NUMBER = re.compile(r"(\d+)")


def natural_key(text: str) -> list:
    """Sort key that ignores case and orders numbers by value (ch2 before ch10)."""
    return [int(part) if part.isdigit() else part.casefold() for part in _NUMBER.split(text)]


class ParameterPanel(QGroupBox):
    """Choice of the x column and the y parameters."""

    xChanged = Signal(str)
    yChanged = Signal()
    sortChanged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Parameters", parent)
        self.x_combo = QComboBox()
        self.x_combo.setToolTip("Column used for the horizontal axis")
        self.x_combo.currentIndexChanged.connect(self._x_changed)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter parameters…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Parameter", "Type"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(False)
        self.tree.itemChanged.connect(self._item_changed)
        self.tree.itemDoubleClicked.connect(self._only_this)
        self.tree.setToolTip("Tick parameters to plot and export. Double-click to show only that parameter.")
        header = self.tree.header()
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._header_clicked)
        self.sort_combo = QComboBox()
        for key, label in PARAMETER_SORTS.items():
            self.sort_combo.addItem(label, key)
        self.sort_combo.setToolTip(
            "Order of the parameter list; clicking a column header also sorts by it.\n"
            "Plots and exports keep the order in which parameters were ticked."
        )
        self.sort_combo.currentIndexChanged.connect(self._sort_changed)
        self.reverse_button = QToolButton()
        self.reverse_button.setCheckable(True)
        self.reverse_button.setToolTip("Reverse the order")
        self.reverse_button.toggled.connect(self._sort_changed)
        self.count_label = QLabel()
        select_all = QToolButton(text="All")
        select_all.setToolTip("Tick all visible parameters")
        select_all.clicked.connect(lambda: self._set_visible_checked(True))
        select_none = QToolButton(text="None")
        select_none.setToolTip("Untick all visible parameters")
        select_none.clicked.connect(lambda: self._set_visible_checked(False))

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.addRow("X axis", self.x_combo)
        buttons = QHBoxLayout()
        buttons.addWidget(self.count_label, 1)
        buttons.addWidget(select_all)
        buttons.addWidget(select_none)
        title = QHBoxLayout()
        title.addWidget(QLabel("Y parameters"), 1)
        title.addWidget(QLabel("Sort"))
        title.addWidget(self.sort_combo)
        title.addWidget(self.reverse_button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(title)
        layout.addWidget(self.filter_edit)
        layout.addWidget(self.tree, 1)
        layout.addLayout(buttons)
        self._updating = False
        self._order: list[str] = []
        self._file_order: list[str] = []
        self._sort_items()

    def set_dataset(self, info: DatasetInfo, x: str, y: list[str]) -> None:
        self._updating = True
        try:
            self.x_combo.clear()
            self.x_combo.addItem(ROW_INDEX_LABEL, ROW_INDEX)
            for col in info.x_candidates:
                self.x_combo.addItem(f"{col.name}  ({_KIND_LABEL.get(col.kind, col.kind)})", col.name)
            self.x_combo.setCurrentIndex(max(0, self.x_combo.findData(x)))
            self.tree.clear()
            for col in info.y_candidates:
                item = QTreeWidgetItem([col.name, col.dtype_name])
                item.setData(0, Qt.ItemDataRole.UserRole, col.name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Checked if col.name in y else Qt.CheckState.Unchecked)
                item.setToolTip(0, col.name)
                self.tree.addTopLevelItem(item)
            self._file_order = [col.name for col in info.y_candidates]
            self._order = [c for c in y if c in self._file_order]
            self.filter_edit.clear()
        finally:
            self._updating = False
        self._sort_items()
        self._update_count()

    def x(self) -> str:
        return self.x_combo.currentData() or ROW_INDEX

    def set_x(self, x: str) -> None:
        index = self.x_combo.findData(x)
        if index >= 0:
            self.x_combo.setCurrentIndex(index)

    def selected_y(self) -> list[str]:
        """Checked parameters, in the order they were ticked."""
        checked = set()
        for item in self._items():
            if item.checkState(0) == Qt.CheckState.Checked:
                checked.add(item.data(0, Qt.ItemDataRole.UserRole))
        order = [c for c in self._order if c in checked]
        order += [c for c in self._file_order if c in checked and c not in order]
        return [c for c in order if c != self.x()]

    def set_selected_y(self, names: list[str]) -> None:
        self._updating = True
        try:
            wanted = set(names)
            for item in self._items():
                name = item.data(0, Qt.ItemDataRole.UserRole)
                item.setCheckState(0, Qt.CheckState.Checked if name in wanted else Qt.CheckState.Unchecked)
            self._order = list(names)
        finally:
            self._updating = False
        self._update_count()
        self.yChanged.emit()

    def _items(self) -> list[QTreeWidgetItem]:
        items = []
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            items.append(it.value())
            it += 1
        return items

    def sort_order(self) -> tuple[str, bool]:
        """The list's order (a key of :data:`PARAMETER_SORTS`) and whether it is reversed."""
        return self.sort_combo.currentData(), self.reverse_button.isChecked()

    def set_sort(self, key: str, reverse: bool = False) -> None:
        for widget in (self.sort_combo, self.reverse_button):
            widget.blockSignals(True)
        try:
            self.sort_combo.setCurrentIndex(max(0, self.sort_combo.findData(key)))
            self.reverse_button.setChecked(reverse)
        finally:
            for widget in (self.sort_combo, self.reverse_button):
                widget.blockSignals(False)
        self._sort_changed()

    def _header_clicked(self, section: int) -> None:
        key = "name" if section == 0 else "type"
        if self.sort_combo.currentData() == key:
            self.set_sort(key, not self.reverse_button.isChecked())
        else:
            self.set_sort(key)

    def _sort_changed(self, *_args) -> None:
        self._sort_items()
        self.sortChanged.emit()

    def _sort_items(self) -> None:
        key, reverse = self.sort_order()
        self.reverse_button.setArrowType(Qt.ArrowType.DownArrow if reverse else Qt.ArrowType.UpArrow)
        header = self.tree.header()
        header.setSortIndicatorShown(key != "file")
        order = Qt.SortOrder.DescendingOrder if reverse else Qt.SortOrder.AscendingOrder
        header.setSortIndicator(1 if key == "type" else 0, order)
        position = {name: i for i, name in enumerate(self._file_order)}

        def sort_key(item: QTreeWidgetItem):
            name = item.data(0, Qt.ItemDataRole.UserRole)
            if key == "name":
                return natural_key(name)
            if key == "type":
                return natural_key(item.text(1)), natural_key(name)
            return position.get(name, 0)

        items = [self.tree.takeTopLevelItem(0) for _ in range(self.tree.topLevelItemCount())]
        self.tree.addTopLevelItems(sorted(items, key=sort_key, reverse=reverse))
        self._apply_filter(self.filter_edit.text())  # hiding does not survive taking items out

    def _x_changed(self) -> None:
        if not self._updating:
            self._update_count()
            self.xChanged.emit(self.x())

    def _item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._updating or column != 0:
            return
        name = item.data(0, Qt.ItemDataRole.UserRole)
        if item.checkState(0) == Qt.CheckState.Checked:
            if name not in self._order:
                self._order.append(name)
        elif name in self._order:
            self._order.remove(name)
        self._update_count()
        self.yChanged.emit()

    def _only_this(self, item: QTreeWidgetItem) -> None:
        self.set_selected_y([item.data(0, Qt.ItemDataRole.UserRole)])

    def _set_visible_checked(self, checked: bool) -> None:
        self._updating = True
        try:
            for item in self._items():
                if not item.isHidden():
                    item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                    name = item.data(0, Qt.ItemDataRole.UserRole)
                    if checked and name not in self._order:
                        self._order.append(name)
                    elif not checked and name in self._order:
                        self._order.remove(name)
        finally:
            self._updating = False
        self._update_count()
        self.yChanged.emit()

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for item in self._items():
            item.setHidden(bool(needle) and needle not in item.text(0).lower())

    def _update_count(self) -> None:
        total = self.tree.topLevelItemCount()
        self.count_label.setText(f"{len(self.selected_y())} of {total} selected")


class AggregationPanel(QGroupBox):
    """Aggregation method, its parameter and the aggregation functions."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Aggregation", parent)
        self.method_combo = QComboBox()
        for method in AGG_METHODS:
            self.method_combo.addItem(AGG_METHOD_LABELS[method], method)
        self.method_combo.setToolTip(
            "Compact large files before plotting and exporting:\n"
            "• Every Nth row keeps one row out of N\n"
            "• Fixed interval buckets aggregates per time interval (e.g. 1 s, 1 min)\n"
            "• Target number of points splits the x range into N equal buckets\n"
            "• One bucket per x value aggregates rows with the same x (e.g. per text category)"
        )
        self.method_combo.currentIndexChanged.connect(self._method_changed)

        self.n_spin = QSpinBox()
        self.n_spin.setRange(2, 1_000_000_000)
        self.n_spin.setValue(10)
        self.n_spin.setPrefix("every ")
        self.n_spin.setSuffix(" rows")
        self.n_spin.valueChanged.connect(self.changed)
        self.every_edit = QLineEdit("1s")
        self.every_edit.textChanged.connect(self._every_changed)
        self.points_spin = QSpinBox()
        self.points_spin.setRange(1, 100_000_000)
        self.points_spin.setSingleStep(500)
        self.points_spin.setValue(2000)
        self.points_spin.setSuffix(" buckets")
        self.points_spin.valueChanged.connect(self.changed)
        self.param_stack = QStackedWidget()
        none_label = QLabel("All rows are used. Large files are reduced for display only.")
        per_value_label = QLabel("Rows with the same x value form one bucket.")
        for label in (none_label, per_value_label):
            label.setWordWrap(True)
            label.setStyleSheet("color: palette(placeholder-text);")
        for widget in (none_label, self.n_spin, self.every_edit, self.points_spin, per_value_label):
            self.param_stack.addWidget(widget)

        self.func_boxes: dict[str, QCheckBox] = {}
        funcs = QWidget()
        grid = QGridLayout(funcs)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        for i, name in enumerate(AGG_FUNCTIONS):
            box = QCheckBox(name)
            box.setChecked(name == "mean")
            box.toggled.connect(self._func_toggled)
            self.func_boxes[name] = box
            grid.addWidget(box, i // 3, i % 3)
        self.funcs_widget = funcs
        self.estimate_label = QLabel()
        self.estimate_label.setStyleSheet("color: palette(placeholder-text);")

        form = QFormLayout(self)
        form.addRow("Method", self.method_combo)
        form.addRow(self.param_stack)
        form.addRow("Functions", funcs)
        form.addRow(self.estimate_label)
        self._x_kind = "datetime"
        self._x_is_duration = False
        self._method_changed()

    def set_x_kind(self, kind: str, is_duration: bool = False) -> None:
        self._x_kind = kind
        self._x_is_duration = is_duration
        # Interval and equal-width buckets need numbers or times; text x aggregates per category.
        model = self.method_combo.model()
        for method in ("interval", "target_points"):
            model.item(self.method_combo.findData(method)).setEnabled(kind != "category")
        if kind == "category" and self.method_combo.currentData() in {"interval", "target_points"}:
            self.method_combo.setCurrentIndex(self.method_combo.findData("per_value"))
        if kind == "datetime":
            self.every_edit.setPlaceholderText("duration, e.g. 500ms, 10s, 1m, 1h")
            self.every_edit.setToolTip("Bucket width as a duration: 500ms, 10s, 1m, 15m, 1h, 1d …")
            if not is_duration_text(self.every_edit.text()):
                self.every_edit.setText("1s")
        else:
            self.every_edit.setPlaceholderText("bucket width in x units, e.g. 100")
            self.every_edit.setToolTip("Bucket width in x units")
            if not _is_number(self.every_edit.text()) and not (is_duration and is_duration_text(self.every_edit.text())):
                self.every_edit.setText("100")
        self._validate_every()

    def config(self) -> AggregationConfig:
        method = self.method_combo.currentData()
        every: str | float = self.every_edit.text().strip()
        if self._x_kind != "datetime" and _is_number(every):
            every = float(every)
            every = int(every) if every.is_integer() else every
        return AggregationConfig(
            method=method,
            n=self.n_spin.value(),
            every=every,
            points=self.points_spin.value(),
            functions=[name for name, box in self.func_boxes.items() if box.isChecked()] or ["mean"],
        )

    def set_config(self, agg: AggregationConfig) -> None:
        widgets = (self.method_combo, self.n_spin, self.every_edit, self.points_spin, *self.func_boxes.values())
        for w in widgets:
            w.blockSignals(True)
        try:
            self.method_combo.setCurrentIndex(max(0, self.method_combo.findData(agg.method)))
            self.n_spin.setValue(max(2, agg.n))
            self.every_edit.setText(str(agg.every))
            self.points_spin.setValue(agg.points)
            for name, box in self.func_boxes.items():
                box.setChecked(name in agg.functions)
        finally:
            for w in widgets:
                w.blockSignals(False)
        self._method_changed(emit=False)
        self._validate_every()

    def is_valid(self) -> bool:
        return self.method_combo.currentData() != "interval" or self._validate_every()

    def set_estimate(self, text: str) -> None:
        self.estimate_label.setText(text)

    def _method_changed(self, *_args, emit: bool = True) -> None:
        method = self.method_combo.currentData()
        self.param_stack.setCurrentIndex(AGG_METHODS.index(method))
        self.funcs_widget.setEnabled(method in {"interval", "target_points", "per_value"})
        if emit:
            self.changed.emit()

    def _func_toggled(self) -> None:
        if not any(box.isChecked() for box in self.func_boxes.values()):
            self.func_boxes["mean"].blockSignals(True)
            self.func_boxes["mean"].setChecked(True)
            self.func_boxes["mean"].blockSignals(False)
        self.changed.emit()

    def _every_changed(self) -> None:
        if self._validate_every():
            self.changed.emit()

    def _validate_every(self) -> bool:
        text = self.every_edit.text().strip()
        if self._x_kind == "datetime":
            ok = is_duration_text(text) or (_is_number(text) and float(text) > 0)
        else:
            ok = (_is_number(text) and float(text) > 0) or (self._x_is_duration and is_duration_text(text))
        self.every_edit.setStyleSheet("" if ok else "border: 1px solid #e34948;")
        return ok


def is_duration_text(text: str) -> bool:
    return bool(text) and is_duration(text)


def _is_number(text: str) -> bool:
    try:
        float(text)
    except (TypeError, ValueError):
        return False
    return True


class RangePanel(QWidget):
    """Table of selected x ranges, kept in sync with the plot."""

    modeChanged = Signal()
    addRequested = Signal()
    removeRequested = Signal(int)
    clearRequested = Signal()
    zoomRequested = Signal(int)
    zoomAllRequested = Signal()
    editRequested = Signal(int, object, object, object)  # index, lo, hi, label

    COLUMNS = ("#", "Label", "Start", "End", "Duration")
    _HINT = (
        "No ranges selected - exports include all data. Shift+drag on the plot, use range mode (R) "
        "or “Add from view” to select time ranges for export."
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        title = QLabel("<b>Ranges</b>")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Absolute x values", "absolute")
        self.mode_combo.addItem("Relative to data start", "relative")
        self.mode_combo.setToolTip(
            "How ranges are stored in a saved configuration.\n"
            "Relative ranges are offsets from the first x value, so they can be\n"
            "applied to other files recorded at different times."
        )
        self.mode_combo.currentIndexChanged.connect(self.modeChanged)
        self.add_button = add = QPushButton("Add from view")
        add.setToolTip("Add a range covering the middle of the visible area")
        add.clicked.connect(self.addRequested)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clearRequested)
        self.zoom_button = QPushButton("Zoom to")
        self.zoom_button.setToolTip("Zoom the plot to the selected range (or all ranges)")
        self.zoom_button.clicked.connect(self._zoom)

        header = QHBoxLayout()
        header.addWidget(title)
        header.addSpacing(12)
        header.addWidget(QLabel("Save as"))
        header.addWidget(self.mode_combo)
        header.addStretch(1)
        for button in (add, self.zoom_button, self.remove_button, self.clear_button):
            header.addWidget(button)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 210)
        self.table.setColumnWidth(3, 210)
        self.table.itemChanged.connect(self._item_changed)
        self.table.cellDoubleClicked.connect(lambda row, col: col == 0 and self.zoomRequested.emit(row))
        self.hint = QLabel(self._HINT)
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: palette(placeholder-text);")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(header)
        layout.addWidget(self.hint)
        layout.addWidget(self.table, 1)
        self._kind = "datetime"
        self._tz: str | None = None
        self._updating = False
        self.refresh([])

    def mode(self) -> str:
        return self.mode_combo.currentData()

    def set_mode(self, mode: str) -> None:
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(mode)))

    def set_x_kind(self, kind: str, tz: str | None) -> None:
        # Time-zone aware columns are displayed (and edited) in UTC.
        self._kind, self._tz = kind, tz
        suffix = " (UTC)" if kind == "datetime" and tz else ""
        self.table.setHorizontalHeaderLabels(["#", "Label", f"Start{suffix}", f"End{suffix}", "Duration"])
        allowed = kind != "category"
        self.mode_combo.setEnabled(allowed)
        self.add_button.setEnabled(allowed)
        self.hint.setText(self._HINT if allowed else "Ranges need a numeric or time x axis; the x axis is text.")

    def refresh(self, ranges: list[RangeEntry]) -> None:
        self._updating = True
        try:
            selected = self.table.currentRow()
            self.table.setRowCount(len(ranges))
            for row, entry in enumerate(ranges):
                values = [
                    str(row + 1),
                    entry.label,
                    format_plot_x(entry.lo, self._kind),
                    format_plot_x(entry.hi, self._kind),
                    format_seconds(entry.hi - entry.lo) if self._kind == "datetime" else f"{entry.hi - entry.lo:.6g}",
                ]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    if col in (0, 4):
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if col in (2, 3):
                        item.setToolTip("Edit to change the range (ISO date/time or number)")
                    self.table.setItem(row, col, item)
            if 0 <= selected < len(ranges):
                self.table.selectRow(selected)
        finally:
            self._updating = False
        has = bool(ranges)
        self.hint.setVisible(not has)
        self.table.setVisible(has)
        for button in (self.remove_button, self.clear_button, self.zoom_button):
            button.setEnabled(has)

    def _item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating:
            return
        row, col = item.row(), item.column()
        if col == 1:
            self.editRequested.emit(row, None, None, item.text().strip())
            return
        try:
            value = parse_x_value(item.text().strip(), self._kind, None)
            plot_value = x_to_plot(value, self._kind)
        except ProcessingError:
            self.editRequested.emit(row, None, None, None)  # triggers a refresh with the old value
            return
        if col == 2:
            self.editRequested.emit(row, plot_value, None, None)
        elif col == 3:
            self.editRequested.emit(row, None, plot_value, None)

    def _remove_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0 and self.table.rowCount():
            row = self.table.rowCount() - 1
        if row >= 0:
            self.removeRequested.emit(row)

    def _zoom(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            self.zoomRequested.emit(rows[0].row())
        else:
            self.zoomAllRequested.emit()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.table.hasFocus():
            self._remove_selected()
            return
        super().keyPressEvent(event)

