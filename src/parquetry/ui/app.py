"""Application entry point for the desktop UI."""

from __future__ import annotations

import sys
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


class FileOpenHandler(QObject):
    """Opens files handed over by the OS (macOS Finder "Open With", double-click)."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt naming
        if event.type() == QEvent.Type.FileOpen and event.file():
            self._window.open_file(Path(event.file()))
            return True
        return super().eventFilter(obj, event)


def create_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        QApplication.setApplicationName("Parquetry")
        QApplication.setOrganizationName("Parquetry")
        app = QApplication(sys.argv[:1])
    # segmentedLineMode avoids Qt dropping sub-pixel segments of dense, aliased lines.
    app.setWindowIcon(QIcon(str(Path(__file__).with_name("icon.svg"))))
    pg.setConfigOptions(antialias=False, segmentedLineMode="on", exitCleanup=False)
    return app


def run(path: str | None = None, config: str | None = None, quit_after: float | None = None) -> int:
    """Start the UI, optionally opening *path* (and applying *config*) directly.

    *quit_after* closes the window after that many seconds; release builds use
    it to check that the bundled Qt libraries load.
    """
    app = create_app()
    window = MainWindow()
    app.installEventFilter(FileOpenHandler(window))
    window.show()
    if path:
        window.open_file(Path(path), config)
    elif config and sys.stderr is not None:  # no stderr in windowed builds
        print("warning: --config needs a file to apply it to; ignoring", file=sys.stderr)
    if quit_after is not None:
        QTimer.singleShot(int(quit_after * 1000), window.close)
    return app.exec()


def main() -> None:
    """Entry point of the ``parquetry-gui`` launcher."""
    from ..cli import main as cli_main

    raise SystemExit(cli_main(["ui", *sys.argv[1:]]))
