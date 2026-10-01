# PyInstaller spec for the Parquetry desktop bundles.
#
# Produces one folder ("onedir") containing two executables that share the
# same libraries:
#   parquetry      console program: CLI, and `parquetry ui` for the UI
#   parquetry-gui  windowed launcher for the UI (no console window on Windows)
# (Distinct names matter: Windows and macOS file systems ignore case.)
# On macOS the folder is additionally wrapped into Parquetry.app.
#
# Build with:  pyinstaller packaging/parquetry.spec --noconfirm
#
# macOS signing: set MACOS_CODESIGN_IDENTITY to a "Developer ID Application"
# identity (its name or SHA-1 hash) to sign every binary with the hardened
# runtime, as notarization requires. Without it the bundle is signed ad hoc.
# See docs/macos-notarization.md.

import os
import re
import subprocess
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "src/parquetry/__init__.py").read_text()).group(1)

CODESIGN_IDENTITY = os.environ.get("MACOS_CODESIGN_IDENTITY") or None
ENTITLEMENTS = str(ROOT / "packaging" / "macos" / "entitlements.plist") if CODESIGN_IDENTITY else None
if CODESIGN_IDENTITY:
    # By default PyInstaller only warns when it cannot sign the .app; fail
    # instead, and verify the finished bundle's signature.
    os.environ["PYINSTALLER_STRICT_BUNDLE_CODESIGN_ERROR"] = "1"
    os.environ["PYINSTALLER_VERIFY_BUNDLE_SIGNATURE"] = "1"

# The icon is rendered from the SVG at build time; PyInstaller converts the PNG
# to .ico/.icns when Pillow is installed.
ICON = Path(workpath) / "parquetry-icon.png"
try:
    subprocess.run([sys.executable, str(ROOT / "packaging" / "render_icon.py"), str(ICON)], check=True)
    import PIL  # noqa: F401
except Exception as exc:
    print(f"WARNING: building without an application icon ({exc})")
    ICON = None

hiddenimports = ["parquetry.ui.app", *collect_submodules("parquetry")]
for runtime in ("_polars_runtime_32", "_polars_runtime_64", "_polars_runtime_compat"):
    try:
        hiddenimports += collect_submodules(runtime)
    except Exception:
        pass

# pyarrow only adds optional metadata (row groups, compression) but ~150 MB.
excludes = [
    "pyarrow", "tkinter", "matplotlib", "scipy", "IPython", "jupyter", "notebook", "pandas", "PyQt5",
    "PyQt6", "PySide2", "OpenGL", "pytest", "pytestqt", "pyqtgraph.opengl", "pyqtgraph.examples",
]

def analysis(script):
    return Analysis(
        [str(ROOT / "packaging" / script)],
        pathex=[str(ROOT / "src")],
        datas=[(str(ROOT / "src" / "parquetry" / "ui" / "icon.svg"), "parquetry/ui")],
        hiddenimports=hiddenimports,
        excludes=excludes,
        noarchive=False,
    )

cli = analysis("entry_cli.py")
gui = analysis("entry_gui.py")

cli_exe = EXE(
    PYZ(cli.pure),
    cli.scripts,
    [],
    exclude_binaries=True,
    name="parquetry",
    console=True,
    upx=False,
    icon=str(ICON) if ICON else None,
    codesign_identity=CODESIGN_IDENTITY,
    entitlements_file=ENTITLEMENTS,
)
gui_exe = EXE(
    PYZ(gui.pure),
    gui.scripts,
    [],
    exclude_binaries=True,
    name="parquetry-gui",
    console=False,
    upx=False,
    icon=str(ICON) if ICON else None,
    codesign_identity=CODESIGN_IDENTITY,
    entitlements_file=ENTITLEMENTS,
)

coll = COLLECT(
    gui_exe,
    cli_exe,
    gui.binaries + cli.binaries,
    gui.datas + cli.datas,
    upx=False,
    name="parquetry",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Parquetry.app",
        icon=str(ICON) if ICON else None,
        bundle_identifier="io.github.parquetry",
        version=VERSION,
        info_plist={
            "CFBundleName": "Parquetry",
            "CFBundleDisplayName": "Parquetry",
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            # BUNDLE takes `console` from the last EXE in COLLECT (the console CLI)
            # and then marks the app background-only: no Dock icon, no menu bar, and
            # keyboard input keeps going to the previously active app.
            "LSBackgroundOnly": False,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleDocumentTypes": [
                {
                    "CFBundleTypeName": "Apache Parquet file",
                    "CFBundleTypeRole": "Viewer",
                    "LSHandlerRank": "Alternate",
                    "CFBundleTypeExtensions": ["parquet", "parq", "pq"],
                },
                {
                    "CFBundleTypeName": "CSV file",
                    "CFBundleTypeRole": "Viewer",
                    "LSHandlerRank": "Alternate",
                    "CFBundleTypeExtensions": ["csv", "tsv"],
                },
            ],
        },
    )
