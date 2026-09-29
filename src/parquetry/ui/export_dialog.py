"""CSV export dialog with live preview and configuration saving."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..cli import command_line
from ..config import (
    QUOTE_STYLES,
    TIME_FORMAT_LABELS,
    AggregationConfig,
    ConfigError,
    CsvOptions,
    ProcessingConfig,
)
from ..dataset import ROW_INDEX, ROW_INDEX_LABEL, DatasetInfo
from ..processing import (
    ROW_INDEX_HEADER,
    ProcessingError,
    estimate_rows,
    export_file,
    plan_outputs,
    preview_csv,
    render_output_path,
    resolve_ranges,
    validate_for,
)
from . import workers

SEPARATORS = [("Comma  ,", ","), ("Semicolon  ;", ";"), ("Tab", "\t"), ("Pipe  |", "|"), ("Space", " "), ("Other…", None)]


def path_to_template(path: Path, input_path: Path) -> str:
    """Generalise a concrete output path into a template for other inputs."""
    name = path.name
    if input_path.stem and input_path.stem in name:
        name = name.replace(input_path.stem, "{stem}", 1)
    if path.parent.resolve() == input_path.parent.resolve():
        return name
    return str(path.parent / name)


def save_config_interactive(parent: QWidget, settings: QSettings, cfg: ProcessingConfig, info: DatasetInfo | None) -> Path | None:
    """Ask for a file name, save *cfg* and show how to reuse it from the CLI."""
    start_dir = settings.value("config/last_dir", str(info.path.parent if info else Path.home()))
    default_name = f"{info.path.stem}_config.json" if info else "parquetry_config.json"
    filename, _ = QFileDialog.getSaveFileName(
        parent, "Save processing configuration", str(Path(start_dir) / default_name), "JSON configuration (*.json)"
    )
    if not filename:
        return None
    path = Path(filename)
    if path.suffix == "":
        path = path.with_suffix(".json")
    try:
        cfg.save(path)
    except OSError as exc:
        QMessageBox.critical(parent, "Save configuration", f"Could not save {path}:\n{exc}")
        return None
    settings.setValue("config/last_dir", str(path.parent))
    folder = info.path.parent if info else Path(".")
    command = command_line([str(folder / "*.parquet")], config_path=str(path))
    box = QMessageBox(parent)
    box.setWindowTitle("Configuration saved")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(f"Saved to {path}")
    box.setInformativeText(
        "Apply it to other files with File ▸ Batch export, or from a script:\n\n" + command
    )
    box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    copy = box.addButton("Copy command", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Ok)
    box.exec()
    if box.clickedButton() is copy:
        QGuiApplication.clipboard().setText(command)
    return path


class ExportDialog(QDialog):
    def __init__(self, info: DatasetInfo, cfg: ProcessingConfig, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Export CSV – {info.path.name}")
        self.info = info
        self.base = cfg.copy()
        self.settings = settings
        self._generation = 0
        self._running = False
        kind = info.x_kind(cfg.x)
        self._x_kind = kind

        # -- output -----------------------------------------------------------
        default_path = self._initial_output_path()
        self.path_edit = QLineEdit(str(default_path))
        self.path_edit.textChanged.connect(self._schedule_preview)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.template_label = QLabel()
        self.template_label.setWordWrap(True)
        self.template_label.setStyleSheet("color: palette(placeholder-text);")
        out_row = QHBoxLayout()
        out_row.addWidget(self.path_edit, 1)
        out_row.addWidget(browse)
        output_box = QGroupBox("Output file")
        out_layout = QVBoxLayout(output_box)
        out_layout.addLayout(out_row)
        out_layout.addWidget(self.template_label)

        # -- data ---------------------------------------------------------------
        n_ranges = len(cfg.ranges)
        self.scope_all = QRadioButton("All data")
        self.scope_ranges = QRadioButton(f"Selected ranges only ({n_ranges})")
        self.scope_ranges.setEnabled(n_ranges > 0)
        (self.scope_ranges if n_ranges else self.scope_all).setChecked(True)
        scope_group = QButtonGroup(self)
        scope_group.addButton(self.scope_all)
        scope_group.addButton(self.scope_ranges)
        self.split_check = QCheckBox("One file per range")
        self.split_check.setToolTip("Writes <name>_<range label or number>.csv for every range")
        self.split_check.setChecked(cfg.output.split_ranges and n_ranges > 1)
        self.agg_combo = QComboBox()
        self.agg_combo.addItem(f"As configured: {cfg.aggregation.describe()}", "configured")
        if cfg.aggregation.method != "none":
            self.agg_combo.addItem("No aggregation (all rows)", "none")
        self.sort_check = QCheckBox("Sort rows by x")
        self.sort_check.setChecked(cfg.sort)
        self.sort_check.setToolTip("Disable only for files that are already sorted by x (saves time and memory)")
        data_box = QGroupBox("Data")
        data_form = QFormLayout(data_box)
        scope_row = QHBoxLayout()
        scope_row.addWidget(self.scope_all)
        scope_row.addWidget(self.scope_ranges)
        scope_row.addWidget(self.split_check)
        scope_row.addStretch(1)
        data_form.addRow("Rows", scope_row)
        data_form.addRow("Aggregation", self.agg_combo)
        data_form.addRow("", self.sort_check)
        for widget in (self.scope_all, self.scope_ranges, self.split_check, self.sort_check):
            widget.toggled.connect(self._scope_changed)
        self.agg_combo.currentIndexChanged.connect(self._agg_changed)

        # -- columns ------------------------------------------------------------
        self.columns_table = QTableWidget(0, 2)
        self.columns_table.setHorizontalHeaderLabels(["Column", "Header in CSV"])
        self.columns_table.verticalHeader().hide()
        self.columns_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.columns_table.itemChanged.connect(self._schedule_preview)
        self.columns_table.setMinimumHeight(120)
        columns_box = QGroupBox("Columns")
        QVBoxLayout(columns_box).addWidget(self.columns_table)

        # -- format -------------------------------------------------------------
        csv = cfg.csv
        self.sep_combo = QComboBox()
        for label, value in SEPARATORS:
            self.sep_combo.addItem(label, value)
        self.sep_other = QLineEdit()
        self.sep_other.setMaxLength(1)
        self.sep_other.setFixedWidth(40)
        index = self.sep_combo.findData(csv.separator)
        if index < 0:
            index = self.sep_combo.count() - 1
            self.sep_other.setText(csv.separator)
        self.sep_combo.setCurrentIndex(index)
        sep_row = QHBoxLayout()
        sep_row.addWidget(self.sep_combo, 1)
        sep_row.addWidget(self.sep_other)

        self.time_combo = QComboBox()
        if kind == "datetime":
            for key, label in TIME_FORMAT_LABELS.items():
                self.time_combo.addItem(label, key)
        else:
            self.time_combo.addItem("Raw x values", "iso")
            self.time_combo.addItem("Offset from data start", "elapsed_s")
        self.time_combo.setCurrentIndex(max(0, self.time_combo.findData(csv.time_format)))
        self.datetime_edit = QLineEdit(csv.datetime_format)
        self.datetime_edit.setToolTip("strftime format, e.g. %Y-%m-%d %H:%M:%S%.3f (%.3f = milliseconds)")
        self.precision_spin = QSpinBox()
        self.precision_spin.setRange(-1, 17)
        self.precision_spin.setSpecialValueText("Full precision")
        self.precision_spin.setValue(-1 if csv.float_precision is None else csv.float_precision)
        self.precision_spin.setSuffix(" decimals")
        self.decimal_comma = QCheckBox("Decimal comma (1,5)")
        self.decimal_comma.setChecked(csv.decimal_comma)
        self.null_edit = QLineEdit(csv.null_value)
        self.null_edit.setPlaceholderText("empty")
        self.quote_combo = QComboBox()
        for style in QUOTE_STYLES:
            self.quote_combo.addItem(style.replace("_", " ").capitalize(), style)
        self.quote_combo.setCurrentIndex(max(0, self.quote_combo.findData(csv.quote_style)))
        self.eol_combo = QComboBox()
        self.eol_combo.addItem("LF (Linux, macOS)", "lf")
        self.eol_combo.addItem("CRLF (Windows)", "crlf")
        self.eol_combo.setCurrentIndex(max(0, self.eol_combo.findData(csv.line_terminator)))
        self.header_check = QCheckBox("Header row")
        self.header_check.setChecked(csv.include_header)
        self.bom_check = QCheckBox("UTF-8 BOM (Excel)")
        self.bom_check.setChecked(csv.include_bom)
        checks = QHBoxLayout()
        for box in (self.header_check, self.bom_check, self.decimal_comma):
            checks.addWidget(box)
        checks.addStretch(1)

        format_box = QGroupBox("CSV format")
        form = QFormLayout(format_box)
        form.addRow("Separator", sep_row)
        form.addRow("Time column", self.time_combo)
        form.addRow("Date/time format", self.datetime_edit)
        form.addRow("Numbers", self.precision_spin)
        form.addRow("Missing values", self.null_edit)
        form.addRow("Quoting", self.quote_combo)
        form.addRow("Line endings", self.eol_combo)
        form.addRow(checks)
        for widget in (self.sep_combo, self.time_combo, self.quote_combo, self.eol_combo):
            widget.currentIndexChanged.connect(self._format_changed)
        for widget in (self.sep_other, self.datetime_edit, self.null_edit):
            widget.textChanged.connect(self._format_changed)
        self.precision_spin.valueChanged.connect(self._format_changed)
        for widget in (self.decimal_comma, self.header_check, self.bom_check):
            widget.toggled.connect(self._format_changed)

        settings_widget = QWidget()
        settings_layout = QVBoxLayout(settings_widget)
        settings_layout.setContentsMargins(0, 0, 6, 0)
        for box in (output_box, data_box, columns_box, format_box):
            settings_layout.addWidget(box)
        settings_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(settings_widget)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        # -- preview ------------------------------------------------------------
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.preview_label = QLabel("Preview")
        self.estimate_label = QLabel()
        self.estimate_label.setWordWrap(True)
        self.estimate_label.setStyleSheet("color: palette(placeholder-text);")
        preview_widget = QWidget()
        preview_layout = QVBoxLayout(preview_widget)
        preview_layout.setContentsMargins(6, 0, 0, 0)
        preview_layout.addWidget(self.preview_label)
        preview_layout.addWidget(self.preview, 1)
        preview_layout.addWidget(self.estimate_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(scroll)
        splitter.addWidget(preview_widget)
        splitter.setSizes([520, 480])

        # -- buttons ------------------------------------------------------------
        self.save_button = QPushButton("Save config…")
        self.save_button.setToolTip("Save these settings as a JSON configuration for batch/CLI processing")
        self.save_button.clicked.connect(self._save_config)
        self.command_button = QPushButton("Copy CLI command")
        self.command_button.setToolTip("Copy an equivalent `parquetry export` command to the clipboard")
        self.command_button.clicked.connect(self._copy_command)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        self.export_button = QPushButton("Export")
        self.export_button.setDefault(True)
        self.export_button.clicked.connect(self._export)
        self.message = QLabel()
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.command_button)
        buttons.addWidget(self.message, 1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.export_button)

        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        layout.addLayout(buttons)
        self.resize(1080, 700)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(250)
        self._preview_timer.timeout.connect(self._run_preview)
        self._fill_columns()
        self._scope_changed()
        self._format_changed()

    # ------------------------------------------------------------------ state
    def _initial_output_path(self) -> Path:
        template = self.base.output.path.replace("_{range}", "").replace("{range}", "")
        try:
            return render_output_path(template, self.info.path)
        except ProcessingError:
            return self.info.path.with_name(f"{self.info.path.stem}_export.csv")

    def _aggregation(self) -> AggregationConfig:
        if self.agg_combo.currentData() == "none":
            return AggregationConfig()
        return self.base.aggregation

    def _separator(self) -> str:
        value = self.sep_combo.currentData()
        return value if value is not None else (self.sep_other.text() or ",")

    def _output_names(self) -> list[tuple[str, str]]:
        """(output column key, description) for the columns table."""
        cfg = self.base.copy()
        cfg.aggregation = self._aggregation()
        x = cfg.x
        rows = [(x, ROW_INDEX_LABEL if x == ROW_INDEX else f"{x}  (x)")]
        rows += [(name, name) for name, _, _ in cfg.output_columns()]
        return rows

    def _fill_columns(self) -> None:
        renames = self._renames() if self.columns_table.rowCount() else dict(self.base.csv.rename)
        self.columns_table.blockSignals(True)
        try:
            names = self._output_names()
            self.columns_table.setRowCount(len(names))
            for row, (key, label) in enumerate(names):
                source = QTableWidgetItem(label)
                source.setData(Qt.ItemDataRole.UserRole, key)
                source.setFlags(source.flags() & ~Qt.ItemFlag.ItemIsEditable)
                default = ROW_INDEX_HEADER if key == ROW_INDEX else key
                header = QTableWidgetItem(renames.get(key, default))
                self.columns_table.setItem(row, 0, source)
                self.columns_table.setItem(row, 1, header)
        finally:
            self.columns_table.blockSignals(False)

    def _renames(self) -> dict[str, str]:
        out = {}
        for row in range(self.columns_table.rowCount()):
            key = self.columns_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            header = self.columns_table.item(row, 1).text().strip()
            default = ROW_INDEX_HEADER if key == ROW_INDEX else key
            if header and header != default:
                out[key] = header
        return out

    def config(self) -> ProcessingConfig:
        """The configuration described by the dialog."""
        cfg = self.base.copy()
        if not self.scope_ranges.isChecked():
            cfg.ranges = []
        cfg.aggregation = self._aggregation()
        cfg.sort = self.sort_check.isChecked()
        cfg.output.split_ranges = self.split_check.isEnabled() and self.split_check.isChecked()
        path = Path(self.path_edit.text().strip() or f"{self.info.path.stem}_export.csv").expanduser()
        if not path.is_absolute():
            path = self.info.path.parent / path
        cfg.output.path = path_to_template(path, self.info.path)
        cfg.csv = CsvOptions(
            separator=self._separator(),
            include_header=self.header_check.isChecked(),
            float_precision=None if self.precision_spin.value() < 0 else self.precision_spin.value(),
            decimal_comma=self.decimal_comma.isChecked(),
            time_format=self.time_combo.currentData(),
            datetime_format=self.datetime_edit.text() or self.base.csv.datetime_format,
            null_value=self.null_edit.text(),
            quote_style=self.quote_combo.currentData(),
            line_terminator=self.eol_combo.currentData(),
            include_bom=self.bom_check.isChecked(),
            rename=self._renames(),
        )
        return cfg

    # -------------------------------------------------------------- reactions
    def _scope_changed(self) -> None:
        multiple = self.scope_ranges.isChecked() and len(self.base.ranges) > 1
        self.split_check.setEnabled(multiple)
        self._schedule_preview()

    def _agg_changed(self) -> None:
        self._fill_columns()
        self._schedule_preview()

    def _format_changed(self) -> None:
        self.sep_other.setVisible(self.sep_combo.currentData() is None)
        self.datetime_edit.setEnabled(self.time_combo.currentData() == "custom")
        self._schedule_preview()

    def _schedule_preview(self, *_args) -> None:
        cfg = self.config()
        template = cfg.output.path
        if cfg.output.split_ranges and "{range}" not in template:
            template += "  (+ _{range} per file)"
        self.template_label.setText(f"Saved configurations store this as: {template}")
        self._preview_timer.start()

    def _run_preview(self) -> None:
        cfg = self.config()
        self._generation += 1
        generation = self._generation
        info = self.info

        def job():
            validate_for(cfg, info)
            text = preview_csv(info, cfg)
            ranges = cfg.ranges[:1] if cfg.output.split_ranges else cfg.ranges
            estimate = estimate_rows(info, cfg, resolve_ranges(cfg, info, ranges))
            outputs = [p.path for p in plan_outputs(info, cfg)]
            return text, estimate, outputs

        workers.submit(job, lambda r: self._preview_done(generation, r), lambda e: self._preview_failed(generation, e))

    def _preview_done(self, generation: int, result) -> None:
        if generation != self._generation:
            return
        text, estimate, outputs = result
        self.preview.setPlainText(text.replace("\t", "→\t") if self._separator() == "\t" else text)
        self.preview_label.setText("Preview (first rows)")
        files = f"{len(outputs)} files" if len(outputs) > 1 else str(outputs[0])
        per = " per file" if len(outputs) > 1 else ""
        self.estimate_label.setText(f"≈ {estimate:,} rows{per} → {files}")
        self.export_button.setEnabled(not self._running)
        self.message.setText("")

    def _preview_failed(self, generation: int, exc: BaseException) -> None:
        if generation != self._generation:
            return
        self.preview.setPlainText("")
        self.estimate_label.setText("")
        self.preview_label.setText(f"<span style='color:#e34948'>{exc}</span>")
        self.export_button.setEnabled(False)

    def _browse(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(self, "Export CSV", self.path_edit.text(), "CSV files (*.csv);;All files (*)")
        if filename:
            self.path_edit.setText(filename)

    def _save_config(self) -> None:
        cfg = self.config()
        try:
            cfg.validate()
        except ConfigError as exc:
            QMessageBox.warning(self, "Save configuration", str(exc))
            return
        if save_config_interactive(self, self.settings, cfg, self.info):
            self.message.setText("Configuration saved")

    def _copy_command(self) -> None:
        cfg = self.config()
        output = cfg.output.path if cfg.output.path != "{stem}_export.csv" else None
        QGuiApplication.clipboard().setText(command_line([str(self.info.path)], cfg, output=output))
        self.message.setText("Command copied to clipboard")

    # ------------------------------------------------------------------ export
    def _export(self) -> None:
        cfg = self.config()
        try:
            validate_for(cfg, self.info)
            plans = plan_outputs(self.info, cfg)
        except (ProcessingError, ConfigError) as exc:
            QMessageBox.warning(self, "Export", str(exc))
            return
        existing = [p.path for p in plans if p.path is not None and p.path.exists()]
        if existing:
            names = "\n".join(str(p) for p in existing[:10])
            more = f"\n… and {len(existing) - 10} more" if len(existing) > 10 else ""
            answer = QMessageBox.question(self, "Overwrite files?", f"These files already exist:\n{names}{more}\n\nOverwrite them?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._set_running(True)
        info = self.info
        workers.submit(lambda: export_file(info, cfg), self._export_done, self._export_failed)

    def _set_running(self, running: bool) -> None:
        self._running = running
        for widget in (self.export_button, self.cancel_button, self.save_button):
            widget.setEnabled(not running)
        self.message.setText("Exporting…" if running else "")
        self.setCursor(Qt.CursorShape.BusyCursor if running else Qt.CursorShape.ArrowCursor)

    def _export_done(self, result) -> None:
        self._set_running(False)
        lines = [f"{path}  ({rows:,} rows)" for path, rows in result.outputs]
        box = QMessageBox(self)
        box.setWindowTitle("Export finished")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"Wrote {result.rows:,} rows to {len(result.outputs)} file(s).")
        box.setInformativeText("\n".join(lines))
        open_folder = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is open_folder and result.outputs:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.outputs[0][0].parent)))
        self.accept()

    def _export_failed(self, exc: BaseException) -> None:
        self._set_running(False)
        QMessageBox.critical(self, "Export failed", str(exc))
