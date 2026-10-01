"""Parquetry - explore, aggregate and export Parquet data.

The package is split into a UI-independent core (``dataset``, ``config``,
``processing``) that powers both the command line interface and the Qt desktop
application in :mod:`parquetry.ui`.
"""

from .config import AggregationConfig, CsvOptions, OutputOptions, ProcessingConfig, TimeRange
from .dataset import ROW_INDEX, ColumnInfo, DatasetInfo, inspect_parquet
from .processing import ExportResult, ProcessingError, export_file, load_plot_data, preview_csv

__version__ = "0.1.2"

__all__ = [
    "ROW_INDEX",
    "AggregationConfig",
    "ColumnInfo",
    "CsvOptions",
    "DatasetInfo",
    "ExportResult",
    "OutputOptions",
    "ProcessingConfig",
    "ProcessingError",
    "TimeRange",
    "__version__",
    "export_file",
    "inspect_parquet",
    "load_plot_data",
    "preview_csv",
]
