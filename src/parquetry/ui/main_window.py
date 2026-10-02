"""Main application window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QFontDatabase, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QStackedWidget,
    QVBoxLayout,
)

from .. import __version__
from ..cli import build_parser, export_parser
from ..config import ConfigError, ProcessingConfig
from ..dataset import DatasetInfo, inspect_file
from ..processing import ProcessingError
from . import workers
from .batch_dialog import BatchDialog
from .explorer import ExplorerPage
from .export_dialog import ExportDialog, save_config_interactive
from .file_browser import FileBrowserPage, add_recent_file, recent_files


class MainWindow(QMainWindow):
    def __init__(self, settings: QSettings | None = None) -> None:
        super().__init__()
        self.settings = settings or QSettings()
        self.setWindowTitle("Parquetry")
        self.setAcceptDrops(True)

        self.browser = FileBrowserPage(self.settings)
        self.explorer = ExplorerPage(self.settings)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.browser)
        self.stack.addWidget(self.explorer)
        self.setCentralWidget(self.stack)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setMaximumWidth(160)
        self.progress.hide()
        self.busy_label = QLabel()
        self.statusBar().addPermanentWidget(self.busy_label)
        self.statusBar().addPermanentWidget(self.progress)

        self.browser.fileChosen.connect(lambda path: self.open_file(path))
        self.browser.backRequested.connect(self.show_explorer)
        self.explorer.openRequested.connect(self.show_browser)
        self.explorer.busy.connect(self._set_busy)
        self.explorer.status.connect(lambda text: self.statusBar().showMessage(text, 0))
        self.explorer.error.connect(lambda title, text: QMessageBox.warning(self, title, text))
        self.explorer.act_load_config.triggered.connect(self.load_config)
        self.explorer.act_save_config.triggered.connect(self.save_config)
        self.explorer.act_export.triggered.connect(self.export_csv)

        self._build_menus()
        geometry = self.settings.value("window/geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1360, 860)
        self.show_browser()

    # ------------------------------------------------------------------ menus
    def _build_menus(self) -> None:
        ex = self.explorer
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(ex.act_open)
        self.recent_menu = file_menu.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        file_menu.addSeparator()
        file_menu.addAction(ex.act_load_config)
        file_menu.addAction(ex.act_save_config)
        file_menu.addSeparator()
        file_menu.addAction(ex.act_export)
        file_menu.addAction(ex.act_export_image)
        file_menu.addAction(ex.act_copy_image)
        self.act_batch = QAction("&Batch export…", self)
        self.act_batch.setShortcut(QKeySequence("Ctrl+B"))
        self.act_batch.setStatusTip("Apply a configuration to many Parquet or CSV files")
        self.act_batch.triggered.connect(self.batch_export)
        file_menu.addAction(self.act_batch)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("&View")
        for action in (ex.act_stacked, ex.act_points, ex.act_auto_y, ex.act_reset, ex.act_detail):
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(ex.act_plot_settings)
        ranges_menu = self.menuBar().addMenu("&Ranges")
        for action in (ex.act_select, ex.act_add_range, ex.act_zoom_ranges):
            ranges_menu.addAction(action)
        clear = QAction("Clear ranges", self)
        clear.triggered.connect(ex.plot.clear_ranges)
        ranges_menu.addAction(clear)

        help_menu = self.menuBar().addMenu("&Help")
        cli_help = QAction("Command line usage", self)
        cli_help.triggered.connect(self.show_cli_help)
        help_menu.addAction(cli_help)
        about = QAction("About Parquetry", self)
        about.triggered.connect(self.show_about)
        help_menu.addAction(about)
        # Actions that only make sense while a file is shown (several have single-key shortcuts).
        self._file_actions = [
            ex.act_load_config, ex.act_save_config, ex.act_export, ex.act_select, ex.act_add_range,
            ex.act_zoom_ranges, ex.act_stacked, ex.act_points, ex.act_auto_y, ex.act_reset, clear,
            ex.act_plot_settings, ex.act_export_image, ex.act_copy_image,
        ]

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        paths = recent_files(self.settings)
        for path in paths:
            action = self.recent_menu.addAction(path)
            action.setEnabled(Path(path).exists())
            action.triggered.connect(lambda _checked=False, p=path: self.open_file(p))
        if not paths:
            self.recent_menu.addAction("(none)").setEnabled(False)

    # ------------------------------------------------------------------ pages
    def show_browser(self) -> None:
        info = self.explorer.info
        self.browser.set_can_go_back(info is not None, info.path.name if info else "")
        if info is not None:
            self.browser.reveal(info.path)
        self.browser.refresh_recent()
        self.stack.setCurrentWidget(self.browser)
        self.explorer.toolbar.setEnabled(False)
        for action in [*self._file_actions, self.explorer.act_detail]:
            action.setEnabled(False)
        self.setWindowTitle("Parquetry")

    def show_explorer(self) -> None:
        if self.explorer.info is None:
            return
        self.stack.setCurrentWidget(self.explorer)
        self.explorer.toolbar.setEnabled(True)
        for action in self._file_actions:
            action.setEnabled(True)
        self.explorer.update_actions()
        self.setWindowTitle(f"{self.explorer.info.path.name} – Parquetry")

    # ------------------------------------------------------------------ files
    def open_file(self, path: str | Path, config_path: str | Path | None = None) -> None:
        """Inspect *path* in the background and show it in the explorer."""
        path = Path(path).expanduser()
        self._set_busy(True, f"Opening {path.name}…")

        def job():
            info = inspect_file(path)
            cfg = ProcessingConfig.load(config_path) if config_path else None
            return info, cfg

        workers.submit(job, self._file_opened, lambda exc: self._open_failed(path, exc))

    def _file_opened(self, result: tuple[DatasetInfo, ProcessingConfig | None]) -> None:
        info, cfg = result
        self._set_busy(False, "")
        add_recent_file(self.settings, info.path)
        try:
            warnings = self.explorer.set_dataset(info, cfg)
        except (ProcessingError, ConfigError) as exc:
            QMessageBox.warning(self, "Configuration", f"The configuration could not be applied:\n{exc}")
            warnings = []
            self.explorer.set_dataset(info)
        self.show_explorer()
        if warnings:
            QMessageBox.information(self, "Configuration", "\n".join(warnings))

    def _open_failed(self, path: Path, exc: BaseException) -> None:
        self._set_busy(False, "")
        QMessageBox.warning(self, "Cannot open file", f"{path}\n\n{exc}")
        self.show_browser()

    def load_config(self) -> None:
        if self.explorer.info is None:
            return
        start = self.settings.value("config/last_dir", str(self.explorer.info.path.parent))
        filename, _ = QFileDialog.getOpenFileName(self, "Load configuration", start, "JSON configuration (*.json);;All files (*)")
        if not filename:
            return
        try:
            cfg = ProcessingConfig.load(filename)
            warnings = self.explorer.apply_config(cfg)
        except (ConfigError, ProcessingError) as exc:
            QMessageBox.warning(self, "Load configuration", str(exc))
            return
        self.settings.setValue("config/last_dir", str(Path(filename).parent))
        self.statusBar().showMessage(f"Applied {Path(filename).name}", 5000)
        if warnings:
            QMessageBox.information(self, "Load configuration", "\n".join(warnings))

    def save_config(self) -> None:
        if self.explorer.info is None:
            return
        cfg = self.explorer.current_config()
        try:
            cfg.validate()
        except ConfigError as exc:
            QMessageBox.warning(self, "Save configuration", str(exc))
            return
        save_config_interactive(self, self.settings, cfg, self.explorer.info)

    def export_csv(self) -> None:
        info = self.explorer.info
        if info is None:
            return
        cfg = self.explorer.current_config()
        if not cfg.y:
            QMessageBox.information(self, "Export CSV", "Select at least one parameter to export.")
            return
        dialog = ExportDialog(info, cfg, self.settings, self)
        dialog.finished.connect(lambda _result: self.explorer.remember_export_settings(dialog.config()))
        dialog.open()

    def batch_export(self) -> None:
        current = None
        if self.explorer.info is not None:
            cfg = self.explorer.current_config()
            if cfg.y:
                current = cfg
        dialog = BatchDialog(self.settings, current, parent=self)
        dialog.open()

    # ------------------------------------------------------------------- misc
    def _set_busy(self, busy: bool, text: str) -> None:
        self.progress.setVisible(busy)
        self.busy_label.setText(text if busy else "")

    def show_cli_help(self) -> None:
        text = (
            "Everything in this window can be scripted. Save a configuration (File ▸ Save config…) and\n"
            "apply it to other files with `parquetry export FILES --config CONFIG.json`.\n\n"
            + build_parser().format_help()
            + "\n\n"
            + export_parser().format_help()
        )
        dialog = QDialog(self)
        dialog.setWindowTitle("Command line usage")
        edit = QPlainTextEdit(text)
        edit.setReadOnly(True)
        edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        edit.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(edit)
        layout.addWidget(buttons)
        dialog.resize(860, 640)
        dialog.exec()

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Parquetry",
            f"<h3>Parquetry {__version__}</h3>"
            "<p>Explore, aggregate and export Parquet and CSV files.</p>"
            "<p>Built on polars, PySide6 and pyqtgraph.</p>",
        )

    # ----------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt naming
        urls = event.mimeData().urls()
        if urls and urls[0].isLocalFile():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt naming
        urls = event.mimeData().urls()
        if not urls:
            return
        path = Path(urls[0].toLocalFile())
        if path.suffix.lower() == ".json" and self.explorer.info is not None:
            try:
                warnings = self.explorer.apply_config(ProcessingConfig.load(path))
            except (ConfigError, ProcessingError) as exc:
                QMessageBox.warning(self, "Load configuration", str(exc))
                return
            if warnings:
                QMessageBox.information(self, "Load configuration", "\n".join(warnings))
        elif path.is_dir():
            self.browser.set_directory(path)
            self.show_browser()
        else:
            self.open_file(path)
        event.acceptProposedAction()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        self.settings.setValue("window/geometry", self.saveGeometry())
        workers.wait_all(5000)
        super().closeEvent(event)

