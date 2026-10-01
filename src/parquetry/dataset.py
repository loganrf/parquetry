"""Inspection of data files (Parquet and CSV): schema, column roles and cheap metadata."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

#: Virtual column name that selects the row number as the x axis.
ROW_INDEX = "__row_index__"
ROW_INDEX_LABEL = "(row index)"

PARQUET_SUFFIXES = (".parquet", ".parq", ".pq")
CSV_SUFFIXES = (".csv", ".tsv")
#: Files that folders are searched for and that the file browser lists.
DATA_SUFFIXES = PARQUET_SUFFIXES + CSV_SUFFIXES

#: Rows of a CSV file used to guess the column types (the whole file is checked afterwards).
CSV_INFER_ROWS = 10_000
#: Cell texts read as missing values in CSV files, besides empty cells.
CSV_NULL_VALUES = ["NA", "N/A", "#N/A", "null", "NULL"]
_CSV_SEPARATORS = (",", ";", "\t", "|")
_CSV_SAMPLE_BYTES = 64 * 1024
_DECIMAL_COMMA = re.compile(r"^[+-]?\d+,\d+$")
_DECIMAL_POINT = re.compile(r"^[+-]?\d*\.\d+$")
_PARSE_ERROR = re.compile(r"could not parse `.*?` as dtype `[^`]*` at column '(.*?)' \(column number", re.DOTALL)

_TIME_NAME = re.compile(r"(^|_)(time|timestamp|datetime|date|ts|t|epoch|utc)($|_)", re.IGNORECASE)


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    dtype: pl.DataType
    #: One of ``datetime``, ``date``, ``duration``, ``numeric``, ``boolean``, ``text`` or ``other``.
    kind: str

    @property
    def dtype_name(self) -> str:
        return dtype_label(self.dtype)

    @property
    def plottable(self) -> bool:
        """Whether the column can be used as a Y parameter (text is shown as categories)."""
        return self.kind in {"numeric", "boolean", "duration", "text"}

    @property
    def x_candidate(self) -> bool:
        return self.kind in {"datetime", "date", "numeric", "duration", "text"}

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
    if dtype == pl.String or isinstance(dtype, (pl.Categorical, pl.Enum)):
        return "text"
    return "other"


@dataclass(frozen=True)
class CsvFormat:
    """How a CSV file is read (detected from the start of the file)."""

    separator: str = ","
    decimal_comma: bool = False

    def describe(self) -> str:
        name = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}.get(self.separator, repr(self.separator))
        return f"CSV ({name} separated{', decimal comma' if self.decimal_comma else ''})"


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
    #: How the file is read when it is a CSV file; ``None`` for Parquet.
    csv: CsvFormat | None = None
    _by_name: dict[str, ColumnInfo] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_name = {c.name: c for c in self.columns}

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def format(self) -> str:
        return "parquet" if self.csv is None else "csv"

    @property
    def format_label(self) -> str:
        return "Parquet" if self.csv is None else self.csv.describe()

    @property
    def text_columns(self) -> frozenset[str]:
        return frozenset(c.name for c in self.columns if c.kind == "text")

    def scan(self) -> pl.LazyFrame:
        """Lazy frame over the whole file (CSV files keep the inspected column types)."""
        if self.csv is None:
            return pl.scan_parquet(self.path)
        return scan_csv(self.path, self.csv, {c.name: c.dtype for c in self.columns})

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
        """``datetime`` for temporal x columns, ``category`` for text and ``numeric`` otherwise."""
        col = self.column(name)
        if col.is_temporal:
            return "datetime"
        return "category" if col.kind == "text" else "numeric"

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
        """The first *count* parameters, preferring numbers to text."""
        candidates = sorted((c for c in self.y_candidates if c.name != x), key=lambda c: c.kind == "text")
        return [c.name for c in candidates[:count]]

    def cache_key(self) -> tuple:
        return (str(self.path), self.size_bytes, self.mtime)


def is_parquet_path(path: str | os.PathLike) -> bool:
    return str(path).lower().endswith(PARQUET_SUFFIXES)


def is_data_path(path: str | os.PathLike) -> bool:
    """Whether *path* has the suffix of a Parquet or CSV file."""
    return str(path).lower().endswith(DATA_SUFFIXES)


def inspect_file(path: str | os.PathLike, *, verify: bool = True) -> DatasetInfo:
    """Read the schema and cheap metadata of a Parquet or CSV file.

    Parquet files only need their footer. For CSV files, the separator, the
    decimal mark and the column types are detected from the start of the file.
    With *verify* the whole file is then parsed once, and columns with values
    that do not fit the detected type are widened (integer, float, text), so
    that later processing cannot fail half-way through the file.
    """
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"No such file: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"{path} is a directory, expected a Parquet or CSV file")
    if is_parquet_path(path) or (not str(path).lower().endswith(CSV_SUFFIXES) and _has_parquet_magic(path)):
        return _inspect_parquet(path)
    return _inspect_csv(path, verify)


def inspect_parquet(path: str | os.PathLike) -> DatasetInfo:
    """Alias of :func:`inspect_file`, kept for compatibility."""
    return inspect_file(path)


def _has_parquet_magic(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(4) == b"PAR1"


def _inspect_parquet(path: Path) -> DatasetInfo:
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


def scan_csv(path: str | os.PathLike, fmt: CsvFormat, schema: Mapping[str, pl.DataType] | None = None) -> pl.LazyFrame:
    """Lazy frame over a CSV file; without *schema* the column types are inferred."""
    options = {
        "separator": fmt.separator,
        "decimal_comma": fmt.decimal_comma,
        "null_values": CSV_NULL_VALUES,
        "encoding": "utf8-lossy",
    }
    if schema is None:
        return pl.scan_csv(path, infer_schema_length=CSV_INFER_ROWS, try_parse_dates=True, **options)
    return pl.scan_csv(path, schema=dict(schema), **options)


def sniff_csv(path: str | os.PathLike) -> CsvFormat:
    """Guess the separator and decimal mark from the first lines of a CSV file."""
    path = Path(path)
    with open(path, "rb") as fh:
        sample = fh.read(_CSV_SAMPLE_BYTES)
    if b"\0" in sample:
        raise ValueError(f"{path.name} is neither a Parquet nor a CSV file")
    lines = sample.decode("utf-8", errors="replace").lstrip("\ufeff").splitlines()
    if len(sample) == _CSV_SAMPLE_BYTES and len(lines) > 1:
        lines.pop()  # probably cut off
    lines = [line for line in lines if line.strip()][:50]
    if path.suffix.lower() == ".tsv":
        separator = "\t"
    else:
        separator, best = ",", (False, 0)
        for candidate in _CSV_SEPARATORS:
            counts = [_count_unquoted(line, candidate) for line in lines]
            # Prefer a separator found the same number of times on every line, then the most columns.
            score = (all(c == counts[0] for c in counts), counts[0]) if counts and counts[0] else (False, 0)
            if score > best:
                separator, best = candidate, score
    fields = [f.strip().strip('"') for line in lines[1:] for f in line.split(separator)]
    decimal_comma = (
        separator != ","
        and any(_DECIMAL_COMMA.match(f) for f in fields)
        and not any(_DECIMAL_POINT.match(f) for f in fields)
    )
    return CsvFormat(separator, decimal_comma)


def _count_unquoted(line: str, separator: str) -> int:
    count, quoted = 0, False
    for char in line:
        if char == '"':
            quoted = not quoted
        elif char == separator and not quoted:
            count += 1
    return count


def _inspect_csv(path: Path, verify: bool) -> DatasetInfo:
    stat = path.stat()
    fmt = sniff_csv(path)
    try:
        schema = dict(scan_csv(path, fmt).collect_schema())
        if verify:
            num_rows = _verify_csv(path, fmt, schema)
        else:
            num_rows = scan_csv(path, fmt, schema).select(pl.len()).collect().item()
    except pl.exceptions.PolarsError as exc:
        message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise ValueError(f"{path.name} is not a readable CSV file: {message}") from exc
    columns = [ColumnInfo(name, dtype, classify_dtype(dtype)) for name, dtype in schema.items()]
    return DatasetInfo(
        path=path, size_bytes=stat.st_size, num_rows=int(num_rows), columns=columns, mtime=stat.st_mtime, csv=fmt
    )


def _verify_csv(path: Path, fmt: CsvFormat, schema: dict[str, pl.DataType]) -> int:
    """Parse every typed column of the file; widen *schema* where values do not fit. Returns the row count."""
    while True:
        typed = [name for name, dtype in schema.items() if dtype != pl.String]
        query = scan_csv(path, fmt, schema).select(
            pl.len().alias("\0rows"), *(pl.col(name).null_count().alias(f"\0{i}") for i, name in enumerate(typed))
        )
        try:
            return query.collect().item(0, 0)
        except pl.exceptions.ComputeError as exc:
            match = _PARSE_ERROR.search(str(exc))
            name = match.group(1) if match else None
            if name not in typed:
                raise
            schema[name] = pl.Float64 if schema[name].is_integer() else pl.String


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
