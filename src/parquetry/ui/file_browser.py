"""Start page: browse the file system for a Parquet or CSV file and preview it."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..dataset import DATA_SUFFIXES, DatasetInfo, format_bytes, inspect_file, is_data_path
from . import workers

NAME_FILTERS = [f"*{suffix}" for suffix in DATA_SUFFIXES]
FILE_DIALOG_FILTERS = (
    "Data files (*.parquet *.parq *.pq *.csv *.tsv);;Parquet files (*.parquet *.parq *.pq);;"
    "CSV files (*.csv *.tsv);;All files (*)"
)
MAX_RECENT = 12
_ROLE = {
    "datetime": "time", "date": "date", "numeric": "numeric", "boolean": "boolean", "duration": "duration",
    "text": "text",
}


def recent_files(settings: QSettings) -> list[str]:
    value = settings.value("recent_files", [])
    if isinstance(value, str):
        value = [value]
    return [v for v in (value or []) if isinstance(v, str)]


def add_recent_file(settings: QSettings, path: str | Path) -> None:
    path = str(Path(path).resolve())
    items = [p for p in recent_files(settings) if p != path]
    settings.setValue("recent_files", [path, *items][:MAX_RECENT])


class FileBrowserPage(QWidget):
    fileChosen = Signal(str)
    backRequested = Signal()

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._generation = 0
        self._selected: Path | None = None

        title = QLabel("<h2 style='margin:0'>Open a Parquet or CSV file</h2>")
        subtitle = QLabel("Double-click a file to explore it, or drop one onto the window.")
        subtitle.setStyleSheet("color: palette(placeholder-text);")

        style = self.style()
        self.back_button = QPushButton("◀ Back to plot")
        self.back_button.clicked.connect(self.backRequested)
        self.back_button.hide()
        up = QToolButton()
        up.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogToParent))
        up.setToolTip("Parent folder (Backspace)")
        up.clicked.connect(self.go_up)
        home = QToolButton()
        home.setIcon(style.standardIcon(QStyle.StandardPixmap.SP_DirHomeIcon))
        home.setToolTip("Home folder")
        home.clicked.connect(lambda: self.set_directory(Path.home()))
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Folder or file path")
        self.path_edit.returnPressed.connect(self._path_entered)
        browse = QPushButton("Browse…")
        browse.setToolTip("Use the system file dialog")
        browse.clicked.connect(self._browse)
        nav = QHBoxLayout()
        nav.addWidget(self.back_button)
        nav.addWidget(up)
        nav.addWidget(home)
        nav.addWidget(self.path_edit, 1)
        nav.addWidget(browse)

        self.model = QFileSystemModel(self)
        self.model.setRootPath(QDir.rootPath())
        self.model.setFilter(QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot)
        self.model.setNameFilters(NAME_FILTERS)
        self.model.setNameFilterDisables(False)
        self.tree = QTreeView()
        self.tree.setModel(self.model)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.tree.setColumnHidden(2, True)  # type column
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setStretchLastSection(False)
        self.tree.setItemsExpandable(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.activated.connect(self._activated)
        self.tree.doubleClicked.connect(self._activated)
        self.tree.selectionModel().currentChanged.connect(self._current_changed)
        self.show_all = QCheckBox("Show all files")
        self.show_all.toggled.connect(lambda on: self.model.setNameFilters([] if on else NAME_FILTERS))
        tree_box = QWidget()
        tree_layout = QVBoxLayout(tree_box)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.addWidget(self.tree, 1)
        tree_layout.addWidget(self.show_all)

        # -- right side: recent files + preview ----------------------------------------
        self.recent = QListWidget()
        self.recent.setMaximumHeight(150)
        self.recent.itemActivated.connect(lambda item: self._open(item.data(Qt.ItemDataRole.UserRole)))
        self.recent.itemClicked.connect(lambda item: self._preview(Path(item.data(Qt.ItemDataRole.UserRole))))
        recent_box = QGroupBox("Recent files")
        QVBoxLayout(recent_box).addWidget(self.recent)

        self.preview_title = QLabel("Select a file to see its details")
        self.preview_title.setWordWrap(True)
        self.preview_title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary = QFormLayout()
        self.summary_labels: dict[str, QLabel] = {}
        for key in ("Format", "Size", "Rows", "Columns", "Row groups", "Compression", "Created by", "Modified"):
            label = QLabel()
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.summary_labels[key] = label
            self.summary.addRow(f"{key}:", label)
        self.schema = QTableWidget(0, 3)
        self.schema.setHorizontalHeaderLabels(["Column", "Type", "Use"])
        self.schema.verticalHeader().hide()
        self.schema.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.schema.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.schema.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.schema.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.open_button = QPushButton("Open")
        self.open_button.setDefault(True)
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(lambda: self._selected and self._open(str(self._selected)))
        preview_box = QGroupBox("Details")
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.addWidget(self.preview_title)
        preview_layout.addLayout(self.summary)
        preview_layout.addWidget(self.schema, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.open_button)
        preview_layout.addLayout(row)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(recent_box)
        right_layout.addWidget(preview_box, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(tree_box)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([640, 440])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(nav)
        layout.addWidget(splitter, 1)

        start = Path(settings.value("browser/last_dir", str(Path.cwd())))
        self.set_directory(start if start.is_dir() else Path.home())
        self.refresh_recent()
        self._clear_preview()

    # ------------------------------------------------------------ navigation
    def set_directory(self, path: Path) -> None:
        path = Path(path).expanduser()
        if not path.is_dir():
            return
        index = self.model.index(str(path))
        self.tree.setRootIndex(index)
        self.path_edit.setText(str(path))
        self.settings.setValue("browser/last_dir", str(path))

    def current_directory(self) -> Path:
        return Path(self.model.filePath(self.tree.rootIndex()) or QDir.rootPath())

    def go_up(self) -> None:
        current = self.current_directory()
        if current.parent != current:
            self.set_directory(current.parent)

    def reveal(self, path: Path) -> None:
        """Navigate to the folder containing *path* and select it."""
        path = Path(path)
        self.set_directory(path.parent)
        index = self.model.index(str(path))
        if index.isValid():
            self.tree.setCurrentIndex(index)
            self.tree.scrollTo(index)

    def set_can_go_back(self, can: bool, name: str = "") -> None:
        self.back_button.setVisible(can)
        self.back_button.setText(f"◀ Back to {name}" if name else "◀ Back")

    def refresh_recent(self) -> None:
        self.recent.clear()
        for path in recent_files(self.settings):
            p = Path(path)
            item = QListWidgetItem(f"{p.name}   —   {p.parent}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            if not p.exists():
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.recent.addItem(item)
        self.recent.parentWidget().setVisible(self.recent.count() > 0)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Backspace and not self.path_edit.hasFocus():
            self.go_up()
            return
        super().keyPressEvent(event)

    def _path_entered(self) -> None:
        path = Path(self.path_edit.text().strip()).expanduser()
        if path.is_dir():
            self.set_directory(path)
        elif path.is_file():
            self._open(str(path))
        else:
            self.path_edit.setStyleSheet("border: 1px solid #e34948;")
            return
        self.path_edit.setStyleSheet("")

    def _browse(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Open data file", str(self.current_directory()), FILE_DIALOG_FILTERS)
        if filename:
            self._open(filename)

    def _activated(self, index: QModelIndex) -> None:
        path = Path(self.model.filePath(index))
        if self.model.isDir(index):
            self.set_directory(path)
        else:
            self._open(str(path))

    def _current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if current.isValid() and not self.model.isDir(current):
            self._preview(Path(self.model.filePath(current)))

    def _open(self, path: str) -> None:
        if path:
            self.fileChosen.emit(path)

    # --------------------------------------------------------------- preview
    def _clear_preview(self) -> None:
        self._selected = None
        self.open_button.setEnabled(False)
        for label in self.summary_labels.values():
            label.clear()
        self.schema.setRowCount(0)

    def _preview(self, path: Path) -> None:
        self._generation += 1
        generation = self._generation
        self._clear_preview()
        self._selected = path
        self.preview_title.setText(f"<b>{path.name}</b><br><span style='color:gray'>Reading metadata…</span>")
        if not is_data_path(path) and not self.show_all.isChecked():
            return
        workers.submit(
            # Only the start of a CSV file is checked: the preview must stay quick for large files.
            lambda: inspect_file(path, verify=False),
            on_done=lambda info: self._preview_done(generation, info),
            on_error=lambda exc: self._preview_failed(generation, path, exc),
        )

    def _preview_done(self, generation: int, info: DatasetInfo) -> None:
        if generation != self._generation:
            return
        self.preview_title.setText(f"<b>{info.path.name}</b><br><span style='color:gray'>{info.path.parent}</span>")
        modified = dt.datetime.fromtimestamp(info.mtime).strftime("%Y-%m-%d %H:%M")
        values = {
            "Format": info.format_label,
            "Size": format_bytes(info.size_bytes),
            "Rows": f"{info.num_rows:,}",
            "Columns": str(len(info.columns)),
            "Row groups": "" if info.num_row_groups is None else str(info.num_row_groups),
            "Compression": info.compression or "",
            "Created by": info.created_by or "",
            "Modified": modified,
        }
        for key, value in values.items():
            self.summary_labels[key].setText(value)
            self.summary.setRowVisible(self.summary_labels[key], bool(value))
        x = info.guess_x()
        self.schema.setRowCount(len(info.columns))
        for row, col in enumerate(info.columns):
            use = _ROLE.get(col.kind, "not plottable")
            if col.name == x:
                use = "x axis (default)"
            for i, text in enumerate((col.name, col.dtype_name, use)):
                self.schema.setItem(row, i, QTableWidgetItem(text))
        self.open_button.setEnabled(True)

    def _preview_failed(self, generation: int, path: Path, exc: BaseException) -> None:
        if generation != self._generation:
            return
        self.preview_title.setText(f"<b>{path.name}</b><br><span style='color:#e34948'>{exc}</span>")
        self.open_button.setEnabled(False)
