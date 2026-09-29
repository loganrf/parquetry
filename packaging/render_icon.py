"""Render the SVG application icon to a PNG (PyInstaller turns it into .ico/.icns).

Usage: python packaging/render_icon.py OUTPUT.png [SIZE]
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

SVG = Path(__file__).resolve().parents[1] / "src" / "parquetry" / "ui" / "icon.svg"


def render(output: Path, size: int = 1024) -> Path:
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841 - needed for painting
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(SVG)).render(painter)
    painter.end()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(output)):
        raise SystemExit(f"could not write {output}")
    return output


if __name__ == "__main__":
    out = render(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 1024)
    print(out)
