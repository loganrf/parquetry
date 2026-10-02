"""Export the plots as an image file (PNG, JPEG, SVG or PDF) or to the clipboard."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .plot_area import IMAGE_FORMATS, TRANSPARENT_FORMATS, PlotArea

#: Choices of the format box: (label, file suffix).
FORMATS = [
    ("PNG image", ".png"),
    ("JPEG image", ".jpg"),
    ("SVG vector graphic", ".svg"),
    ("PDF document", ".pdf"),
]
RESOLUTIONS = [(1.0, "1× (as on screen)"), (1.5, "1.5×"), (2.0, "2× (sharp)"), (3.0, "3×"), (4.0, "4× (print)")]
VECTOR = {".svg", ".pdf"}


class ImageExportDialog(QDialog):
    """Choose file, format, size and resolution, then save (or copy) an image of the plots."""

    saved = Signal(str)

    def __init__(self, area: PlotArea, settings: QSettings, source: Path | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export image")
        self.area = area
        self.settings = settings
        suffix = settings.value("image/format", ".png")
        suffix = suffix if suffix in {value for _, value in FORMATS} else ".png"
        folder = Path(settings.value("image/last_dir", str(source.parent if source else Path.home())))
        name = f"{source.stem}_plot{suffix}" if source else f"plot{suffix}"

        self.path_edit = QLineEdit(str(folder / name))
        self.path_edit.textChanged.connect(self._path_changed)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        self.format_combo = QComboBox()
        for label, value in FORMATS:
            self.format_combo.addItem(f"{label} ({value})", value)
        self.format_combo.setCurrentIndex(self.format_combo.findData(suffix))
        self.format_combo.currentIndexChanged.connect(self._format_changed)

        size = area.image_size()
        self.width_spin = QSpinBox()
        self.height_spin = QSpinBox()
        for spin, value in ((self.width_spin, size.width()), (self.height_spin, size.height())):
            spin.setRange(100, 20000)
            spin.setSingleStep(50)
            spin.setSuffix(" px")
            spin.setValue(value)
            spin.valueChanged.connect(self._update_info)
        self.width_spin.setToolTip("Width of the layout; text keeps its size, so a wider image shows more detail")
        self.height_spin.setToolTip("Height of the layout")
        window = QPushButton("As shown")
        window.setToolTip("The size of the plots on screen")
        window.clicked.connect(self._window_size)
        size_row = QHBoxLayout()
        size_row.addWidget(self.width_spin)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.height_spin)
        size_row.addWidget(window)
        size_row.addStretch(1)
        self.resolution_combo = QComboBox()
        for value, label in RESOLUTIONS:
            self.resolution_combo.addItem(label, value)
        stored = settings.value("image/resolution", 2.0, type=float)
        self.resolution_combo.setCurrentIndex(max(0, self.resolution_combo.findData(stored)))
        self.resolution_combo.setToolTip("Pixels per layout pixel: text and lines grow with it")
        self.resolution_combo.currentIndexChanged.connect(self._update_info)
        self.transparent_check = QCheckBox("Transparent background")
        self.transparent_check.setChecked(settings.value("image/transparent", False, type=bool))
        self.info_label = QLabel()
        self.info_label.setStyleSheet("color: palette(placeholder-text);")

        form = QFormLayout()
        form.addRow("File", path_row)
        form.addRow("Format", self.format_combo)
        form.addRow("Size", size_row)
        form.addRow("Resolution", self.resolution_combo)
        form.addRow("", self.transparent_check)
        form.addRow("", self.info_label)

        self.message = QLabel()
        copy = QPushButton("Copy to clipboard")
        copy.setToolTip("Copy the image instead of saving it")
        copy.clicked.connect(self.copy)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton("Save")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.save)
        buttons = QHBoxLayout()
        buttons.addWidget(copy)
        buttons.addWidget(self.message, 1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addStretch(1)
        layout.addLayout(buttons)
        self.resize(560, 0)
        self._update_info()

    # ------------------------------------------------------------------ state
    def suffix(self) -> str:
        return self.format_combo.currentData()

    def image_size(self) -> QSize:
        """Layout size of the image in logical pixels."""
        return QSize(self.width_spin.value(), self.height_spin.value())

    def resolution(self) -> float:
        return self.resolution_combo.currentData()

    def transparent(self) -> bool:
        return self.transparent_check.isEnabled() and self.transparent_check.isChecked()

    def path(self) -> Path:
        path = Path(self.path_edit.text().strip() or f"plot{self.suffix()}").expanduser()
        return path if path.suffix.lower() in IMAGE_FORMATS else path.with_name(path.name + self.suffix())

    # -------------------------------------------------------------- reactions
    def _path_changed(self, text: str) -> None:
        suffix = Path(text.strip()).suffix.lower()
        suffix = ".jpg" if suffix == ".jpeg" else suffix
        index = self.format_combo.findData(suffix)
        if index >= 0 and index != self.format_combo.currentIndex():
            self.format_combo.blockSignals(True)
            self.format_combo.setCurrentIndex(index)
            self.format_combo.blockSignals(False)
            self._update_info()

    def _format_changed(self) -> None:
        text = self.path_edit.text().strip()
        if text:
            path = Path(text)
            if path.suffix.lower() in IMAGE_FORMATS:
                path = path.with_suffix(self.suffix())
            else:
                path = path.with_name(path.name + self.suffix())
            self.path_edit.blockSignals(True)
            self.path_edit.setText(str(path))
            self.path_edit.blockSignals(False)
        self._update_info()

    def _window_size(self) -> None:
        size = self.area.image_size()
        self.width_spin.setValue(size.width())
        self.height_spin.setValue(size.height())

    def _update_info(self) -> None:
        vector = self.suffix() in VECTOR
        self.resolution_combo.setEnabled(not vector)
        self.transparent_check.setEnabled(self.suffix() in TRANSPARENT_FORMATS)
        if vector:
            self.info_label.setText("Vector graphic: sharp at any zoom level")
        else:
            width = round(self.width_spin.value() * self.resolution())
            height = round(self.height_spin.value() * self.resolution())
            self.info_label.setText(f"The image will be {width:,} × {height:,} pixels")

    def _browse(self) -> None:
        filters = ";;".join(f"{label} (*{suffix})" for label, suffix in FORMATS)
        current = next(f"{label} (*{suffix})" for label, suffix in FORMATS if suffix == self.suffix())
        filename, _ = QFileDialog.getSaveFileName(self, "Export image", str(self.path()), filters, current)
        if filename:
            self.path_edit.setText(filename)

    # ----------------------------------------------------------------- actions
    def _remember(self, path: Path | None = None) -> None:
        if path is not None:
            self.settings.setValue("image/last_dir", str(path.parent))
        self.settings.setValue("image/format", self.suffix())
        self.settings.setValue("image/resolution", self.resolution())
        self.settings.setValue("image/transparent", self.transparent_check.isChecked())

    def save(self) -> Path | None:
        path = self.path()
        if path.exists():
            answer = QMessageBox.question(self, "Overwrite file?", f"{path} already exists. Overwrite it?")
            if answer != QMessageBox.StandardButton.Yes:
                return None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.area.save_image(path, self.image_size(), self.resolution(), self.transparent())
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Export image", f"Could not save {path}:\n{exc}")
            return None
        self._remember(path)
        self.saved.emit(str(path))
        self.accept()
        return path

    def copy(self) -> None:
        image = self.area.render_image(self.image_size(), self.resolution(), self.transparent())
        QGuiApplication.clipboard().setImage(image)
        self._remember()
        self.message.setText(f"Copied {image.width():,} × {image.height():,} pixels")
