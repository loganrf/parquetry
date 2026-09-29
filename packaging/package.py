"""Turn the PyInstaller output in dist/ into release archives in artifacts/.

Usage: python packaging/package.py [DIST_DIR] [OUT_DIR]

* Linux:   parquetry-<version>-linux-<arch>.tar.gz and parquetry_<version>_<debarch>.deb
* Windows: parquetry-<version>-windows-<arch>.zip
* macOS:   parquetry-<version>-macos-<arch>.dmg (drag Parquetry.app to Applications)
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def version() -> str:
    text = (ROOT / "src" / "parquetry" / "__init__.py").read_text()
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)


def package_linux(dist: Path, out: Path, ver: str) -> list[Path]:
    bundle = dist / "parquetry"
    tarball = shutil.make_archive(str(out / f"parquetry-{ver}-linux-{arch()}"), "gztar", dist, "parquetry")
    deb_arch = {"x86_64": "amd64", "arm64": "arm64"}.get(arch(), arch())
    deb = out / f"parquetry_{ver}_{deb_arch}.deb"
    subprocess.run(["bash", str(ROOT / "packaging" / "linux" / "build_deb.sh"), ver, str(bundle), str(deb)], check=True)
    return [Path(tarball), deb]


def package_windows(dist: Path, out: Path, ver: str) -> list[Path]:
    archive = shutil.make_archive(str(out / f"parquetry-{ver}-windows-{arch()}"), "zip", dist, "parquetry")
    return [Path(archive)]


def package_macos(dist: Path, out: Path, ver: str) -> list[Path]:
    app = dist / "Parquetry.app"
    dmg = out / f"parquetry-{ver}-macos-{arch()}.dmg"
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "Parquetry"
        staging.mkdir()
        # ditto keeps the bundle's symlinks, permissions and code signatures intact
        subprocess.run(["ditto", str(app), str(staging / "Parquetry.app")], check=True)
        os.symlink("/Applications", staging / "Applications")
        subprocess.run(
            ["hdiutil", "create", "-volname", f"Parquetry {ver}", "-srcfolder", str(staging),
             "-fs", "HFS+", "-format", "UDZO", "-ov", str(dmg)],
            check=True,
        )
    return [dmg]


def main() -> None:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "artifacts").resolve()
    out.mkdir(parents=True, exist_ok=True)
    ver = version()
    if sys.platform.startswith("linux"):
        files = package_linux(dist, out, ver)
    elif sys.platform == "win32":
        files = package_windows(dist, out, ver)
    elif sys.platform == "darwin":
        files = package_macos(dist, out, ver)
    else:
        raise SystemExit(f"unsupported platform {sys.platform}")
    for path in files:
        print(f"{path}  ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
