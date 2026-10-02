"""Command line interface.

``parquetry`` with no arguments (or with just a file name) starts the desktop
UI. The ``info`` and ``export`` sub-commands provide scripted access to the
same processing engine, typically driven by a configuration saved from the UI.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from . import __version__
from .config import (
    AGG_FUNCTIONS,
    AGG_METHOD_ALIASES,
    AGG_METHODS,
    QUOTE_STYLES,
    TIME_FORMATS,
    ConfigError,
    ProcessingConfig,
    Scaling,
    TimeRange,
    parse_number,
)
from .dataset import ROW_INDEX, ROW_INDEX_LABEL, format_bytes, inspect_file
from .durations import format_seconds, is_duration

COMMANDS = ("ui", "info", "export", "sample")

EXPORT_EPILOG = """\
examples:
  # re-run a configuration saved from the UI on a whole folder
  parquetry export data/*.parquet --config telemetry.json -o out/{stem}.csv

  # everything on the command line: 1 minute means and maxima of two columns
  parquetry export flight.parquet --x timestamp --y altitude_m airspeed_mps \\
      --agg interval --every 1m --func mean max -o flight_1min.csv

  # first 10 minutes of every file, relative to each file's start
  parquetry export runs/ --config cfg.json --range 0 10m --range-mode relative

  # CSV input; mean and maximum per category of a text column
  parquetry export sales.csv --x region --y revenue --agg per_value --func mean max

  # feet instead of metres, km/h instead of m/s, and times two hours later
  parquetry export flight.parquet --y altitude_m airspeed_mps --scale altitude_m=3.28084 \
      --scale airspeed_mps=3.6 --offset timestamp=2h

directories are searched for Parquet (.parquet, .parq, .pq) and CSV (.csv, .tsv)
files. Files that the export itself writes (e.g. results of an earlier run in
the same folder) are skipped.

output templates may use {stem}, {name}, {parent} (folder name), {dir} (folder
path) and {range}. Relative templates from a configuration are resolved next to each input file; a
template given with -o is resolved against the current directory. Use -o - to
write to standard output.
"""


def _separator(text: str) -> str:
    named = {"tab": "\t", "\\t": "\t", "comma": ",", "semicolon": ";", "pipe": "|", "space": " "}
    value = named.get(text.lower(), text)
    if len(value) != 1:
        raise argparse.ArgumentTypeError("separator must be a single character (or tab/comma/semicolon/pipe/space)")
    return value


def _agg_method(text: str) -> str:
    value = AGG_METHOD_ALIASES.get(text, text)
    if value not in AGG_METHODS:
        raise argparse.ArgumentTypeError(f"choose from {', '.join(AGG_METHODS)} (aliases: nth, points)")
    return value


def _rename(text: str) -> tuple[str, str]:
    if "=" not in text:
        raise argparse.ArgumentTypeError("use OLD=NEW")
    old, new = text.split("=", 1)
    return old, new


def _scale(text: str) -> tuple[str, float]:
    column, _, value = text.rpartition("=")
    if not column:
        raise argparse.ArgumentTypeError("use COLUMN=FACTOR, e.g. altitude_m=3.28084 or speed=1/3.6")
    try:
        factor = parse_number(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    if factor == 0:
        raise argparse.ArgumentTypeError("the factor must not be zero")
    return column, factor


def _offset(text: str) -> tuple[str, float | str]:
    column, _, value = text.rpartition("=")
    if not column:
        raise argparse.ArgumentTypeError("use COLUMN=VALUE, e.g. temp_c=32 or timestamp=2h")
    try:
        return column, parse_number(value)
    except ValueError:
        if is_duration(value.strip().lstrip("-")):
            return column, value.strip()
        raise argparse.ArgumentTypeError(f"{value!r} is neither a number nor a duration such as 2h") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parquetry",
        description="Explore, aggregate and export Parquet and CSV files. Run without a command to open the UI.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    ui = sub.add_parser("ui", help="open the desktop UI (default)", description="Open the desktop UI.")
    ui.add_argument("file", nargs="?", help="Parquet or CSV file to open directly (skips the file browser)")
    ui.add_argument("-c", "--config", help="processing configuration to apply after opening the file")
    ui.add_argument("--quit-after", type=float, help=argparse.SUPPRESS)  # seconds; used by packaging smoke tests

    info = sub.add_parser("info", help="show schema and metadata of a Parquet or CSV file")
    info.add_argument("file", help="Parquet or CSV file")
    info.add_argument("--x", help="column whose value range is reported (default: auto-detected time column)")
    info.add_argument("--stats", action="store_true", help="compute min/max/mean/null counts (reads all data)")
    info.add_argument("--json", action="store_true", help="machine readable output")

    exp = sub.add_parser(
        "export",
        help="export Parquet or CSV data to CSV using a configuration and/or options",
        description="Filter, aggregate and export Parquet and CSV files to CSV. Options given on the "
        "command line override the values from --config.",
        epilog=EXPORT_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    exp.add_argument("inputs", nargs="+", metavar="INPUT", help="Parquet or CSV files, directories or glob patterns")
    exp.add_argument("-c", "--config", help="JSON processing configuration (e.g. saved from the UI)")
    exp.add_argument("-o", "--output", help="output file or template, a directory, or - for stdout")
    exp.add_argument("-r", "--recursive", action="store_true", help="search directories recursively")

    sel = exp.add_argument_group("data selection")
    sel.add_argument("--x", help=f"x axis column ({ROW_INDEX} for the row number)")
    sel.add_argument("--y", nargs="+", metavar="COL", help="y parameter columns")
    sel.add_argument(
        "--range",
        nargs="+",
        action="append",
        metavar="START END [LABEL]",
        dest="ranges",
        help="keep only this x range (repeatable). Use - for an open end; the optional label names "
        "the file with --split-ranges",
    )
    sel.add_argument("--range-mode", choices=("absolute", "relative"), help="interpret ranges as absolute values or offsets from the first x value")
    sel.add_argument("--all-data", action="store_true", help="ignore ranges from the configuration")
    sel.add_argument("--split-ranges", action="store_true", default=None, help="write one file per range")
    sel.add_argument("--no-sort", action="store_true", help="skip sorting by x (only for files already sorted)")

    scl = exp.add_argument_group("scaling (value * FACTOR + OFFSET, before ranges and aggregation)")
    scl.add_argument(
        "--scale", type=_scale, action="append", metavar="COL=FACTOR",
        help="multiply a column by FACTOR (repeatable), e.g. altitude_m=3.28084 or speed=1/3.6",
    )
    scl.add_argument(
        "--offset", type=_offset, action="append", metavar="COL=VALUE",
        help="add VALUE to a column (repeatable); times are shifted by seconds or a duration such as timestamp=2h",
    )

    agg = exp.add_argument_group("aggregation")
    agg.add_argument("--agg", type=_agg_method, metavar="METHOD", help=f"one of {', '.join(AGG_METHODS)}")
    agg.add_argument("--every", help="interval bucket width: duration (10s, 1m, 1h) or number for numeric x")
    agg.add_argument("--n", type=int, help="keep every Nth row (every_nth)")
    agg.add_argument("--points", type=int, help="number of buckets (target_points)")
    agg.add_argument("--func", nargs="+", choices=AGG_FUNCTIONS, metavar="FUNC", help=f"aggregation functions: {', '.join(AGG_FUNCTIONS)}")

    fmt = exp.add_argument_group("CSV format")
    fmt.add_argument("--sep", "--separator", type=_separator, dest="separator", help="field separator (default ,)")
    fmt.add_argument("--time-format", choices=TIME_FORMATS, help="how the x column is written for date/time data")
    fmt.add_argument("--datetime-format", help="strftime format for --time-format custom, e.g. '%%Y-%%m-%%d %%H:%%M:%%S'")
    fmt.add_argument("--float-precision", type=int, help="digits after the decimal point")
    fmt.add_argument("--decimal-comma", action="store_true", default=None, help="write 1,5 instead of 1.5")
    fmt.add_argument("--null-value", help="text for missing values (default empty)")
    fmt.add_argument("--quote-style", choices=QUOTE_STYLES)
    fmt.add_argument("--no-header", action="store_true", help="omit the header row")
    fmt.add_argument("--bom", action="store_true", default=None, help="write a UTF-8 byte order mark (for Excel)")
    fmt.add_argument("--crlf", action="store_true", default=None, help="Windows line endings")
    fmt.add_argument("--rename", type=_rename, action="append", metavar="OLD=NEW", help="rename an output column (repeatable)")

    run = exp.add_argument_group("run control")
    run.add_argument("--save-config", metavar="PATH", help="write the effective configuration to PATH")
    run.add_argument("--dry-run", action="store_true", help="show what would be written without exporting")
    run.add_argument("--skip-existing", action="store_true", help="do not overwrite existing output files")
    run.add_argument("--fail-fast", action="store_true", help="stop at the first file that fails")
    run.add_argument("-q", "--quiet", action="store_true", help="only print errors")

    smp = sub.add_parser("sample", help="write a synthetic telemetry Parquet file to try things out")
    smp.add_argument("path", help="output .parquet file (.csv or .tsv writes CSV)")
    smp.add_argument("--rows", type=int, default=1_000_000, help="number of rows (default 1,000,000)")
    smp.add_argument("--rate", type=float, default=100.0, help="sample rate in Hz (default 100)")
    return parser


def export_parser() -> argparse.ArgumentParser:
    """The ``export`` sub-command parser (used by the UI to show CLI help)."""
    parser = build_parser()
    for action in parser._subparsers._group_actions:  # noqa: SLF001 - argparse has no public accessor
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return action.choices["export"]
    raise RuntimeError("export parser missing")


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and not argv[0].startswith("-")):
        argv.insert(0, "ui")
    args = build_parser().parse_args(argv)
    try:
        if args.command == "ui":
            return _cmd_ui(args)
        if args.command == "info":
            return _cmd_info(args)
        if args.command == "export":
            return _cmd_export(args)
        if args.command == "sample":
            return _cmd_sample(args)
    except (ConfigError, FileNotFoundError, IsADirectoryError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    build_parser().print_help()
    return 0


# ---------------------------------------------------------------------------
# ui
# ---------------------------------------------------------------------------


def _cmd_ui(args: argparse.Namespace) -> int:
    try:
        from .ui.app import run
    except ImportError as exc:
        print(
            f"error: the UI needs PySide6 and pyqtgraph ({exc}).\n"
            "Install them with:  pip install 'parquetry[ui]'",
            file=sys.stderr,
        )
        return 3
    return run(args.file, args.config, quit_after=args.quit_after)


# ---------------------------------------------------------------------------
# info
# ---------------------------------------------------------------------------


def _cmd_info(args: argparse.Namespace) -> int:
    from .processing import data_bounds, to_epoch_us

    info = inspect_file(args.file)
    x = args.x or info.guess_x()
    if not info.has_column(x):
        raise ValueError(f"Column {x!r} not found")
    bounds = data_bounds(info, x) if info.num_rows else None
    stats = _column_stats(info) if args.stats else {}

    if args.json:
        payload = {
            "path": str(info.path),
            "format": info.format,
            **({"csv": {"separator": info.csv.separator, "decimal_comma": info.csv.decimal_comma}} if info.csv else {}),
            "size_bytes": info.size_bytes,
            "rows": info.num_rows,
            "row_groups": info.num_row_groups,
            "created_by": info.created_by,
            "compression": info.compression,
            "suggested_x": x,
            "x_range": [str(bounds.lo), str(bounds.hi)] if bounds else None,
            "columns": [
                {"name": c.name, "dtype": c.dtype_name, "kind": c.kind, **({"stats": stats[c.name]} if c.name in stats else {})}
                for c in info.columns
            ],
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    rows = [
        ("File", str(info.path)),
        ("Format", info.format_label),
        ("Size", format_bytes(info.size_bytes)),
        ("Rows", f"{info.num_rows:,}"),
        ("Columns", str(len(info.columns))),
    ]
    if info.num_row_groups is not None:
        rows.append(("Row groups", str(info.num_row_groups)))
    if info.compression:
        rows.append(("Compression", info.compression))
    if info.created_by:
        rows.append(("Created by", info.created_by))
    x_label = ROW_INDEX_LABEL if x == ROW_INDEX else x
    if bounds is not None:
        span = ""
        if info.x_kind(x) == "datetime":
            span = f"  ({format_seconds((to_epoch_us(bounds.hi) - to_epoch_us(bounds.lo)) / 1e6)})"
        rows.append((f"X range [{x_label}]", f"{bounds.lo} -> {bounds.hi}{span}"))
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key:<{width}}  {value}")
    print()
    headers = ["#", "Column", "Type", "Role"]
    if stats:
        headers += ["Nulls", "Min", "Max", "Mean"]
    table = []
    for i, col in enumerate(info.columns):
        role = {
            "datetime": "time (x)", "date": "time (x)", "numeric": "numeric", "boolean": "boolean",
            "duration": "duration", "text": "text",
        }.get(col.kind, "-")
        line = [str(i), col.name, col.dtype_name, role]
        if stats:
            s = stats.get(col.name, {})
            line += [str(s.get("nulls", "")), _fmt(s.get("min")), _fmt(s.get("max")), _fmt(s.get("mean"))]
        table.append(line)
    widths = [max(len(h), *(len(r[i]) for r in table)) if table else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
    print("  ".join("-" * w for w in widths))
    for line in table:
        print("  ".join(v.ljust(w) for v, w in zip(line, widths)).rstrip())
    return 0


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def _column_stats(info) -> dict[str, dict]:
    import polars as pl

    exprs = []
    kinds = {}
    for col in info.columns:
        if col.kind in {"numeric", "boolean", "datetime", "date", "duration"}:
            exprs += [pl.col(col.name).null_count().alias(f"{col.name}\0nulls")]
            exprs += [pl.col(col.name).min().alias(f"{col.name}\0min"), pl.col(col.name).max().alias(f"{col.name}\0max")]
            if col.kind in {"numeric", "boolean"}:
                exprs.append(pl.col(col.name).cast(pl.Float64).mean().alias(f"{col.name}\0mean"))
            kinds[col.name] = col.kind
        else:
            exprs.append(pl.col(col.name).null_count().alias(f"{col.name}\0nulls"))
    if not exprs:
        return {}
    row = info.scan().select(exprs).collect().row(0, named=True)
    stats: dict[str, dict] = {}
    for key, value in row.items():
        name, stat = key.split("\0")
        stats.setdefault(name, {})[stat] = value
    return stats


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def config_from_args(args: argparse.Namespace) -> ProcessingConfig:
    """Merge ``--config`` with command line overrides."""
    cfg = ProcessingConfig.load(args.config) if args.config else ProcessingConfig()
    if args.x:
        cfg.x = args.x
    if args.y:
        cfg.y = list(args.y)
    if args.all_data:
        cfg.ranges = []
    if args.ranges:
        for values in args.ranges:
            if len(values) not in (2, 3):
                raise ConfigError(f"--range takes START END [LABEL], got {' '.join(values)!r}")
        cfg.ranges = [TimeRange.from_value(values) for values in args.ranges]
    if args.range_mode:
        cfg.range_mode = args.range_mode
    if args.split_ranges:
        cfg.output.split_ranges = True
    if args.no_sort:
        cfg.sort = False
    for column, factor in args.scale or []:
        cfg.scaling[column] = replace(cfg.scaling.get(column) or Scaling(), scale=factor)
    for column, offset in args.offset or []:
        cfg.scaling[column] = replace(cfg.scaling.get(column) or Scaling(), offset=offset)
    agg = cfg.aggregation
    if args.agg:
        agg.method = args.agg
    if args.every is not None:
        agg.every = args.every
        if not args.agg and agg.method == "none":
            agg.method = "interval"
    if args.n is not None:
        agg.n = args.n
        if not args.agg and agg.method == "none":
            agg.method = "every_nth"
    if args.points is not None:
        agg.points = args.points
        if not args.agg and agg.method == "none":
            agg.method = "target_points"
    if args.func:
        agg.functions = list(args.func)
    csv = cfg.csv
    if args.separator is not None:
        csv.separator = args.separator
    if args.time_format:
        csv.time_format = args.time_format
    if args.datetime_format:
        csv.datetime_format = args.datetime_format
        if not args.time_format:
            csv.time_format = "custom"
    if args.float_precision is not None:
        csv.float_precision = args.float_precision
    if args.decimal_comma:
        csv.decimal_comma = True
        if args.separator is None and csv.separator == ",":
            csv.separator = ";"
    if args.null_value is not None:
        csv.null_value = args.null_value
    if args.quote_style:
        csv.quote_style = args.quote_style
    if args.no_header:
        csv.include_header = False
    if args.bom:
        csv.include_bom = True
    if args.crlf:
        csv.line_terminator = "crlf"
    for old, new in args.rename or []:
        csv.rename[old] = new
    return cfg


def config_to_args(cfg: ProcessingConfig) -> list[str]:
    """Command line options equivalent to *cfg* (inverse of :func:`config_from_args`)."""
    default = ProcessingConfig()
    args = ["--x", cfg.x, "--y", *cfg.y]
    for column, scaling in cfg.scaling.items():
        if scaling.scale != 1:
            args += ["--scale", f"{column}={_number(scaling.scale)}"]
        if scaling.offset_value != 0:
            offset = scaling.offset if isinstance(scaling.offset, str) else _number(scaling.offset)
            args += ["--offset", f"{column}={offset}"]
    for rng in cfg.ranges:
        args += ["--range", _bound_arg(rng.start), _bound_arg(rng.end), *([rng.label] if rng.label else [])]
    if cfg.range_mode != default.range_mode:
        args += ["--range-mode", cfg.range_mode]
    if cfg.output.split_ranges:
        args.append("--split-ranges")
    if not cfg.sort:
        args.append("--no-sort")
    agg = cfg.aggregation
    if agg.method != "none":
        args += ["--agg", agg.method]
        if agg.method == "every_nth":
            args += ["--n", str(agg.n)]
        elif agg.method == "interval":
            args += ["--every", str(agg.every)]
        elif agg.method == "target_points":
            args += ["--points", str(agg.points)]
        if agg.is_bucketed:
            args += ["--func", *agg.functions]
    csv, dcsv = cfg.csv, default.csv
    if csv.separator != dcsv.separator:
        args += ["--sep", {"\t": "tab", " ": "space"}.get(csv.separator, csv.separator)]
    if csv.time_format != dcsv.time_format:
        args += ["--time-format", csv.time_format]
    if csv.time_format == "custom":
        args += ["--datetime-format", csv.datetime_format]
    if csv.float_precision is not None:
        args += ["--float-precision", str(csv.float_precision)]
    if csv.decimal_comma:
        args.append("--decimal-comma")
    if csv.null_value != dcsv.null_value:
        args += ["--null-value", csv.null_value]
    if csv.quote_style != dcsv.quote_style:
        args += ["--quote-style", csv.quote_style]
    if not csv.include_header:
        args.append("--no-header")
    if csv.include_bom:
        args.append("--bom")
    if csv.line_terminator == "crlf":
        args.append("--crlf")
    for old, new in csv.rename.items():
        args += ["--rename", f"{old}={new}"]
    return args


def _bound_arg(value) -> str:
    return "-" if value is None else str(value)


def _number(value: float) -> str:
    """Shortest text that reads back as the same number."""
    value = float(value)
    return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)


def command_line(inputs: Sequence[str], cfg: ProcessingConfig | None = None, config_path: str | None = None,
                 output: str | None = None) -> str:
    """A shell command that reproduces an export, for display in the UI.

    A relative *output* template is meant relative to each input file (as in
    configurations), so it is anchored with ``{dir}`` because ``-o`` would
    otherwise resolve it against the current directory.
    """
    import os
    import shlex
    import subprocess

    if output and output != "-" and not Path(output).expanduser().is_absolute() and not output.startswith("{dir}"):
        output = os.path.join("{dir}", output)

    args = ["parquetry", "export", *inputs]
    if config_path:
        args += ["--config", config_path]
    elif cfg is not None:
        args += config_to_args(cfg)
    if output:
        args += ["-o", output]
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def _output_template(output: str | None, cfg: ProcessingConfig, multiple: bool) -> str | None:
    """Resolve ``-o``: a directory means "configured file name inside it"."""
    if output is None or output == "-":
        if output == "-" and multiple:
            raise ValueError("-o - (stdout) only works with a single input file")
        return output
    path = Path(output).expanduser()
    if output.endswith(("/", "\\")) or path.is_dir():
        return str(path / Path(cfg.output.path).name)
    if multiple and not any(p in output for p in ("{stem}", "{name}", "{parent}")):
        raise ValueError("With several inputs, -o must be a directory or contain {stem}, {name} or {parent}")
    return str(path)


def _cmd_export(args: argparse.Namespace) -> int:
    from .processing import (
        ProcessingError,
        batch_export,
        estimate_rows,
        exclude_outputs,
        find_inputs,
        plan_outputs,
        resolve_ranges,
    )

    cfg = config_from_args(args)
    inputs = find_inputs(args.inputs, recursive=args.recursive)
    if not inputs:
        raise ValueError("No Parquet or CSV files found")
    template = _output_template(args.output, cfg, len(inputs) > 1)
    base_dir = Path.cwd() if args.output else None
    log = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))
    inputs, own_outputs = exclude_outputs(inputs, cfg, template, base_dir)
    for path in own_outputs:
        log(f"{path}: skipped (written by this export)")
    if not inputs:
        raise ValueError("No input files left after skipping the files this export writes")

    missing_x = not cfg.x
    if missing_x or not cfg.y:
        # Fill gaps from the first file so quick ad-hoc exports need few options.
        first = inspect_file(inputs[0])
        if missing_x:
            cfg.x = first.guess_x()
        if not cfg.y:
            cfg.y = [c.name for c in first.y_candidates if c.name != cfg.x]
    cfg.validate()

    if args.save_config:
        saved = cfg.copy()
        saved.source = saved.source or str(inputs[0])
        path = saved.save(args.save_config)
        if not args.quiet:
            print(f"Saved configuration to {path}", file=sys.stderr)

    if args.dry_run:
        failures = 0
        for path in inputs:
            try:
                info = inspect_file(path)
                from .processing import validate_for

                validate_for(cfg, info)
                for plan in plan_outputs(info, cfg, template, base_dir):
                    estimate = estimate_rows(info, cfg, resolve_ranges(cfg, info, plan.ranges))
                    target = "<stdout>" if plan.path is None else plan.path
                    print(f"{path} -> {target}  (~{estimate:,} rows)")
            except (ProcessingError, ValueError, OSError) as exc:
                failures += 1
                print(f"{path}: error: {exc}", file=sys.stderr)
        return 1 if failures else 0

    failures = 0
    for item in batch_export(
        inputs,
        cfg,
        output=template,
        base_dir=base_dir,
        overwrite=not args.skip_existing,
        fail_fast=args.fail_fast,
    ):
        if item.error:
            failures += 1
            print(f"{item.input}: error: {item.error}", file=sys.stderr)
            continue
        for out, rows in item.result.outputs:
            if out is not None:
                log(f"{item.input} -> {out}  ({rows:,} rows)")
        for skipped in item.result.skipped:
            log(f"{item.input} -> {skipped}  (exists, skipped)")
    if failures and len(inputs) > 1:
        log(f"{failures} of {len(inputs)} file(s) failed")
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# sample
# ---------------------------------------------------------------------------


def _cmd_sample(args: argparse.Namespace) -> int:
    from .sample import generate_sample

    path = generate_sample(args.path, rows=args.rows, rate_hz=args.rate)
    print(f"Wrote {args.rows:,} rows to {path} ({format_bytes(path.stat().st_size)})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
