"""Table of the processed rows behind the plot, text included."""

from __future__ import annotations

import datetime as dt
import math

import polars as pl
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QLabel, QTableView, QVBoxLayout, QWidget

from ..dataset import ROW_INDEX, ROW_INDEX_LABEL
from ..processing import TableData


def format_cell(value) -> str:
    """Display text of one value; numbers keep full precision."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return "NaN" if math.isnan(value) else repr(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="microseconds" if value.microsecond else "seconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


class FrameModel(QAbstractTableModel):
    """Read-only model of a polars DataFrame."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._headers: list[str] = []
        self._columns: list[list] = []
        self._numeric: list[bool] = []
        self._rows = 0

    def set_frame(self, frame: pl.DataFrame | None) -> None:
        self.beginResetModel()
        if frame is None:
            self._headers, self._columns, self._numeric, self._rows = [], [], [], 0
        else:
            self._headers = [ROW_INDEX_LABEL if name == ROW_INDEX else name for name in frame.columns]
            self._columns = [frame[name].to_list() for name in frame.columns]
            self._numeric = [dtype.is_numeric() for dtype in frame.dtypes]
            self._rows = frame.height
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt naming
        return 0 if parent.isValid() else self._rows

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802 - Qt naming
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return format_cell(self._columns[index.column()][index.row()])
        if role == Qt.ItemDataRole.TextAlignmentRole:
            side = Qt.AlignmentFlag.AlignRight if self._numeric[index.column()] else Qt.AlignmentFlag.AlignLeft
            return side | Qt.AlignmentFlag.AlignVCenter
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return self._headers[section] if section < len(self._headers) else None
        return str(section + 1)


class DataTable(QTableView):
    """Table view that copies the selected cells as tab-separated text."""

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
            return
        super().keyPressEvent(event)

    def copy_selection(self) -> None:
        indexes = self.selectionModel().selectedIndexes()
        if not indexes:
            return
        rows = sorted({i.row() for i in indexes})
        columns = sorted({i.column() for i in indexes})
        cells = {(i.row(), i.column()): i.data() or "" for i in indexes}
        lines = ["\t".join(cells.get((r, c), "") for c in columns) for r in rows]
        if len(rows) > 1 or len(columns) > 1:  # several cells: include the column names
            model = self.model()
            lines.insert(0, "\t".join(str(model.headerData(c, Qt.Orientation.Horizontal)) for c in columns))
        QGuiApplication.clipboard().setText("\n".join(lines))


class DataPanel(QWidget):
    """The processed rows of the visible part of the plot."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: palette(placeholder-text);")
        self.model = FrameModel(self)
        self.table = DataTable()
        self.table.setModel(self.model)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.verticalHeader().setDefaultSectionSize(self.fontMetrics().height() + 6)
        self.table.setToolTip("Ctrl+C copies the selected cells")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.status)
        layout.addWidget(self.table, 1)
        self.set_message("Open a file to see its rows.")

    def set_message(self, text: str) -> None:
        self.model.set_frame(None)
        self.status.setText(text)

    def set_table(self, table: TableData) -> None:
        self.model.set_frame(table.frame)
        shown = table.frame.height
        where = " in view" if table.windowed else ""
        if table.truncated:
            hint = " Zoom in on the plot to see other rows." if table.windowed else ""
            self.status.setText(f"First {shown:,} of ≈ {table.rows:,} rows{where}.{hint}")
        else:
            self.status.setText(f"{shown:,} row{'s' if shown != 1 else ''}{where}")
        self.table.resizeColumnsToContents()
        for column in range(self.model.columnCount()):
            self.table.setColumnWidth(column, min(self.table.columnWidth(column), 320))
