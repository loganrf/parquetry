"""PyInstaller entry point of the windowed executable (always opens the UI)."""

import sys

from parquetry.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["ui", *sys.argv[1:]]))
