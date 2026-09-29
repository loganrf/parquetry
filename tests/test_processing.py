import datetime as dt
import io

import numpy as np
import polars as pl
import pytest

from parquetry.config import AggregationConfig, CsvOptions, OutputOptions, ProcessingConfig, TimeRange
from parquetry.dataset import ROW_INDEX, inspect_parquet
from parquetry.processing import (
    PlotData,
    PlotSeries,
    ProcessingError,
    batch_export,
    data_bounds,
    estimate_rows,
    export_file,
    export_query,
    find_inputs,
    load_plot_data,
    merge_plot_data,
    plan_outputs,
    preview_csv,
    render_output_path,
)

from .conftest import START


def cfg(**kwargs) -> ProcessingConfig:
    base = {"x": "time", "y": ["speed"]}
    base.update(kwargs)
    return ProcessingConfig(**base)


def run(path, config: ProcessingConfig) -> pl.DataFrame:
    return export_query(inspect_parquet(path), config).collect()


def read_csv(path, **kwargs) -> pl.DataFrame:
    return pl.read_csv(path, **kwargs)


# -- inspection -------------------------------------------------------------------------


def test_inspect(telemetry):
    info = inspect_parquet(telemetry)
    assert info.num_rows == 600
    assert info.guess_x() == "time"
    assert [c.name for c in info.y_candidates] == ["speed", "temp", "rpm", "on"]
    assert info.column("time").dtype_name == "Datetime[us]"
    assert info.default_y("time") == ["speed", "temp"]


def test_inspect_rejects_non_parquet(tmp_path):
    bogus = tmp_path / "x.parquet"
    bogus.write_text("not parquet")
    with pytest.raises(ValueError, match="not a readable Parquet file"):
        inspect_parquet(bogus)


def test_bounds(telemetry):
    info = inspect_parquet(telemetry)
    b = data_bounds(info, "time")
    assert b.lo == START and b.hi == START + dt.timedelta(seconds=59.9)
    assert data_bounds(info, ROW_INDEX).hi == 599


# -- ranges -----------------------------------------------------------------------------


def test_absolute_range_is_inclusive(telemetry):
    df = run(telemetry, cfg(ranges=[TimeRange("2024-01-01T00:00:10", "2024-01-01T00:00:20")]))
    assert df.height == 101
    assert df["speed"][0] == 100 and df["speed"][-1] == 200


def test_relative_ranges_and_open_ends(telemetry):
    config = cfg(range_mode="relative", ranges=[TimeRange(0, 1), TimeRange("59s", None)])
    df = run(telemetry, config)
    assert df.height == 11 + 10
    assert df["speed"].to_list()[:3] == [0, 1, 2]
    assert df["speed"][-1] == 599


def test_range_end_before_start(telemetry):
    with pytest.raises(ProcessingError, match="end is before start"):
        run(telemetry, cfg(ranges=[TimeRange("2024-01-01T00:00:20", "2024-01-01T00:00:10")]))


def test_bad_range_value(telemetry):
    with pytest.raises(ProcessingError, match="Cannot parse"):
        run(telemetry, cfg(ranges=[TimeRange("yesterday", None)]))


def test_time_zone_aware_ranges(tz_aware):
    config = ProcessingConfig(x="ts", y=["v"], ranges=[TimeRange("2024-01-01T02:00:00", "2024-01-01T03:00:00")])
    assert run(tz_aware, config)["v"].to_list() == [2.0, 3.0]  # naive = column time zone
    config.ranges = [TimeRange("2024-01-01T01:00:00Z", None)]  # 01:00 UTC == 02:00 Berlin
    assert run(tz_aware, config)["v"].to_list() == [2.0, 3.0, 4.0, 5.0]


# -- aggregation --------------------------------------------------------------------------


def test_interval_aggregation(telemetry):
    agg = AggregationConfig(method="interval", every="10s", functions=["mean", "max", "count"])
    df = run(telemetry, cfg(y=["speed", "temp", "on"], aggregation=agg))
    assert df.height == 6
    assert df["time"][0] == START and df["time"][1] == START + dt.timedelta(seconds=10)
    assert df["speed_mean"][0] == pytest.approx(49.5)
    assert df["speed_max"][0] == 99
    assert df["temp_count"][0] == 99  # one null in the first 10 s
    assert df["on_mean"][0] == pytest.approx(0.5)


def test_interval_given_as_seconds_for_datetime(telemetry):
    agg = AggregationConfig(method="interval", every=30, functions=["min"])
    df = run(telemetry, cfg(aggregation=agg))
    assert df["speed"].to_list() == [0, 300]


def test_target_points(telemetry):
    agg = AggregationConfig(method="target_points", points=6, functions=["mean"])
    df = run(telemetry, cfg(aggregation=agg))
    assert df.height == 6
    assert df["speed"][0] == pytest.approx(49.5)


def test_target_points_within_ranges(telemetry):
    agg = AggregationConfig(method="target_points", points=2, functions=["first", "last"])
    config = cfg(aggregation=agg, range_mode="relative", ranges=[TimeRange(10, 20)])
    df = run(telemetry, config)
    assert df.height == 2
    assert df["speed_first"][0] == 100
    assert df["speed_last"][1] == 200


def test_every_nth(telemetry):
    df = run(telemetry, cfg(aggregation=AggregationConfig(method="every_nth", n=100)))
    assert df["speed"].to_list() == [0, 100, 200, 300, 400, 500]


def test_unsorted_input_is_sorted(unsorted):
    config = ProcessingConfig(x="time", y=["value"], aggregation=AggregationConfig(method="interval", every="10s", functions=["first"]))
    df = run(unsorted, config)
    assert df["time"].is_sorted()
    assert df["value"].to_list() == [0, 10, 20, 30, 40]


def test_numeric_x(numeric_x):
    config = ProcessingConfig(x="distance", y=["force"], aggregation=AggregationConfig(method="interval", every=10, functions=["max"]))
    df = run(numeric_x, config)
    assert df.height == 10
    assert df["distance"].to_list()[:2] == [0.0, 10.0]
    assert df["force"][0] == 81.0
    config.aggregation = AggregationConfig(method="target_points", points=4, functions=["count"])
    assert run(numeric_x, config)["force"].to_list() == [25, 25, 25, 25]


def test_numeric_x_rejects_duration_interval(numeric_x):
    config = ProcessingConfig(x="distance", y=["force"], aggregation=AggregationConfig(method="interval", every="1m"))
    with pytest.raises(ProcessingError, match="is a duration but x column"):
        run(numeric_x, config)


def test_row_index_x(telemetry):
    config = ProcessingConfig(x=ROW_INDEX, y=["speed"], aggregation=AggregationConfig(method="interval", every=200, functions=["mean"]))
    df = run(telemetry, config)
    assert df.columns == ["row_index", "speed"]
    assert df["row_index"].to_list() == [0, 200, 400]


@pytest.mark.parametrize(
    "change, message",
    [
        ({"y": ["nope"]}, "not found"),
        ({"y": ["label"]}, "Only numeric or boolean"),
        ({"x": "label"}, "cannot be used as the x axis"),
        ({"aggregation": AggregationConfig(method="interval", every="abc")}, "neither a duration"),
    ],
)
def test_validation(telemetry, change, message):
    with pytest.raises(ProcessingError, match=message):
        run(telemetry, cfg(**change))


# -- CSV output ----------------------------------------------------------------------------


def test_export_default(telemetry):
    result = export_file(telemetry, cfg(y=["speed", "temp"]))
    out = telemetry.with_name("telemetry_export.csv")
    assert result.outputs == [(out, 600)]
    df = read_csv(out)
    assert df.columns == ["time", "speed", "temp"]
    assert out.read_text().splitlines()[1] == "2024-01-01T00:00:00.000000,0.0,20.0"


@pytest.mark.parametrize(
    "fmt, expected",
    [
        ("epoch_s", "1704067200.1"),
        ("epoch_ms", "1704067200100"),
        ("elapsed_s", "0.1"),
    ],
)
def test_time_formats(telemetry, fmt, expected):
    buf = io.StringIO()
    export_file(telemetry, cfg(csv=CsvOptions(time_format=fmt)), output="-", stream=buf)
    assert buf.getvalue().splitlines()[2].split(",")[0] == expected


def test_custom_format_and_options(telemetry):
    csv = CsvOptions(
        separator=";",
        decimal_comma=True,
        float_precision=1,
        time_format="custom",
        datetime_format="%H:%M:%S%.3f",
        null_value="NA",
        rename={"speed": "Speed [m/s]"},
        line_terminator="crlf",
    )
    config = cfg(y=["speed", "temp"], csv=csv, range_mode="relative", ranges=[TimeRange(5, 5)])
    buf = io.StringIO()
    export_file(telemetry, config, output="-", stream=buf)
    assert buf.getvalue() == "time;Speed [m/s];temp\r\n00:00:05.000;50,0;NA\r\n"


def test_no_header_and_quote_all(telemetry):
    config = cfg(csv=CsvOptions(include_header=False, quote_style="always"), ranges=[TimeRange(None, "2024-01-01T00:00:00.1")])
    buf = io.StringIO()
    export_file(telemetry, config, output="-", stream=buf)
    assert buf.getvalue().splitlines() == ['"2024-01-01T00:00:00.000000","0.0"', '"2024-01-01T00:00:00.100000","1.0"']


def test_duplicate_headers_rejected(telemetry):
    config = cfg(y=["speed", "temp"], csv=CsvOptions(rename={"temp": "speed"}))
    with pytest.raises(ProcessingError, match="Duplicate output column"):
        run(telemetry, config)


def test_split_ranges(telemetry, tmp_path):
    config = cfg(
        range_mode="relative",
        ranges=[TimeRange(0, 1, "start"), TimeRange(10, 12)],
        output=OutputOptions(path=str(tmp_path / "out" / "{stem}.csv"), split_ranges=True),
    )
    result = export_file(telemetry, config)
    paths = [p for p, _ in result.outputs]
    assert paths == [tmp_path / "out" / "telemetry_start.csv", tmp_path / "out" / "telemetry_2.csv"]
    assert [rows for _, rows in result.outputs] == [11, 21]
    assert not list((tmp_path / "out").glob(".*partial*"))


def test_output_templates(telemetry, tmp_path):
    info = inspect_parquet(telemetry)
    assert render_output_path("{parent}/{stem}_{range}.csv", info.path, "a b") == info.path.parent / info.path.parent.name / "telemetry_a_b.csv"
    with pytest.raises(ProcessingError, match="placeholders"):
        render_output_path("{nope}.csv", info.path)
    with pytest.raises(ProcessingError, match="Refusing to overwrite the input"):
        plan_outputs(info, cfg(output=OutputOptions(path="{name}")))


def test_skip_existing(telemetry):
    export_file(telemetry, cfg())
    result = export_file(telemetry, cfg(), overwrite=False)
    assert result.outputs == [] and len(result.skipped) == 1


def test_preview(telemetry):
    text = preview_csv(telemetry, cfg(y=["speed", "rpm"]), rows=3)
    assert text.splitlines() == [
        "time,speed,rpm",
        "2024-01-01T00:00:00.000000,0.0,0",
        "2024-01-01T00:00:00.100000,1.0,2",
        "2024-01-01T00:00:00.200000,2.0,4",
    ]


def test_estimate_rows(telemetry):
    info = inspect_parquet(telemetry)
    assert estimate_rows(info, cfg()) == 600
    assert estimate_rows(info, cfg(aggregation=AggregationConfig(method="every_nth", n=7))) == 86
    assert estimate_rows(info, cfg(aggregation=AggregationConfig(method="interval", every="1s"))) == 60
    assert estimate_rows(info, cfg(aggregation=AggregationConfig(method="target_points", points=50))) == 50


# -- batch ---------------------------------------------------------------------------------


def test_find_inputs_and_batch(telemetry, tmp_path):
    other = tmp_path / "sub" / "copy.parquet"
    other.parent.mkdir()
    other.write_bytes(telemetry.read_bytes())
    broken = tmp_path / "broken.parquet"
    broken.write_text("nope")
    assert find_inputs([tmp_path]) == sorted([broken, telemetry])
    assert set(find_inputs([tmp_path], recursive=True)) == {broken, telemetry, other}
    assert find_inputs([str(tmp_path / "*" / "*.parquet")]) == [other]

    items = list(batch_export([telemetry, broken, other], cfg(), output=str(tmp_path / "out" / "{stem}.csv")))
    assert [i.error is None for i in items] == [True, False, True]
    assert (tmp_path / "out" / "copy.csv").exists()
    items = list(batch_export([broken, telemetry], cfg(), fail_fast=True))
    assert len(items) == 1


# -- plot data -----------------------------------------------------------------------------


def test_plot_data_full(telemetry):
    data = load_plot_data(telemetry, cfg(y=["speed", "on"]))
    assert not data.reduced
    assert [s.name for s in data.series] == ["speed", "on"]
    assert data.series[0].x[1] - data.series[0].x[0] == pytest.approx(0.1)
    assert data.bounds[0] == pytest.approx(START.replace(tzinfo=dt.timezone.utc).timestamp())


def test_plot_data_reduced_keeps_peaks(telemetry):
    data = load_plot_data(telemetry, cfg(y=["speed", "temp"]), max_points=100, envelope_buckets=20)
    assert data.reduced
    speed = data.series[0]
    assert len(speed.x) == 40
    assert speed.y.max() == 599 and speed.y.min() == 0
    assert np.all(np.diff(speed.x) >= 0)


def test_plot_detail_and_merge(telemetry):
    config = cfg()
    overview = load_plot_data(telemetry, config, max_points=100, envelope_buckets=10)
    lo = overview.bounds[0] + 10
    detail = load_plot_data(telemetry, config, view=(lo, lo + 5))
    assert not detail.reduced and len(detail.series[0].x) == 51
    merged = merge_plot_data(overview, detail)
    assert np.all(np.diff(merged.series[0].x) >= 0)
    assert len(merged.series[0].x) > len(overview.series[0].x)


def test_merge_keeps_unknown_series():
    x = np.arange(5.0)
    base = PlotData("x", "numeric", None, [PlotSeries("a", "a", None, x, x), PlotSeries("b", "b", None, x, x)], 5)
    detail = PlotData("x", "numeric", None, [PlotSeries("a", "a", None, np.array([2.5]), np.array([9.0]))], 1, view=(2, 3))
    merged = merge_plot_data(base, detail)
    assert merged.series[0].x.tolist() == [0, 1, 2.5, 4]
    assert merged.series[1] is base.series[1]
