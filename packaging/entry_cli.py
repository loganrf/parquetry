"""PyInstaller entry point of the console executable (CLI + UI)."""

from parquetry.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
