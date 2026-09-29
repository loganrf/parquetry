"""Inspection of Parquet files: schema, column roles and cheap metadata."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

#: Virtual column name that selects the row number as the x axis.
ROW_INDEX = "__row_index__"
ROW_INDEX_LABEL = "(row index)"

PARQUET_SUFFIXES = (".parquet", ".parq", ".pq")

_TIME_NAME = re.compile(r"(^|_)(time|timestamp|datetime|date|ts|t|epoch|utc)($|_)", re.IGNORECASE)


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    dtype: pl.DataType
    #: One of ``datetime``, ``date``, ``duration``, ``numeric``, ``boolean`` or ``other``.
    kind: str

    @property
    def dtype_name(self) -> str:
        return dtype_label(self.dtype)

    @property
    def plottable(self) -> bool:
        """Whether the column can be used as a Y parameter."""
        return self.kind in {"numeric", "boolean", "duration"}

    @property
    def x_candidate(self) -> bool:
        return self.kind in {"datetime", "date", "numeric", "duration"}

    @property
    def is_temporal(self) -> bool:
        return self.kind in {"datetime", "date"}

    @property
    def time_zone(self) -> str | None:
        return getattr(self.dtype, "time_zone", None)


def dtype_label(dtype: pl.DataType) -> str:
    """Compact dtype text such as ``Datetime[us, UTC]``."""
    if isinstance(dtype, pl.Datetime):
        tz = f", {dtype.time_zone}" if dtype.time_zone else ""
        return f"Datetime[{dtype.time_unit}{tz}]"
    if isinstance(dtype, pl.Duration):
        return f"Duration[{dtype.time_unit}]"
    return str(dtype)


def classify_dtype(dtype: pl.DataType) -> str:
    if isinstance(dtype, pl.Datetime):
        return "datetime"
    if dtype == pl.Date:
        return "date"
    if isinstance(dtype, pl.Duration):
        return "duration"
    if dtype == pl.Boolean:
        return "boolean"
    if dtype.is_numeric():
        return "numeric"
    return "other"


@dataclass
class DatasetInfo:
    path: Path
    size_bytes: int
    num_rows: int
    columns: list[ColumnInfo]
    num_row_groups: int | None = None
    created_by: str | None = None
    compression: str | None = None
    mtime: float = 0.0
    _by_name: dict[str, ColumnInfo] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_name = {c.name: c for c in self.columns}

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    def has_column(self, name: str) -> bool:
        return name == ROW_INDEX or name in self._by_name

    def column(self, name: str) -> ColumnInfo:
        if name == ROW_INDEX:
            return ColumnInfo(ROW_INDEX, pl.UInt32, "numeric")
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"Column {name!r} not found in {self.path.name}") from None

    def x_kind(self, name: str) -> str:
        """``datetime`` for temporal x columns, ``numeric`` otherwise."""
        return "datetime" if self.column(name).is_temporal else "numeric"

    @property
    def x_candidates(self) -> list[ColumnInfo]:
        return [c for c in self.columns if c.x_candidate]

    @property
    def y_candidates(self) -> list[ColumnInfo]:
        return [c for c in self.columns if c.plottable]

    def guess_x(self) -> str:
        """Pick a sensible default x axis: a timestamp column if there is one."""
        temporal = [c for c in self.columns if c.kind == "datetime"] or [
            c for c in self.columns if c.kind == "date"
        ]
        if temporal:
            named = [c for c in temporal if _TIME_NAME.search(c.name)]
            return (named or temporal)[0].name
        for c in self.columns:
            if c.kind in {"numeric", "duration"} and _TIME_NAME.search(c.name):
                return c.name
        return ROW_INDEX

    def default_y(self, x: str, count: int = 2) -> list[str]:
        return [c.name for c in self.y_candidates if c.name != x][:count]

    def cache_key(self) -> tuple:
        return (str(self.path), self.size_bytes, self.mtime)


def is_parquet_path(path: str | os.PathLike) -> bool:
    return str(path).lower().endswith(PARQUET_SUFFIXES)


def inspect_parquet(path: str | os.PathLike) -> DatasetInfo:
    """Read schema and footer metadata without loading any data."""
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"{path} is a directory, expected a Parquet file")
    stat = path.stat()
    try:
        schema = pl.read_parquet_schema(path)
        num_rows = pl.scan_parquet(path).select(pl.len()).collect().item()
    except Exception as exc:  # polars raises a variety of error types here
        raise ValueError(f"{path.name} is not a readable Parquet file: {exc}") from exc
    columns = [ColumnInfo(name, dtype, classify_dtype(dtype)) for name, dtype in schema.items()]
    info = DatasetInfo(
        path=path,
        size_bytes=stat.st_size,
        num_rows=int(num_rows),
        columns=columns,
        mtime=stat.st_mtime,
    )
    _add_pyarrow_metadata(info)
    return info


def _add_pyarrow_metadata(info: DatasetInfo) -> None:
    """Fill in optional details that polars does not expose (needs pyarrow)."""
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return
    try:
        meta = pq.ParquetFile(info.path).metadata
    except Exception:
        return
    info.num_row_groups = meta.num_row_groups
    info.created_by = meta.created_by
    if meta.num_row_groups and meta.num_columns:
        codecs = {meta.row_group(0).column(i).compression for i in range(meta.num_columns)}
        info.compression = ", ".join(sorted(str(c) for c in codecs))


def format_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
