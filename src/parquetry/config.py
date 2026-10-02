"""Processing configuration shared by the UI and the command line.

A :class:`ProcessingConfig` captures everything needed to turn a Parquet or
CSV file into CSV: the x column, the y parameters, their scaling, the ranges to
keep, how to aggregate and how the CSV should be formatted. It round-trips through JSON so that a
configuration built interactively can be re-applied to similar files with
``parquetry export --config``.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import os
import re
from collections.abc import Collection
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .durations import is_duration, parse_offset_seconds

CONFIG_VERSION = 1

AGG_METHODS = ("none", "every_nth", "interval", "target_points", "per_value")
AGG_METHOD_ALIASES = {
    "raw": "none",
    "nth": "every_nth",
    "decimate": "every_nth",
    "bucket": "interval",
    "points": "target_points",
    "auto": "target_points",
    "group": "per_value",
    "category": "per_value",
}
AGG_METHOD_LABELS = {
    "none": "None (all rows)",
    "every_nth": "Every Nth row",
    "interval": "Fixed interval buckets",
    "target_points": "Target number of points",
    "per_value": "One bucket per x value",
}
AGG_FUNCTIONS = ("mean", "min", "max", "median", "first", "last", "sum", "count", "std")
#: Aggregation functions that also apply to text columns.
TEXT_FUNCTIONS = ("first", "last", "count", "min", "max")

RANGE_MODES = ("absolute", "relative")
TIME_FORMATS = ("iso", "custom", "epoch_s", "epoch_ms", "elapsed_s")
TIME_FORMAT_LABELS = {
    "iso": "ISO 8601 (2024-01-31T12:00:00.000000)",
    "custom": "Custom strftime format",
    "epoch_s": "Unix epoch seconds",
    "epoch_ms": "Unix epoch milliseconds",
    "elapsed_s": "Elapsed seconds from data start",
}
QUOTE_STYLES = ("necessary", "always", "non_numeric", "never")
LINE_TERMINATORS = {"lf": "\n", "crlf": "\r\n"}


class ConfigError(ValueError):
    """Raised for invalid or inconsistent configuration values."""


def _check_keys(kind: str, data: dict, allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"Unknown {kind} setting(s): {', '.join(sorted(unknown))}")


_ARITHMETIC = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def parse_number(text: str | float) -> float:
    """A number, or arithmetic of numbers such as ``1/3.6`` or ``9/5``.

    Only ``+``, ``-``, ``*``, ``/`` and parentheses are allowed, which is enough
    to write unit conversion factors exactly. Raises ValueError otherwise.
    """
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        value = float(text)
    else:
        try:
            node = ast.parse(str(text).strip(), mode="eval").body
            value = _evaluate(node)
        except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, RecursionError):
            raise ValueError(f"{text!r} is not a number (arithmetic such as 1/3.6 is allowed)") from None
    if not math.isfinite(value):
        raise ValueError(f"{text!r} is not a finite number")
    return value


def _evaluate(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _evaluate(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITHMETIC:
        return _ARITHMETIC[type(node.op)](_evaluate(node.left), _evaluate(node.right))
    raise ValueError("not arithmetic")


def _norm_offset(value: Any) -> float | str:
    """A number for numeric offsets; duration strings (``"2h"``) are kept as text."""
    if isinstance(value, bool):
        raise ConfigError(f"Invalid offset {value!r}")
    try:
        return parse_number(value)
    except ValueError:
        return str(value).strip()


@dataclass(frozen=True)
class Scaling:
    """A first-order correction or unit conversion of a column: ``value * scale + offset``.

    It applies before ranges, aggregation and export, so plots, tables and CSV
    files all show the converted values. Date/time columns can only be shifted:
    their offset is in seconds or a duration such as ``"2h"`` or ``"-30s"``.
    """

    scale: float = 1.0
    offset: float | str = 0.0

    @property
    def offset_value(self) -> float:
        """The offset as a number (seconds for a duration)."""
        return parse_offset_seconds(self.offset)

    @property
    def is_identity(self) -> bool:
        return self.scale == 1 and self.offset_value == 0

    def apply(self, value: float) -> float:
        return value * self.scale + self.offset_value

    def describe(self) -> str:
        """Compact text such as ``× 1.8 + 32`` or ``+ 2h``."""
        parts = [f"× {self.scale:g}"] if self.scale != 1 else []
        offset = self.offset_value
        if offset:
            text = self.offset.strip().lstrip("-").strip() if isinstance(self.offset, str) else f"{abs(offset):g}"
            parts.append(f"{'−' if offset < 0 else '+'} {text}")
        return " ".join(parts) or "as is"

    def invert(self, value: float) -> float:
        return (value - self.offset_value) / self.scale

    def validate(self, column: str = "") -> None:
        where = f"scaling of {column!r}" if column else "scaling"
        scale = self.scale
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale == 0:
            raise ConfigError(f"The {where} needs a non-zero scale, got {scale!r}")
        try:
            offset = self.offset_value
        except ValueError:
            raise ConfigError(
                f"The offset {self.offset!r} in the {where} is neither a number nor a duration such as '2h'"
            ) from None
        if not math.isfinite(offset):
            raise ConfigError(f"The {where} needs a finite offset, got {self.offset!r}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.scale != 1:
            out["scale"] = self.scale
        if self.offset_value != 0:
            out["offset"] = self.offset
        return out

    @classmethod
    def from_value(cls, value: Any) -> Scaling:
        if isinstance(value, Scaling):
            return value
        if not isinstance(value, dict):
            raise ConfigError(f'A scaling needs {{"scale": ..., "offset": ...}}, got {value!r}')
        _check_keys("scaling", value, {"scale", "offset"})
        try:
            scale = parse_number(value.get("scale", 1.0))
        except ValueError as exc:
            raise ConfigError(f"Invalid scale: {exc}") from None
        return cls(scale, _norm_offset(value.get("offset", 0.0)))


def _load_scaling(column: str, value: Any) -> Scaling:
    scaling = Scaling.from_value(value)
    scaling.validate(column)
    return scaling


_COLOR = re.compile(r"^(#[0-9a-fA-F]{6}|[A-Za-z]+)$")


@dataclass
class TimeRange:
    """A span of the x axis. ``None`` means open ended.

    In ``absolute`` mode values are ISO 8601 strings for datetime x columns and
    numbers for numeric ones. In ``relative`` mode they are offsets from the
    first x value of the file: seconds (or duration strings like ``"5m"``) for
    datetime columns, x units for numeric ones.
    """

    start: str | float | None = None
    end: str | float | None = None
    label: str = ""
    #: Highlight colour on the plot (``#rrggbb`` or a colour name); empty for the default.
    color: str = ""

    @classmethod
    def from_value(cls, value: Any) -> TimeRange:
        if isinstance(value, TimeRange):
            return value
        if isinstance(value, (list, tuple)):
            if len(value) not in (2, 3):
                raise ConfigError(f"A range needs [start, end], got {value!r}")
            return cls(_norm_bound(value[0]), _norm_bound(value[1]), str(value[2]) if len(value) == 3 else "")
        if isinstance(value, dict):
            _check_keys("range", value, {"start", "end", "label", "color"})
            color = str(value.get("color") or "").strip()
            if color and not _COLOR.match(color):
                raise ConfigError(f"Invalid range color {color!r}; use #rrggbb or a colour name")
            return cls(
                _norm_bound(value.get("start")), _norm_bound(value.get("end")), str(value.get("label") or ""), color
            )
        raise ConfigError(f"Invalid range {value!r}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"start": self.start, "end": self.end}
        if self.label:
            out["label"] = self.label
        if self.color:
            out["color"] = self.color
        return out


def _norm_bound(value: Any) -> str | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(f"Invalid range bound {value!r}")
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if text.lower() in {"", "-", "none", "null", "open"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return text
    if not math.isfinite(number):
        raise ConfigError(f"Invalid range bound {value!r}")
    return int(number) if number.is_integer() and not any(c in text for c in ".eE") else number


@dataclass
class AggregationConfig:
    method: str = "none"
    #: every_nth: keep one row out of ``n``.
    n: int = 10
    #: interval: bucket width, a duration string (``"1m"``) for datetime x or a number for numeric x.
    every: str | float = "1s"
    #: target_points: number of equal-width buckets spanning the data.
    points: int = 2000
    #: Functions applied per bucket (interval, target_points and per_value, which
    #: makes every distinct x value - or text category - its own bucket).
    functions: list[str] = field(default_factory=lambda: ["mean"])

    def __post_init__(self) -> None:
        self.method = AGG_METHOD_ALIASES.get(self.method, self.method)

    def validate(self) -> None:
        if self.method not in AGG_METHODS:
            raise ConfigError(f"Unknown aggregation method {self.method!r}; choose from {', '.join(AGG_METHODS)}")
        if self.method == "every_nth" and (not isinstance(self.n, int) or self.n < 1):
            raise ConfigError("aggregation.n must be a positive integer")
        if self.method == "target_points" and (not isinstance(self.points, int) or self.points < 1):
            raise ConfigError("aggregation.points must be a positive integer")
        if self.method == "interval":
            if isinstance(self.every, bool):
                raise ConfigError("aggregation.every must be a duration string or a positive number")
            if isinstance(self.every, (int, float)):
                if self.every <= 0:
                    raise ConfigError("aggregation.every must be positive")
            elif not is_duration(str(self.every)):
                try:
                    if float(self.every) <= 0:
                        raise ConfigError("aggregation.every must be positive")
                except ValueError:
                    raise ConfigError(
                        f"aggregation.every {self.every!r} is neither a duration (e.g. '1m') nor a number"
                    ) from None
        if self.is_bucketed:
            if not self.functions:
                raise ConfigError("Select at least one aggregation function")
            bad = [f for f in self.functions if f not in AGG_FUNCTIONS]
            if bad:
                raise ConfigError(f"Unknown aggregation function(s) {bad}; choose from {', '.join(AGG_FUNCTIONS)}")

    @property
    def is_bucketed(self) -> bool:
        return self.method in {"interval", "target_points", "per_value"}

    def describe(self) -> str:
        if self.method == "none":
            return "No aggregation"
        if self.method == "every_nth":
            return f"Every {self.n}th row"
        funcs = ", ".join(self.functions)
        if self.method == "interval":
            return f"{funcs} per {self.every} bucket"
        if self.method == "per_value":
            return f"{funcs} per x value"
        return f"{funcs} over {self.points:,} buckets"

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"method": self.method}
        if self.method == "every_nth":
            out["n"] = self.n
        elif self.method == "interval":
            out["every"] = self.every
        elif self.method == "target_points":
            out["points"] = self.points
        if self.is_bucketed:
            out["functions"] = list(self.functions)
        return out

    @classmethod
    def from_dict(cls, data: dict) -> AggregationConfig:
        if isinstance(data, str):
            data = {"method": data}
        _check_keys("aggregation", data, {f.name for f in fields(cls)})
        agg = cls(**data)
        if isinstance(agg.functions, str):
            agg.functions = [f.strip() for f in agg.functions.split(",") if f.strip()]
        return agg


@dataclass
class CsvOptions:
    separator: str = ","
    include_header: bool = True
    #: Digits after the decimal point; ``None`` writes full precision.
    float_precision: int | None = None
    decimal_comma: bool = False
    time_format: str = "iso"
    #: strftime-style format used when ``time_format == "custom"``.
    datetime_format: str = "%Y-%m-%d %H:%M:%S%.3f"
    null_value: str = ""
    quote_style: str = "necessary"
    line_terminator: str = "lf"
    include_bom: bool = False
    #: Output header names, keyed by source column name.
    rename: dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        if len(self.separator) != 1:
            raise ConfigError("csv.separator must be a single character")
        if self.time_format not in TIME_FORMATS:
            raise ConfigError(f"Unknown csv.time_format {self.time_format!r}; choose from {', '.join(TIME_FORMATS)}")
        if self.time_format == "custom" and not self.datetime_format:
            raise ConfigError("csv.datetime_format is required when time_format is 'custom'")
        if self.quote_style not in QUOTE_STYLES:
            raise ConfigError(f"Unknown csv.quote_style {self.quote_style!r}; choose from {', '.join(QUOTE_STYLES)}")
        if self.line_terminator not in LINE_TERMINATORS:
            raise ConfigError("csv.line_terminator must be 'lf' or 'crlf'")
        if self.float_precision is not None and (not isinstance(self.float_precision, int) or self.float_precision < 0):
            raise ConfigError("csv.float_precision must be a non-negative integer or null")
        if self.decimal_comma and self.separator == ",":
            raise ConfigError("Decimal comma needs a separator other than ',' (e.g. ';')")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> CsvOptions:
        _check_keys("csv", data, {f.name for f in fields(cls)})
        data = dict(data)
        if data.get("separator") in {"\\t", "tab"}:
            data["separator"] = "\t"
        return cls(**data)


@dataclass
class OutputOptions:
    #: Output path template. Placeholders: ``{stem}`` (input name without
    #: suffix), ``{name}``, ``{parent}`` (input directory name), ``{dir}`` (input
    #: directory path) and ``{range}``
    #: (range label or number, when writing one file per range). Relative
    #: paths are resolved against the input file's directory.
    path: str = "{stem}_export.csv"
    #: Write every range to its own file instead of one combined file.
    split_ranges: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> OutputOptions:
        _check_keys("output", data, {f.name for f in fields(cls)})
        return cls(**data)


@dataclass
class ProcessingConfig:
    x: str = ""
    y: list[str] = field(default_factory=list)
    #: Scale and offset by column name (x or y); columns without one are used as they are.
    scaling: dict[str, Scaling] = field(default_factory=dict)
    ranges: list[TimeRange] = field(default_factory=list)
    range_mode: str = "absolute"
    aggregation: AggregationConfig = field(default_factory=AggregationConfig)
    #: Sort rows by x before aggregating/exporting (required for unsorted files).
    sort: bool = True
    csv: CsvOptions = field(default_factory=CsvOptions)
    output: OutputOptions = field(default_factory=OutputOptions)
    #: Informational only: the file the configuration was created from.
    source: str | None = None

    def validate(self) -> None:
        if not self.x:
            raise ConfigError("No x column configured")
        if not self.y:
            raise ConfigError("No y parameters configured")
        if self.range_mode not in RANGE_MODES:
            raise ConfigError(f"range_mode must be one of {', '.join(RANGE_MODES)}")
        self.validate_scaling()
        self.aggregation.validate()
        self.csv.validate()

    def validate_scaling(self) -> None:
        for column, scaling in self.scaling.items():
            scaling.validate(column)

    def scaling_for(self, column: str) -> Scaling | None:
        """The scaling of *column*, or ``None`` when its values are used as they are."""
        scaling = self.scaling.get(column)
        return None if scaling is None or scaling.is_identity else scaling

    def copy(self, **changes: Any) -> ProcessingConfig:
        clone = ProcessingConfig.from_dict(self.to_dict())
        return replace(clone, **changes) if changes else clone

    def output_columns(self, text: Collection[str] = ()) -> list[tuple[str, str, str | None]]:
        """``(output name, source column, function)`` for every value column.

        *text* names the y columns that hold text. Only :data:`TEXT_FUNCTIONS`
        apply to them; when none of the configured functions does, the first
        value of each bucket is used.
        """
        ys = [c for c in dict.fromkeys(self.y) if c != self.x]
        agg = self.aggregation
        if not agg.is_bucketed:
            return [(c, c, None) for c in ys]
        funcs = list(dict.fromkeys(agg.functions))
        out = []
        for c in ys:
            own = funcs
            if c in text:
                own = [fn for fn in funcs if fn in TEXT_FUNCTIONS] or ["first"]
            if len(funcs) == 1:
                out.append((c, c, own[0]))
            else:
                out += [(f"{c}_{fn}", c, fn) for fn in own]
        return out

    # -- serialisation -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"version": CONFIG_VERSION}
        if self.source:
            out["source"] = self.source
        out.update({"x": self.x, "y": list(self.y)})
        scaling = {column: s.to_dict() for column, s in self.scaling.items() if not s.is_identity}
        if scaling:  # left out otherwise, so that older versions can read the configuration
            out["scaling"] = scaling
        out.update(
            {
                "range_mode": self.range_mode,
                "ranges": [r.to_dict() for r in self.ranges],
                "aggregation": self.aggregation.to_dict(),
                "sort": self.sort,
                "csv": self.csv.to_dict(),
                "output": self.output.to_dict(),
            }
        )
        return out

    @classmethod
    def from_dict(cls, data: dict) -> ProcessingConfig:
        if not isinstance(data, dict):
            raise ConfigError("A configuration must be a JSON object")
        data = dict(data)
        version = data.pop("version", CONFIG_VERSION)
        if not isinstance(version, int) or version > CONFIG_VERSION:
            raise ConfigError(f"Unsupported configuration version {version!r} (this program supports {CONFIG_VERSION})")
        _check_keys("configuration", data, {f.name for f in fields(cls)})
        y = data.get("y", [])
        if isinstance(y, str):
            y = [y]
        scaling = data.get("scaling") or {}
        if not isinstance(scaling, dict):
            raise ConfigError('scaling must map column names to {"scale": ..., "offset": ...}')
        try:
            return cls(
                x=str(data.get("x") or ""),
                y=[str(c) for c in y],
                scaling={str(column): _load_scaling(str(column), value) for column, value in scaling.items()},
                ranges=[TimeRange.from_value(r) for r in data.get("ranges") or []],
                range_mode=data.get("range_mode", "absolute"),
                aggregation=AggregationConfig.from_dict(data.get("aggregation") or {}),
                sort=bool(data.get("sort", True)),
                csv=CsvOptions.from_dict(data.get("csv") or {}),
                output=OutputOptions.from_dict(data.get("output") or {}),
                source=data.get("source"),
            )
        except TypeError as exc:
            raise ConfigError(str(exc)) from exc

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"

    @classmethod
    def from_json(cls, text: str) -> ProcessingConfig:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"Invalid JSON: {exc}") from exc
        return cls.from_dict(data)

    def save(self, path: str | os.PathLike) -> Path:
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: str | os.PathLike) -> ProcessingConfig:
        path = Path(path).expanduser()
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"Cannot read configuration {path}: {exc}") from exc
        try:
            return cls.from_json(text)
        except ConfigError as exc:
            raise ConfigError(f"{path.name}: {exc}") from exc
