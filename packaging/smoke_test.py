"""Check that a PyInstaller bundle actually works before it is released.

Usage: python packaging/smoke_test.py [DIST_DIR]

Runs the bundled CLI (version, sample data, info, export) and starts the UI
headless for a few seconds with both executables. On macOS it also checks the
app's Info.plist.
"""

from __future__ import annotations

import csv
import os
import plistlib
import subprocess
import sys
import tempfile
from pathlib import Path


def executables(dist: Path) -> tuple[Path, Path]:
    exe = ".exe" if sys.platform == "win32" else ""
    if sys.platform == "darwin":
        folder = dist / "Parquetry.app" / "Contents" / "MacOS"
    else:
        folder = dist / "parquetry"
    cli, gui = folder / f"parquetry{exe}", folder / f"parquetry-gui{exe}"
    for path in (cli, gui):
        if not path.is_file():
            raise SystemExit(f"missing executable: {path}")
    return cli, gui


def check_info_plist(dist: Path) -> None:
    """A background-only app gets no Dock icon or menu bar, and no keyboard input."""
    contents = dist / "Parquetry.app" / "Contents"
    with open(contents / "Info.plist", "rb") as fh:
        info = plistlib.load(fh)
    for key in ("LSBackgroundOnly", "LSUIElement"):
        if info.get(key) not in (None, False, 0, "0"):
            raise SystemExit(f"Info.plist sets {key}={info[key]!r}")
    if not (contents / "MacOS" / info.get("CFBundleExecutable", "")).is_file():
        raise SystemExit(f"Info.plist names a missing executable: {info.get('CFBundleExecutable')!r}")


def run(*args: str | Path, cwd: Path, timeout: int = 180) -> str:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    print("$", " ".join(str(a) for a in args), flush=True)
    result = subprocess.run([str(a) for a in args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    output = result.stdout + result.stderr
    print(output, flush=True)
    if result.returncode != 0:
        raise SystemExit(f"command failed with exit code {result.returncode}")
    for problem in ("Traceback", "Cannot open file", "Could not load the Qt platform plugin"):
        if problem in output:
            raise SystemExit(f"unexpected output: {problem!r}")
    return result.stdout


def main() -> None:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else "dist").resolve()
    cli, gui = executables(dist)
    if sys.platform == "darwin":
        check_info_plist(dist)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        assert "parquetry" in run(cli, "--version", cwd=work)
        run(cli, "sample", "sample.parquet", "--rows", "20000", cwd=work)
        assert "20,000" in run(cli, "info", "sample.parquet", cwd=work)
        run(
            cli, "export", "sample.parquet", "--y", "altitude_m", "engine_rpm", "--agg", "interval",
            "--every", "10s", "--func", "mean", "max", "--range", "0", "60", "--range-mode", "relative",
            "-o", "out.csv", cwd=work,
        )
        with open(work / "out.csv", newline="") as fh:
            rows = list(csv.reader(fh))
        assert rows[0] == ["timestamp", "altitude_m_mean", "altitude_m_max", "engine_rpm_mean", "engine_rpm_max"], rows[0]
        assert len(rows) == 8, len(rows)  # header + 7 buckets covering 0-60 s
        run(cli, "ui", "sample.parquet", "--quit-after", "3", cwd=work)
        run(gui, "--quit-after", "3", cwd=work)
    print("smoke test passed")


if __name__ == "__main__":
    main()
