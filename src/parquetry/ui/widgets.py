"""Small widgets shared by several panels and dialogs."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QColorDialog, QMenu, QToolButton, QWidget

from .theme import parse_color


def swatch(color: QColor, size: int = 14, automatic: bool = False) -> QIcon:
    """A square of *color*; an automatic colour is drawn as a smaller, framed square."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    inset = 3 if automatic else 0
    painter.setPen(QColor(128, 128, 128))
    painter.drawRect(0, 0, size - 1, size - 1)
    painter.fillRect(1 + inset, 1 + inset, size - 2 - 2 * inset, size - 2 - 2 * inset, color)
    painter.end()
    return QIcon(pixmap)


class ColorButton(QToolButton):
    """Shows a colour and picks another one; the menu returns to the automatic colour.

    ``color()`` is ``""`` while the automatic colour (given by
    :meth:`set_automatic`, e.g. the palette colour of a parameter) applies.
    """

    colorChanged = Signal(str)

    def __init__(self, color: str = "", automatic: QColor | str = "#808080", title: str = "Choose a colour",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self._automatic = QColor(automatic)
        self._title = title
        self.setIconSize(QSize(14, 14))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        menu = QMenu(self)
        menu.addAction("Choose…", self.choose)
        self.automatic_action = menu.addAction("Automatic", lambda: self.set_color("", emit=True))
        self.setMenu(menu)
        self.clicked.connect(self.choose)
        self._update()

    def color(self) -> str:
        return self._color

    def effective_color(self) -> QColor:
        return parse_color(self._color) or self._automatic

    def set_color(self, color: str, emit: bool = False) -> None:
        color = color if parse_color(color) else ""
        changed = color != self._color
        self._color = color
        self._update()
        if emit and changed:
            self.colorChanged.emit(color)

    def set_automatic(self, color: QColor | str) -> None:
        self._automatic = QColor(color)
        self._update()

    def choose(self) -> None:
        chosen = QColorDialog.getColor(self.effective_color(), self, self._title)
        if chosen.isValid():
            self.set_color(chosen.name(), emit=True)

    def _update(self) -> None:
        automatic = not self._color
        self.setIcon(swatch(self.effective_color(), automatic=automatic))
        self.automatic_action.setEnabled(not automatic)
        name = self.effective_color().name()
        self.setToolTip(f"Automatic colour ({name}); click to choose another" if automatic else f"{name}; click to change")
