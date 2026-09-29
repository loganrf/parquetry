"""Batch export: apply one configuration to many Parquet files."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..cli import command_line
from ..config import ConfigError, ProcessingConfig
from ..dataset import PARQUET_SUFFIXES
from ..processing import batch_export, find_inputs
from . import workers


def describe_config(cfg: ProcessingConfig) -> str:
    parts = [f"x = {cfg.x}", f"{len(cfg.y)} parameter(s): {', '.join(cfg.y[:6])}{' …' if len(cfg.y) > 6 else ''}"]
    parts.append(cfg.aggregation.describe())
    if cfg.ranges:
        parts.append(f"{len(cfg.ranges)} {cfg.range_mode} range(s)")
    else:
        parts.append("all data")
    return " · ".join(parts)


class BatchDialog(QDialog):
    def __init__(
        self,
        settings: QSettings,
        current: ProcessingConfig | None = None,
        inputs: list[Path] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Batch export")
        self.settings = settings
        self.current = current
        self.loaded: ProcessingConfig | None = None
        self._cancel = threading.Event()
        self._running = False

        # -- configuration --------------------------------------------------------
        self.use_current = QRadioButton("Current settings")
        self.use_current.setEnabled(current is not None)
        self.use_file = QRadioButton("Configuration file")
        group = QButtonGroup(self)
        group.addButton(self.use_current)
        group.addButton(self.use_file)
        (self.use_current if current is not None else self.use_file).setChecked(True)
        self.config_edit = QLineEdit()
        self.config_edit.setPlaceholderText("path to a saved .json configuration")
        self.config_edit.editingFinished.connect(self._load_config_file)
        browse_cfg = QPushButton("Browse…")
        browse_cfg.clicked.connect(self._browse_config)
        self.config_summary = QLabel()
        self.config_summary.setWordWrap(True)
        self.config_summary.setStyleSheet("color: palette(placeholder-text);")
        cfg_row = QHBoxLayout()
        cfg_row.addWidget(self.use_file)
        cfg_row.addWidget(self.config_edit, 1)
        cfg_row.addWidget(browse_cfg)
        cfg_box = QGroupBox("Configuration")
        cfg_layout = QVBoxLayout(cfg_box)
        cfg_layout.addWidget(self.use_current)
        cfg_layout.addLayout(cfg_row)
        cfg_layout.addWidget(self.config_summary)
        self.use_current.toggled.connect(self._update_summary)

        # -- inputs -----------------------------------------------------------------
        self.inputs = QListWidget()
        self.inputs.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        add_files = QPushButton("Add files…")
        add_files.clicked.connect(self._add_files)
        add_folder = QPushButton("Add folder…")
        add_folder.clicked.connect(self._add_folder)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_selected)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.inputs.clear)
        self.recursive = QCheckBox("Include sub-folders")
        in_buttons = QHBoxLayout()
        for w in (add_files, add_folder, self.recursive):
            in_buttons.addWidget(w)
        in_buttons.addStretch(1)
        in_buttons.addWidget(remove)
        in_buttons.addWidget(clear)
        in_box = QGroupBox("Input files")
        in_layout = QVBoxLayout(in_box)
        in_layout.addWidget(self.inputs, 1)
        in_layout.addLayout(in_buttons)

        # -- output -----------------------------------------------------------------
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("{stem}_export.csv")
        self.output_edit.setToolTip(
            "Output file template. Placeholders: {stem}, {name}, {parent}, {dir}, {range}.\n"
            "Relative paths are written next to each input file. Leave empty to use the configuration's setting."
        )
        out_folder = QPushButton("Folder…")
        out_folder.clicked.connect(self._choose_output_folder)
        out_row = QHBoxLayout()
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(out_folder)
        self.skip_existing = QCheckBox("Skip files whose output already exists")
        self.fail_fast = QCheckBox("Stop at the first error")
        out_box = QGroupBox("Output")
        out_form = QFormLayout(out_box)
        out_form.addRow("File name", out_row)
        out_form.addRow(self.skip_existing)
        out_form.addRow(self.fail_fast)

        # -- progress ---------------------------------------------------------------
        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.log.setMaximumBlockCount(5000)

        self.command_button = QPushButton("Copy CLI command")
        self.command_button.setToolTip("Copy the equivalent `parquetry export` command")
        self.command_button.clicked.connect(self._copy_command)
        self.run_button = QPushButton("Run")
        self.run_button.setDefault(True)
        self.run_button.clicked.connect(self._run)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self._close_or_cancel)
        buttons = QHBoxLayout()
        buttons.addWidget(self.command_button)
        buttons.addStretch(1)
        buttons.addWidget(self.run_button)
        buttons.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(cfg_box)
        layout.addWidget(in_box, 2)
        layout.addWidget(out_box)
        layout.addWidget(self.progress)
        layout.addWidget(self.log, 1)
        layout.addLayout(buttons)
        self.resize(820, 720)

        for path in inputs or []:
            self._add_paths([path])
        self._update_summary()

    # ---------------------------------------------------------------- config
    def config(self) -> ProcessingConfig | None:
        if self.use_current.isChecked():
            return self.current
        return self.loaded

    def _browse_config(self) -> None:
        start = self.settings.value("config/last_dir", str(Path.home()))
        filename, _ = QFileDialog.getOpenFileName(self, "Open configuration", start, "JSON configuration (*.json);;All files (*)")
        if filename:
            self.config_edit.setText(filename)
            self._load_config_file()

    def _load_config_file(self) -> None:
        text = self.config_edit.text().strip()
        if not text:
            return
        try:
            self.loaded = ProcessingConfig.load(text)
            self.loaded.validate()
        except ConfigError as exc:
            self.loaded = None
            self.config_summary.setText(f"<span style='color:#e34948'>{exc}</span>")
            return
        self.settings.setValue("config/last_dir", str(Path(text).parent))
        self.use_file.setChecked(True)
        self._update_summary()

    def _update_summary(self) -> None:
        cfg = self.config()
        self.config_summary.setText(describe_config(cfg) if cfg else "Choose a configuration file")

    # ---------------------------------------------------------------- inputs
    def _add_paths(self, paths) -> None:
        existing = {self.inputs.item(i).text() for i in range(self.inputs.count())}
        for path in find_inputs([str(p) for p in paths], recursive=self.recursive.isChecked()):
            if str(path) not in existing and path.suffix.lower() in PARQUET_SUFFIXES:
                self.inputs.addItem(str(path))
                existing.add(str(path))

    def _add_files(self) -> None:
        start = self.settings.value("browser/last_dir", str(Path.home()))
        files, _ = QFileDialog.getOpenFileNames(self, "Add Parquet files", start, "Parquet files (*.parquet *.parq *.pq);;All files (*)")
        self._add_paths(files)

    def _add_folder(self) -> None:
        start = self.settings.value("browser/last_dir", str(Path.home()))
        folder = QFileDialog.getExistingDirectory(self, "Add folder", start)
        if folder:
            before = self.inputs.count()
            self._add_paths([folder])
            if self.inputs.count() == before:
                QMessageBox.information(self, "Add folder", "No Parquet files found in that folder.")

    def _remove_selected(self) -> None:
        for item in self.inputs.selectedItems():
            self.inputs.takeItem(self.inputs.row(item))

    def _choose_output_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Output folder", str(Path.home()))
        if folder:
            cfg = self.config()
            name = Path(self.output_edit.text().strip() or (cfg.output.path if cfg else "{stem}_export.csv")).name
            self.output_edit.setText(str(Path(folder) / name))

    def _input_paths(self) -> list[Path]:
        return [Path(self.inputs.item(i).text()) for i in range(self.inputs.count())]

    # ------------------------------------------------------------------- run
    def _copy_command(self) -> None:
        cfg = self.config()
        if cfg is None:
            return
        inputs = [str(p) for p in self._input_paths()] or ["*.parquet"]
        path = self.config_edit.text().strip() if self.use_file.isChecked() else None
        output = self.output_edit.text().strip() or None
        QGuiApplication.clipboard().setText(command_line(inputs, cfg, config_path=path, output=output))
        self.log.appendPlainText("Command copied to clipboard.")

    def _run(self) -> None:
        cfg = self.config()
        if cfg is None:
            QMessageBox.warning(self, "Batch export", "Choose a valid configuration first.")
            return
        inputs = self._input_paths()
        if not inputs:
            QMessageBox.warning(self, "Batch export", "Add at least one input file.")
            return
        template = self.output_edit.text().strip() or None
        if template and len(inputs) > 1 and not any(p in template for p in ("{stem}", "{name}", "{parent}")):
            QMessageBox.warning(self, "Batch export", "With several inputs the file name must contain {stem}, {name} or {parent}.")
            return
        self._cancel.clear()
        self._set_running(True)
        self.progress.setRange(0, len(inputs))
        self.progress.setValue(0)
        self.log.appendPlainText(f"Exporting {len(inputs)} file(s): {describe_config(cfg)}")
        overwrite = not self.skip_existing.isChecked()
        fail_fast = self.fail_fast.isChecked()
        cancel = self._cancel

        def job(report):
            ok = failed = 0
            for item in batch_export(inputs, cfg, output=template, overwrite=overwrite, fail_fast=fail_fast):
                if item.error:
                    failed += 1
                else:
                    ok += 1
                report(item)
                if cancel.is_set():
                    break
            return ok, failed

        workers.submit(job, self._finished, self._failed, self._item_done)

    def _item_done(self, item) -> None:
        self.progress.setValue(self.progress.value() + 1)
        if item.error:
            self.log.appendPlainText(f"✗ {item.input}: {item.error}")
            return
        for path, rows in item.result.outputs:
            self.log.appendPlainText(f"✓ {item.input.name} → {path}  ({rows:,} rows)")
        for path in item.result.skipped:
            self.log.appendPlainText(f"• {item.input.name} → {path}  (exists, skipped)")

    def _finished(self, counts) -> None:
        ok, failed = counts
        self._set_running(False)
        stopped = " (stopped)" if self._cancel.is_set() else ""
        self.log.appendPlainText(f"Done{stopped}: {ok} succeeded, {failed} failed.\n")

    def _failed(self, exc: BaseException) -> None:
        self._set_running(False)
        self.log.appendPlainText(f"Batch export failed: {exc}")

    def _set_running(self, running: bool) -> None:
        self._running = running
        self.run_button.setEnabled(not running)
        self.close_button.setText("Stop" if running else "Close")

    def _close_or_cancel(self) -> None:
        if self._running:
            self._cancel.set()
            self.log.appendPlainText("Stopping after the current file…")
        else:
            self.accept()

    def reject(self) -> None:  # Escape / window close
        if self._running:
            self._close_or_cancel()
            return
        super().reject()

