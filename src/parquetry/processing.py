"""The processing engine: filtering, aggregation, CSV export and plot data.

Everything here is lazy polars under the hood, so only the columns that are
needed are read, range filters are pushed down into the Parquet scan and
results are streamed to disk where possible. The same functions back the
command line interface and the desktop UI, which guarantees that a saved
configuration produces exactly what the UI showed.
"""

from __future__ import annotations

import datetime as dt
import glob
import inspect
import math
import operator
import os
import re
import sys
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import reduce
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

from .config import LINE_TERMINATORS, ConfigError, ProcessingConfig, TimeRange
from .dataset import PARQUET_SUFFIXES, ROW_INDEX, DatasetInfo, inspect_parquet
from .durations import is_duration, parse_duration_us, parse_offset_seconds, to_polars_duration

UTC = dt.timezone.utc
_EPOCH = dt.datetime(1970, 1, 1)
_BUCKET = "__bucket__"
ROW_INDEX_HEADER = "row_index"

_WRITE_CSV_PARAMS = frozenset(inspect.signature(pl.DataFrame.write_csv).parameters)
_SINK_CSV_PARAMS = frozenset(inspect.signature(pl.LazyFrame.sink_csv).parameters)
_MAP_BATCHES_PARAMS = frozenset(inspect.signature(pl.Expr.map_batches).parameters)

_AGG_EXPRS = {
    "mean": lambda e: e.mean(),
    "min": lambda e: e.min(),
    "max": lambda e: e.max(),
    "median": lambda e: e.median(),
    "first": lambda e: e.first(),
    "last": lambda e: e.last(),
    "sum": lambda e: e.sum(),
    "count": lambda e: e.count(),
    "std": lambda e: e.std(),
}


class ProcessingError(ValueError):
    """A configuration cannot be applied to a particular file."""


@dataclass(frozen=True)
class Bounds:
    """Minimum and maximum x value (python ``datetime`` or number)."""

    lo: Any
    hi: Any


Resolved = list[tuple[Any, Any]]

# ---------------------------------------------------------------------------
# x value conversions
# ---------------------------------------------------------------------------


def to_epoch_us(value: dt.datetime) -> int:
    """Microseconds since the Unix epoch. Naive datetimes are treated as UTC."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    delta = value - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def from_epoch_us(us: int, tz: str | None = None) -> dt.datetime:
    value = _EPOCH + dt.timedelta(microseconds=int(us))
    if tz:
        value = value.replace(tzinfo=UTC).astimezone(ZoneInfo(tz))
    return value


def x_to_plot(value: Any, kind: str) -> float:
    """Convert an x value to the float used on the plot (epoch seconds for datetimes)."""
    if kind == "datetime":
        return to_epoch_us(value) / 1e6
    return float(value)


def plot_to_x(value: float, kind: str, tz: str | None = None) -> Any:
    if kind == "datetime":
        return from_epoch_us(round(value * 1e6), tz)
    return float(value)


def format_x(value: Any, kind: str) -> str:
    """Canonical text for an x value, as stored in configurations."""
    if value is None:
        return ""
    if kind == "datetime":
        return value.isoformat(sep="T", timespec="microseconds" if value.microsecond else "seconds")
    value = float(value)
    return str(int(value)) if value.is_integer() else repr(value)


_ISO = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?\s*(Z|[+-]\d{2}(?::?\d{2})?)?$",
    re.IGNORECASE,
)


def _parse_iso(text: str) -> dt.datetime:
    """ISO 8601 date/time, including forms Python 3.10's fromisoformat rejects
    (e.g. ``12:00:00.1``, ``Z`` suffix, ``+0100`` offsets)."""
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        match = _ISO.match(text)
        if not match:
            raise
    year, month, day, hour, minute, second, fraction, offset = match.groups()
    tzinfo = None
    if offset:
        if offset.upper() == "Z":
            tzinfo = UTC
        else:
            digits = offset[1:].replace(":", "").ljust(4, "0")
            delta = dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            tzinfo = dt.timezone(-delta if offset[0] == "-" else delta)
    return dt.datetime(
        int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0),
        int((fraction or "0")[:6].ljust(6, "0")), tzinfo=tzinfo,
    )


def parse_x_value(value: Any, kind: str, tz: str | None = None) -> Any:
    """Parse an absolute range bound for an x column of the given kind."""
    if kind != "datetime":
        if isinstance(value, dt.datetime):
            raise ProcessingError(f"Range bound {value!s} is a date/time but the x axis is numeric")
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ProcessingError(f"Range bound {value!r} is not a number (the x axis is numeric)") from None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        return from_epoch_us(round(value * 1e6), tz)  # epoch seconds
    else:
        try:
            parsed = _parse_iso(str(value).strip())
        except ValueError:
            raise ProcessingError(
                f"Cannot parse {value!r} as a date/time; use ISO 8601 such as 2024-01-31T12:00:00"
            ) from None
    if tz is None:
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    elif parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(tz))
    else:
        parsed = parsed.astimezone(ZoneInfo(tz))
    return parsed


def _offset(base: Any, amount: float, kind: str, tz: str | None) -> Any:
    if kind == "datetime":
        return from_epoch_us(to_epoch_us(base) + round(amount * 1e6), tz)
    return float(base) + amount


def _x_literal(value: Any, kind: str, tz: str | None) -> pl.Expr:
    if kind == "datetime":
        naive_utc = value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
        expr = pl.lit(naive_utc, dtype=pl.Datetime("us"))
        if tz:
            expr = expr.dt.replace_time_zone("UTC").dt.convert_time_zone(tz)
        return expr
    return pl.lit(value)


def _us_to_seconds(expr: pl.Expr) -> pl.Expr:
    """Microseconds to float seconds, correctly rounded.

    polars divides floats by multiplying with the reciprocal, which turns
    100000 / 1e6 into 0.09999999999999999 in CSV output; numpy does not.
    """

    def convert(series: pl.Series) -> pl.Series:
        return pl.Series(series.name, series.cast(pl.Float64).to_numpy() / 1e6, nan_to_null=True)

    extra = {"is_elementwise": True} if "is_elementwise" in _MAP_BATCHES_PARAMS else {}
    return expr.map_batches(convert, return_dtype=pl.Float64, **extra)


def _x_meta(info: DatasetInfo, x: str) -> tuple[str, str | None]:
    col = info.column(x)
    return info.x_kind(x), col.time_zone


# ---------------------------------------------------------------------------
# validation and scanning
# ---------------------------------------------------------------------------


def validate_for(cfg: ProcessingConfig, info: DatasetInfo, *, require_y: bool = True) -> None:
    """Check that *cfg* can be applied to the file described by *info*."""
    try:
        if require_y:
            cfg.validate()
        else:
            cfg.aggregation.validate()
            cfg.csv.validate()
    except ConfigError as exc:
        raise ProcessingError(str(exc)) from exc
    name = info.path.name
    if not cfg.x:
        raise ProcessingError("No x column configured")
    if not info.has_column(cfg.x):
        raise ProcessingError(f"X column {cfg.x!r} not found in {name}")
    xcol = info.column(cfg.x)
    if cfg.x != ROW_INDEX and not xcol.x_candidate:
        raise ProcessingError(f"Column {cfg.x!r} ({xcol.dtype_name}) cannot be used as the x axis")
    missing = [y for y in cfg.y if y == ROW_INDEX or not info.has_column(y)]
    if missing:
        raise ProcessingError(f"Column(s) not found in {name}: {', '.join(missing)}")
    bad = [f"{y} ({info.column(y).dtype_name})" for y in cfg.y if not info.column(y).plottable]
    if bad:
        raise ProcessingError(f"Only numeric or boolean columns can be y parameters: {', '.join(bad)}")
    if cfg.aggregation.method == "interval":
        _interval_width(cfg, info)  # raises on a mismatch between x type and interval


def _interval_width(cfg: ProcessingConfig, info: DatasetInfo) -> str | float:
    """Polars duration string (datetime x) or numeric bucket width."""
    every = cfg.aggregation.every
    kind = info.x_kind(cfg.x)
    text = str(every).strip()
    if kind == "datetime":
        if isinstance(every, (int, float)) or not is_duration(text):
            try:
                seconds = float(every)
            except ValueError:
                raise ProcessingError(f"Invalid interval {every!r}; use a duration such as '10s', '1m' or '1h'") from None
            return f"{max(1, round(seconds * 1e6))}us"
        return to_polars_duration(text)
    if not isinstance(every, (int, float)) and is_duration(text):
        if info.column(cfg.x).kind == "duration":
            return parse_duration_us(text) / 1e6
        raise ProcessingError(
            f"Interval {every!r} is a duration but x column {cfg.x!r} is numeric; give a number (bucket width in x units)"
        )
    try:
        width = float(every)
    except ValueError:
        raise ProcessingError(f"Invalid interval {every!r}; give the bucket width as a number") from None
    if width <= 0:
        raise ProcessingError("The interval must be positive")
    return width


def _scan(info: DatasetInfo, x: str, columns: Sequence[str]) -> pl.LazyFrame:
    """Lazy frame with *columns* normalised for processing and null x rows removed."""
    lf = pl.scan_parquet(info.path)
    if ROW_INDEX in columns:
        lf = lf.with_row_index(ROW_INDEX)
    exprs = []
    for name in dict.fromkeys(columns):
        col = info.column(name)
        expr = pl.col(name)
        if col.kind == "date":
            expr = expr.cast(pl.Datetime("us"))
        elif col.kind == "duration":
            expr = _us_to_seconds(expr.dt.total_microseconds())
        elif isinstance(col.dtype, pl.Decimal):
            expr = expr.cast(pl.Float64)
        exprs.append(expr.alias(name))
    lf = lf.select(exprs)
    keep = pl.col(x).is_not_null()
    if info.column(x).dtype in (pl.Float32, pl.Float64):
        keep = keep & pl.col(x).is_not_nan()
    return lf.filter(keep)


_bounds_cache: dict[tuple, Bounds | None] = {}
_bounds_lock = threading.Lock()


def data_bounds(info: DatasetInfo, x: str) -> Bounds | None:
    """First and last x value of the file (cached per file version)."""
    key = (*info.cache_key(), x)
    with _bounds_lock:
        if key in _bounds_cache:
            return _bounds_cache[key]
    if x == ROW_INDEX:
        result = Bounds(0, info.num_rows - 1) if info.num_rows else None
    else:
        row = (
            _scan(info, x, [x])
            .select(pl.col(x).min().alias("lo"), pl.col(x).max().alias("hi"))
            .collect()
            .row(0)
        )
        result = None if row[0] is None else Bounds(row[0], row[1])
    with _bounds_lock:
        if len(_bounds_cache) > 256:
            _bounds_cache.clear()
        _bounds_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# ranges
# ---------------------------------------------------------------------------


def resolve_ranges(
    cfg: ProcessingConfig, info: DatasetInfo, ranges: Sequence[TimeRange] | None = None
) -> Resolved:
    """Turn configured ranges (absolute or relative) into concrete x bounds."""
    kind, tz = _x_meta(info, cfg.x)
    ranges = cfg.ranges if ranges is None else ranges
    base = None
    out: Resolved = []
    for number, rng in enumerate(ranges, 1):
        bounds = []
        for value in (rng.start, rng.end):
            if value is None:
                bounds.append(None)
            elif cfg.range_mode == "relative":
                if base is None:
                    data = data_bounds(info, cfg.x)
                    if data is None:
                        raise ProcessingError(f"{info.path.name} has no rows with a valid x value")
                    base = data.lo
                try:
                    amount = parse_offset_seconds(value)
                except ValueError as exc:
                    raise ProcessingError(f"Range {number}: {exc}") from None
                bounds.append(_offset(base, amount, kind, tz))
            else:
                bounds.append(parse_x_value(value, kind, tz))
        lo, hi = bounds
        if lo is not None and hi is not None and hi < lo:
            raise ProcessingError(f"Range {number}: end is before start")
        out.append((lo, hi))
    return out


def _range_predicate(x: str, resolved: Resolved, kind: str, tz: str | None) -> pl.Expr | None:
    terms = []
    for lo, hi in resolved:
        parts = []
        if lo is not None:
            parts.append(pl.col(x) >= _x_literal(lo, kind, tz))
        if hi is not None:
            parts.append(pl.col(x) <= _x_literal(hi, kind, tz))
        if not parts:
            return None  # an unbounded range keeps everything
        terms.append(reduce(operator.and_, parts))
    return reduce(operator.or_, terms) if terms else None


def _range_spans(info: DatasetInfo, x: str, resolved: Resolved) -> list[tuple[pl.Expr, Bounds | None]]:
    """For every range: a row condition and its extent clipped to the data."""
    data = data_bounds(info, x)
    if data is None or not resolved:
        return [(pl.lit(True), data)]
    kind, tz = _x_meta(info, x)
    spans = []
    for lo, hi in resolved:
        a = data.lo if lo is None else max(lo, data.lo)
        b = data.hi if hi is None else min(hi, data.hi)
        condition = _range_predicate(x, [(lo, hi)], kind, tz)
        spans.append((pl.lit(True) if condition is None else condition, Bounds(a, b) if a <= b else None))
    return spans


def covered_fraction(info: DatasetInfo, x: str, resolved: Resolved) -> float:
    """Approximate share of the x span covered by *resolved* ranges."""
    data = data_bounds(info, x)
    if data is None:
        return 0.0
    if not resolved:
        return 1.0
    kind = info.x_kind(x)
    lo, hi = x_to_plot(data.lo, kind), x_to_plot(data.hi, kind)
    if hi <= lo:
        return 1.0
    covered = 0.0
    for a, b in resolved:
        a = lo if a is None else max(lo, x_to_plot(a, kind))
        b = hi if b is None else min(hi, x_to_plot(b, kind))
        covered += max(0.0, b - a)
    return min(1.0, covered / (hi - lo))


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def _points_bucket(x: str, kind: str, tz: str | None, span: Bounds | None, points: int, integer: bool) -> pl.Expr:
    """Expression assigning each row to one of *points* equal-width buckets."""
    if kind == "datetime":
        lo_us, hi_us = (to_epoch_us(span.lo), to_epoch_us(span.hi)) if span else (0, 0)
        width = max(1, math.ceil((hi_us - lo_us) / points))
        index = ((pl.col(x).dt.epoch("us") - lo_us) // width).clip(0, points - 1)
        expr = pl.from_epoch(index * width + lo_us, time_unit="us")
        if tz:
            expr = expr.dt.replace_time_zone("UTC").dt.convert_time_zone(tz)
        return expr
    lo, hi = (float(span.lo), float(span.hi)) if span else (0.0, 0.0)
    if integer:
        width = max(1, math.ceil((int(hi) - int(lo) + 1) / points))
        return ((pl.col(x) - int(lo)) // width) * width + int(lo)
    width = (hi - lo) / points or 1.0
    index = ((pl.col(x) - lo) / width).floor().clip(0, points - 1)
    return index * width + lo


def _interval_bucket(x: str, kind: str, width: str | float, integer: bool) -> pl.Expr:
    if kind == "datetime":
        return pl.col(x).dt.truncate(str(width))
    if integer and float(width).is_integer():
        step = int(width)
        return (pl.col(x) // step) * step
    return (pl.col(x) / width).floor() * width


def _aggregate(lf: pl.LazyFrame, info: DatasetInfo, cfg: ProcessingConfig, resolved: Resolved) -> pl.LazyFrame:
    agg = cfg.aggregation
    if agg.method == "none":
        return lf
    if agg.method == "every_nth":
        return lf.gather_every(agg.n)
    x = cfg.x
    kind, tz = _x_meta(info, x)
    integer = info.column(x).dtype.is_integer()
    if agg.method == "interval":
        bucket = _interval_bucket(x, kind, _interval_width(cfg, info), integer)
    else:
        # Every range gets its own `points` buckets, so separate ranges keep
        # their resolution instead of sharing one grid over the gap between them.
        bucket = None
        for condition, span in _range_spans(info, x, resolved):
            expr = _points_bucket(x, kind, tz, span, agg.points, integer)
            bucket = expr if bucket is None else pl.when(condition).then(expr).otherwise(bucket)
    values = []
    for out_name, source, func in cfg.output_columns():
        expr = pl.col(source)
        if info.column(source).kind == "boolean":
            expr = expr.cast(pl.Int32)
        values.append(_AGG_EXPRS[func](expr).alias(out_name))
    return lf.group_by(bucket.alias(x), maintain_order=True).agg(values).sort(x)


def processed_frame(
    info: DatasetInfo,
    cfg: ProcessingConfig,
    resolved: Resolved,
    *,
    row_limit: int | None = None,
    sort: bool | None = None,
) -> pl.LazyFrame:
    """x plus value columns after range filtering, sorting and aggregation."""
    x = cfg.x
    ys = [c for c in dict.fromkeys(cfg.y) if c != x]
    kind, tz = _x_meta(info, x)
    lf = _scan(info, x, [x, *ys])
    predicate = _range_predicate(x, resolved, kind, tz)
    if predicate is not None:
        lf = lf.filter(predicate)
    if row_limit is not None:
        lf = lf.head(row_limit)
    if cfg.sort if sort is None else sort:
        lf = lf.sort(x)
    return _aggregate(lf, info, cfg, resolved)


def estimate_rows(info: DatasetInfo, cfg: ProcessingConfig, resolved: Resolved | None = None) -> int:
    """Estimated number of rows the configuration produces (for UI hints)."""
    resolved = resolved or []
    fraction = covered_fraction(info, cfg.x, resolved) if resolved else 1.0
    rows = info.num_rows * fraction
    agg = cfg.aggregation
    if agg.method == "none":
        return int(rows)
    if agg.method == "every_nth":
        return math.ceil(rows / max(1, agg.n))
    if agg.method == "target_points":
        return int(min(rows, agg.points * max(1, len(resolved))))
    data = data_bounds(info, cfg.x)
    if data is None:
        return 0
    kind = info.x_kind(cfg.x)
    span = (x_to_plot(data.hi, kind) - x_to_plot(data.lo, kind)) * fraction
    try:
        width = _interval_width(cfg, info)
    except ProcessingError:
        return int(rows)
    width = parse_duration_us(width) / 1e6 if isinstance(width, str) else width
    return int(min(rows, math.floor(span / width) + 1 + len(resolved)))


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def csv_write_options(cfg: ProcessingConfig, *, for_sink: bool = False) -> dict[str, Any]:
    csv = cfg.csv
    options: dict[str, Any] = {
        "separator": csv.separator,
        "include_header": csv.include_header,
        "line_terminator": LINE_TERMINATORS[csv.line_terminator],
        "null_value": csv.null_value,
        "quote_style": csv.quote_style,
        "include_bom": csv.include_bom,
    }
    if csv.float_precision is not None:
        options["float_precision"] = csv.float_precision
    if csv.time_format == "custom":
        options["datetime_format"] = csv.datetime_format
    if csv.decimal_comma:
        options["decimal_comma"] = True
    supported = _SINK_CSV_PARAMS if for_sink else _WRITE_CSV_PARAMS
    unsupported = [k for k in options if k not in supported]
    if unsupported:
        raise ProcessingError(f"The installed polars version does not support CSV option(s): {', '.join(unsupported)}")
    return options


def csv_frame(lf: pl.LazyFrame, info: DatasetInfo, cfg: ProcessingConfig) -> pl.LazyFrame:
    """Apply time formatting and header renames to a processed frame."""
    x = cfg.x
    kind, _ = _x_meta(info, x)
    fmt = cfg.csv.time_format
    expr = None
    if fmt == "elapsed_s":
        data = data_bounds(info, x)
        base = data.lo if data else None
        if base is not None:
            if kind == "datetime":
                expr = _us_to_seconds(pl.col(x).dt.epoch("us") - to_epoch_us(base))
            else:
                expr = pl.col(x) - base
    elif kind == "datetime" and fmt == "epoch_s":
        expr = _us_to_seconds(pl.col(x).dt.epoch("us"))
    elif kind == "datetime" and fmt == "epoch_ms":
        expr = pl.col(x).dt.epoch("ms")
    if expr is not None:
        lf = lf.with_columns(expr.alias(x))
    names = [x, *(name for name, _, _ in cfg.output_columns())]
    rename = {k: v for k, v in cfg.csv.rename.items() if k in names and v and v != k}
    if x == ROW_INDEX and x not in rename:
        rename[x] = ROW_INDEX_HEADER
    headers = [rename.get(n, n) for n in names]
    duplicates = sorted({h for h in headers if headers.count(h) > 1})
    if duplicates:
        raise ProcessingError(f"Duplicate output column name(s): {', '.join(duplicates)}")
    return lf.rename(rename) if rename else lf


def export_query(info: DatasetInfo, cfg: ProcessingConfig, ranges: Sequence[TimeRange] | None = None) -> pl.LazyFrame:
    """The complete lazy query that produces the CSV rows for *cfg*."""
    validate_for(cfg, info)
    resolved = resolve_ranges(cfg, info, ranges)
    return csv_frame(processed_frame(info, cfg, resolved), info, cfg)


_UNSAFE = re.compile(r"[^\w.-]+")


def render_output_path(template: str, input_path: Path, range_label: str = "", base_dir: Path | None = None) -> Path:
    values = {
        "stem": input_path.stem,
        "name": input_path.name,
        "parent": input_path.parent.name,
        "dir": str(input_path.parent),
        "range": _UNSAFE.sub("_", range_label).strip("_") or range_label,
    }
    try:
        text = template.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        raise ProcessingError(
            f"Invalid output path template {template!r} ({exc}); placeholders are {{stem}}, {{name}}, {{parent}}, {{dir}} and {{range}}"
        ) from None
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = (base_dir or input_path.parent) / path
    return path


@dataclass
class OutputPlan:
    path: Path | None  # None means standard output
    ranges: list[TimeRange]
    label: str = ""


def plan_outputs(
    source: DatasetInfo | Path,
    cfg: ProcessingConfig,
    output: str | None = None,
    base_dir: Path | None = None,
) -> list[OutputPlan]:
    """Work out which files an export writes (one per range when splitting)."""
    input_path = source.path if isinstance(source, DatasetInfo) else Path(source)
    template = output or cfg.output.path
    if template == "-":
        if cfg.output.split_ranges and len(cfg.ranges) > 1:
            raise ProcessingError("Cannot write one file per range to standard output")
        return [OutputPlan(None, list(cfg.ranges))]
    if cfg.output.split_ranges and cfg.ranges:
        if "{range}" not in template:
            stem, dot, suffix = template.rpartition(".")
            template = f"{stem}_{{range}}.{suffix}" if dot and "/" not in suffix and "\\" not in suffix else f"{template}_{{range}}"
        plans = []
        for number, rng in enumerate(cfg.ranges, 1):
            label = rng.label or str(number)
            plans.append(OutputPlan(render_output_path(template, input_path, label, base_dir), [rng], label))
        paths = [p.path for p in plans]
        if len(set(paths)) != len(paths):
            raise ProcessingError("Several ranges map to the same output file; give the ranges unique labels")
    else:
        plans = [OutputPlan(render_output_path(template, input_path, "all", base_dir), list(cfg.ranges))]
    for plan in plans:
        if plan.path is not None and plan.path.resolve() == input_path.resolve():
            raise ProcessingError(f"Refusing to overwrite the input file {input_path}")
    return plans


def check_batch_outputs(
    inputs: Sequence[Path], cfg: ProcessingConfig, output: str | None = None, base_dir: Path | None = None
) -> None:
    """Refuse a batch in which two inputs would be written to the same file."""
    owners: dict[Path, Path] = {}
    for path in inputs:
        try:
            plans = plan_outputs(Path(path), cfg, output, base_dir)
        except ProcessingError:
            continue  # reported for that file when it is exported
        for plan in plans:
            if plan.path is None:
                if len(inputs) > 1:
                    raise ProcessingError("Only a single input file can be written to standard output")
                continue
            key = plan.path.resolve()
            if key in owners and owners[key] != path:
                raise ProcessingError(
                    f"{owners[key].name} and {Path(path).name} would both be written to {plan.path}; "
                    "include {stem} in the output file name"
                )
            owners[key] = path


@dataclass
class ExportResult:
    input: Path
    outputs: list[tuple[Path | None, int]] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)

    @property
    def rows(self) -> int:
        return sum(rows for _, rows in self.outputs)


def _write_stdout(text: str, stream: Any = None) -> None:
    """Write CSV text to *stream*, or to stdout as UTF-8 bytes.

    Writing bytes avoids the platform newline translation (``\\r\\r\\n`` on
    Windows) and the console encoding of text-mode stdout.
    """
    if stream is not None:
        stream.write(text)
        return
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(text)
        return
    sys.stdout.flush()
    buffer.write(text.encode("utf-8"))
    buffer.flush()


def _count_lines(path: Path) -> int:
    count = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            count += chunk.count(b"\n")
    return count


def export_file(
    source: str | os.PathLike | DatasetInfo,
    cfg: ProcessingConfig,
    *,
    output: str | None = None,
    base_dir: Path | None = None,
    overwrite: bool = True,
    stream: Any = None,
) -> ExportResult:
    """Export one Parquet file to CSV according to *cfg*.

    ``output`` overrides ``cfg.output.path`` (``"-"`` writes to *stream*,
    standard output by default). Relative output paths are resolved against
    *base_dir*, or the input file's directory when not given.
    """
    info = source if isinstance(source, DatasetInfo) else inspect_parquet(source)
    validate_for(cfg, info)
    result = ExportResult(info.path)
    for plan in plan_outputs(info, cfg, output, base_dir):
        query = export_query(info, cfg, plan.ranges)
        if plan.path is None:
            df = query.collect()
            text = df.write_csv(**csv_write_options(cfg))
            _write_stdout(text, stream)
            result.outputs.append((None, df.height))
            continue
        if plan.path.exists() and not overwrite:
            result.skipped.append(plan.path)
            continue
        plan.path.parent.mkdir(parents=True, exist_ok=True)
        partial = plan.path.with_name(f".{plan.path.stem}.partial{plan.path.suffix}")
        try:
            query.sink_csv(partial, **csv_write_options(cfg, for_sink=True))
            lines = _count_lines(partial)
            os.replace(partial, plan.path)
        finally:
            if partial.exists():
                partial.unlink()
        result.outputs.append((plan.path, max(0, lines - (1 if cfg.csv.include_header else 0))))
    return result


def preview_csv(
    source: str | os.PathLike | DatasetInfo,
    cfg: ProcessingConfig,
    *,
    rows: int = 20,
    scan_limit: int = 200_000,
) -> str:
    """The first *rows* lines of the CSV an export would write.

    Only the first *scan_limit* matching rows are processed, which keeps the
    preview fast for large files (aggregated values of the last bucket may
    therefore differ from the real export).
    """
    info = source if isinstance(source, DatasetInfo) else inspect_parquet(source)
    validate_for(cfg, info)
    # When writing one file per range, preview the first one.
    ranges = cfg.ranges[:1] if cfg.output.split_ranges else cfg.ranges
    resolved = resolve_ranges(cfg, info, ranges)
    lf = processed_frame(info, cfg, resolved, row_limit=scan_limit)
    df = csv_frame(lf, info, cfg).head(rows).collect()
    options = csv_write_options(cfg)
    options["include_bom"] = False
    return df.write_csv(**options)


# ---------------------------------------------------------------------------
# batch processing
# ---------------------------------------------------------------------------


def find_inputs(paths: Iterable[str | os.PathLike], recursive: bool = False) -> list[Path]:
    """Expand files, directories and glob patterns into a list of Parquet files."""
    found: list[Path] = []
    for item in paths:
        text = os.fspath(item)
        if any(ch in text for ch in "*?["):
            matches = sorted(Path(p) for p in glob.glob(os.path.expanduser(text), recursive=True))
            found.extend(p for p in matches if p.is_file())
            continue
        path = Path(text).expanduser()
        if path.is_dir():
            pattern = "**/*" if recursive else "*"
            found.extend(
                sorted(p for p in path.glob(pattern) if p.is_file() and p.suffix.lower() in PARQUET_SUFFIXES)
            )
        else:
            found.append(path)
    return list(dict.fromkeys(found))


@dataclass
class BatchItem:
    input: Path
    result: ExportResult | None = None
    error: str | None = None


def batch_export(
    inputs: Iterable[Path],
    cfg: ProcessingConfig,
    *,
    output: str | None = None,
    base_dir: Path | None = None,
    overwrite: bool = True,
    fail_fast: bool = False,
) -> Iterator[BatchItem]:
    """Export every input with the same configuration, yielding per-file results.

    Raises :class:`ProcessingError` before writing anything if two inputs
    would produce the same output file.
    """
    inputs = list(inputs)
    check_batch_outputs(inputs, cfg, output, base_dir)
    for path in inputs:
        try:
            yield BatchItem(path, export_file(path, cfg, output=output, base_dir=base_dir, overwrite=overwrite))
        except (ProcessingError, ConfigError, OSError, ValueError, pl.exceptions.PolarsError) as exc:
            yield BatchItem(path, error=str(exc))
            if fail_fast:
                return


# ---------------------------------------------------------------------------
# plot data
# ---------------------------------------------------------------------------


@dataclass
class PlotSeries:
    name: str
    column: str
    func: str | None
    x: np.ndarray
    y: np.ndarray


@dataclass
class PlotData:
    x_name: str
    x_kind: str
    x_tz: str | None
    series: list[PlotSeries]
    #: Rows produced by the processing configuration (estimate when reduced).
    rows: int
    #: True when the data was reduced to a min/max envelope for display.
    reduced: bool = False
    #: Full data extent in plot units.
    bounds: tuple[float, float] | None = None
    #: When set, the data only covers this x window (detail load).
    view: tuple[float, float] | None = None

    @property
    def points(self) -> int:
        return sum(len(s.x) for s in self.series)


def _plot_x(x: str, kind: str) -> pl.Expr:
    if kind == "datetime":
        return pl.col(x).dt.epoch("us").cast(pl.Float64) / 1e6
    return pl.col(x).cast(pl.Float64)


def load_plot_data(
    source: str | os.PathLike | DatasetInfo,
    cfg: ProcessingConfig,
    *,
    max_points: int = 1_000_000,
    envelope_buckets: int = 50_000,
    view: tuple[float, float] | None = None,
) -> PlotData:
    """Load the processed data for plotting.

    Ranges in *cfg* are ignored (the plot shows everything and draws ranges as
    overlays). If the processed data would exceed *max_points* rows it is
    reduced to a min/max envelope of *envelope_buckets* buckets that preserves
    peaks; exports are never affected by this. *view* restricts loading to an
    x window given in plot units, which is used to fetch full detail for a
    zoomed-in region.
    """
    info = source if isinstance(source, DatasetInfo) else inspect_parquet(source)
    validate_for(cfg, info, require_y=False)
    x = cfg.x
    kind, tz = _x_meta(info, x)
    data = data_bounds(info, x)
    bounds = (x_to_plot(data.lo, kind), x_to_plot(data.hi, kind)) if data else None
    columns = cfg.output_columns()
    result = PlotData(x, kind, tz, [], 0, bounds=bounds, view=view)
    if data is None or not columns:
        return result
    resolved: Resolved = []
    if view is not None:
        resolved = [(plot_to_x(view[0], kind, tz), plot_to_x(view[1], kind, tz))]
    estimate = estimate_rows(info, cfg, resolved)
    result.rows = estimate
    if estimate <= max_points:
        df = (
            processed_frame(info, cfg, resolved)
            .select(_plot_x(x, kind).alias(x), *(pl.col(name).cast(pl.Float64) for name, _, _ in columns))
            .collect()
        )
        xs = df[x].to_numpy()
        result.rows = df.height
        result.series = [PlotSeries(name, src, func, xs, df[name].to_numpy()) for name, src, func in columns]
        return result

    # Too many rows to draw: reduce to the positions of the min and max of
    # every value column within each of `envelope_buckets` buckets.
    result.reduced = True
    # Row order does not matter for the envelope, so skip the (expensive) sort
    # when nothing downstream depends on it.
    lf = processed_frame(info, cfg, resolved, sort=False if cfg.aggregation.method == "none" else None)
    span = Bounds(*resolved[0]) if resolved else data
    integer = lf.collect_schema()[x].is_integer()
    bucket = _points_bucket(x, kind, tz, span, envelope_buckets, integer)
    xpos = pl.col("__x__")
    aggs = []
    for i, (name, _, _) in enumerate(columns):
        value = pl.col(name)
        aggs += [
            value.min().alias(f"lo{i}"),
            value.max().alias(f"hi{i}"),
            xpos.get(value.arg_min()).alias(f"xlo{i}"),
            xpos.get(value.arg_max()).alias(f"xhi{i}"),
        ]
    df = (
        lf.with_columns(_plot_x(x, kind).alias("__x__"), *(pl.col(n).cast(pl.Float64) for n, _, _ in columns))
        .group_by(bucket.alias(_BUCKET))
        .agg(aggs)
        .sort(_BUCKET)
        .collect()
    )
    for i, (name, src, func) in enumerate(columns):
        xlo, xhi = df[f"xlo{i}"].to_numpy(), df[f"xhi{i}"].to_numpy()
        ylo, yhi = df[f"lo{i}"].to_numpy(), df[f"hi{i}"].to_numpy()
        keep = ~(np.isnan(ylo) | np.isnan(xlo))
        xlo, xhi, ylo, yhi = xlo[keep], xhi[keep], ylo[keep], yhi[keep]
        low_first = xlo <= xhi
        xs = np.empty(2 * len(xlo))
        ys = np.empty(2 * len(xlo))
        xs[0::2] = np.where(low_first, xlo, xhi)
        xs[1::2] = np.where(low_first, xhi, xlo)
        ys[0::2] = np.where(low_first, ylo, yhi)
        ys[1::2] = np.where(low_first, yhi, ylo)
        result.series.append(PlotSeries(name, src, func, xs, ys))
    return result


def merge_plot_data(overview: PlotData, detail: PlotData) -> PlotData:
    """Splice *detail* (loaded for ``detail.view``) into *overview*."""
    if detail.view is None:
        return detail
    lo, hi = detail.view
    by_name = {s.name: s for s in detail.series}
    merged = []
    for series in overview.series:
        extra = by_name.get(series.name)
        if extra is None:
            merged.append(series)
            continue
        left = series.x < lo
        right = series.x > hi
        merged.append(
            PlotSeries(
                series.name,
                series.column,
                series.func,
                np.concatenate([series.x[left], extra.x, series.x[right]]),
                np.concatenate([series.y[left], extra.y, series.y[right]]),
            )
        )
    return PlotData(
        overview.x_name,
        overview.x_kind,
        overview.x_tz,
        merged,
        overview.rows,
        reduced=overview.reduced,
        bounds=overview.bounds,
    )
