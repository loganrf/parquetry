import pytest

from parquetry.config import AggregationConfig, ConfigError, CsvOptions, ProcessingConfig, TimeRange
from parquetry.durations import (
    format_seconds,
    parse_duration_us,
    parse_offset_seconds,
    seconds_to_duration,
    to_polars_duration,
)


def full_config() -> ProcessingConfig:
    return ProcessingConfig(
        x="time",
        y=["speed", "temp"],
        ranges=[TimeRange("2024-01-01T00:00:10", "2024-01-01T00:00:20", "first"), TimeRange(None, "2024-01-01T00:00:05")],
        aggregation=AggregationConfig(method="interval", every="1s", functions=["mean", "max"]),
        csv=CsvOptions(separator=";", float_precision=3, rename={"speed_mean": "v"}),
    )


def test_json_round_trip(tmp_path):
    cfg = full_config()
    path = cfg.save(tmp_path / "cfg.json")
    loaded = ProcessingConfig.load(path)
    assert loaded.to_dict() == cfg.to_dict()
    assert loaded.ranges[0].label == "first"
    assert loaded.ranges[1].start is None


def test_copy_is_deep():
    cfg = full_config()
    clone = cfg.copy()
    clone.y.append("rpm")
    clone.csv.rename["x"] = "y"
    assert "rpm" not in cfg.y
    assert "x" not in cfg.csv.rename


@pytest.mark.parametrize(
    "data, message",
    [
        ({"x": "t", "y": ["a"], "bogus": 1}, "Unknown configuration"),
        ({"x": "t", "y": ["a"], "aggregation": {"method": "wat"}}, "Unknown aggregation method"),
        ({"x": "t", "y": ["a"], "aggregation": {"method": "interval", "every": "1 fortnight"}}, "neither a duration"),
        ({"x": "t", "y": ["a"], "csv": {"separator": ";;"}}, "single character"),
        ({"x": "t", "y": ["a"], "csv": {"decimal_comma": True}}, "Decimal comma"),
        ({"x": "t", "y": []}, "No y parameters"),
        ({"x": "t", "y": ["a"], "version": 99}, "Unsupported configuration version"),
    ],
)
def test_validation_errors(data, message):
    with pytest.raises(ConfigError, match=message):
        ProcessingConfig.from_dict(data).validate()


def test_aliases_and_function_strings():
    agg = AggregationConfig.from_dict({"method": "points", "points": 10, "functions": "min, max"})
    assert agg.method == "target_points"
    assert agg.functions == ["min", "max"]
    assert AggregationConfig.from_dict("nth").method == "every_nth"


def test_output_columns_naming():
    cfg = ProcessingConfig(x="t", y=["a", "b", "t"])
    assert [c[0] for c in cfg.output_columns()] == ["a", "b"]
    cfg.aggregation = AggregationConfig(method="interval", functions=["max"])
    assert cfg.output_columns() == [("a", "a", "max"), ("b", "b", "max")]
    cfg.aggregation.functions = ["mean", "max"]
    assert [c[0] for c in cfg.output_columns()] == ["a_mean", "a_max", "b_mean", "b_max"]


def test_range_bounds_are_normalised():
    rng = TimeRange.from_value(["0", "10.5", "warmup"])
    assert rng.start == 0 and rng.end == 10.5 and rng.label == "warmup"
    assert TimeRange.from_value(["-", "5m"]).start is None
    assert TimeRange.from_value({"start": "2024-01-01T00:00:00"}).end is None


def test_durations():
    assert parse_duration_us("1h30m") == 5400e6
    assert parse_duration_us("250ms") == 250e3
    assert to_polars_duration("1.5s") == "1500000us"
    assert to_polars_duration("1m") == "1m"
    assert parse_offset_seconds("-2m") == -120
    assert parse_offset_seconds(3) == 3.0
    assert seconds_to_duration(3723.5) == "1h2m3s500ms"
    assert format_seconds(3723.5) == "01:02:03.5"
    assert format_seconds(2.5) == "2.5 s"
    with pytest.raises(ValueError):
        parse_duration_us("5 minutes")
