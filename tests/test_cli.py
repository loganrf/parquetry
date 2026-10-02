import json
import os
import shlex

import polars as pl
import pytest

from parquetry.cli import build_parser, command_line, config_from_args, config_to_args, main
from parquetry.config import AggregationConfig, CsvOptions, OutputOptions, ProcessingConfig, Scaling, TimeRange
from parquetry.dataset import inspect_file


def test_info_text(telemetry, capsys):
    assert main(["info", str(telemetry)]) == 0
    out = capsys.readouterr().out
    assert "600" in out and "time (x)" in out and "00:00:59.9" in out


def test_info_json_with_stats(telemetry, capsys):
    assert main(["info", str(telemetry), "--json", "--stats"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == 600
    assert payload["suggested_x"] == "time"
    speed = next(c for c in payload["columns"] if c["name"] == "speed")
    assert speed["stats"]["max"] == 599


def test_info_csv(tmp_path, capsys):
    path = tmp_path / "data.csv"
    path.write_text("t;v;mode\n2024-01-01 00:00:00;1,5;a\n2024-01-01 00:00:01;2,5;b\n")
    assert main(["info", str(path)]) == 0
    out = capsys.readouterr().out
    assert "CSV (semicolon separated, decimal comma)" in out and "time (x)" in out and "text" in out
    assert main(["info", str(path), "--json", "--stats"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["format"] == "csv" and payload["csv"] == {"separator": ";", "decimal_comma": True}
    assert next(c for c in payload["columns"] if c["name"] == "v")["stats"]["max"] == 2.5


def test_export_folder_of_csv_files_twice(tmp_path, capsys):
    for name in ("a", "b"):
        (tmp_path / f"{name}.csv").write_text("x,y,label\n1,2,p\n3,4,q\n")
    for _ in range(2):  # the second run must not export the first run's results
        assert main(["export", str(tmp_path), "--x", "x", "--y", "y", "label"]) == 0
    assert "a_export.csv: skipped (written by this export)" in capsys.readouterr().err
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.csv", "a_export.csv", "b.csv", "b_export.csv"]
    assert (tmp_path / "a_export.csv").read_text().splitlines() == ["x,y,label", "1,2,p", "3,4,q"]


def test_export_per_category(tmp_path, capsys):
    path = tmp_path / "sales.csv"
    path.write_text("region,revenue\nNorth,10\nSouth,20\nNorth,30\n")
    assert main(["export", str(path), "--x", "region", "--agg", "per_value", "--func", "mean", "-o", "-", "-q"]) == 0
    assert capsys.readouterr().out.splitlines() == ["region,revenue", "North,20.0", "South,20.0"]


def test_export_with_options(telemetry, tmp_path, capsys):
    out = tmp_path / "out.csv"
    code = main(
        [
            "export", str(telemetry), "--y", "speed", "rpm", "--agg", "interval", "--every", "30s",
            "--func", "mean", "max", "--sep", "tab", "-o", str(out), "--save-config", str(tmp_path / "c.json"),
        ]
    )
    assert code == 0
    df = pl.read_csv(out, separator="\t")
    assert df.columns == ["time", "speed_mean", "speed_max", "rpm_mean", "rpm_max"]
    assert df.height == 2
    saved = ProcessingConfig.load(tmp_path / "c.json")
    assert saved.aggregation.every == "30s" and saved.csv.separator == "\t"


def test_export_config_to_directory_for_many_files(telemetry, tmp_path, capsys):
    copy = tmp_path / "second.parquet"
    copy.write_bytes(telemetry.read_bytes())
    cfg = ProcessingConfig(x="time", y=["speed"], range_mode="relative", ranges=[TimeRange(0, 1)])
    cfg_path = cfg.save(tmp_path / "cfg.json")
    out_dir = tmp_path / "results"
    out_dir.mkdir()
    assert main(["export", str(tmp_path), "--config", str(cfg_path), "-o", str(out_dir)]) == 0
    assert sorted(p.name for p in out_dir.iterdir()) == ["second_export.csv", "telemetry_export.csv"]
    assert pl.read_csv(out_dir / "second_export.csv").height == 11


def test_export_stdout_and_defaults(telemetry, capsys):
    assert main(["export", str(telemetry), "--n", "300", "-o", "-", "-q"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "time,speed,temp,rpm,on,label"  # every parameter, text included
    assert len(lines) == 3


def test_export_dry_run(telemetry, capsys):
    assert main(["export", str(telemetry), "--y", "speed", "--range", "0", "10", "a", "--range", "20", "-", "b",
                 "--range-mode", "relative", "--split-ranges", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "telemetry_export_a.csv" in out and "telemetry_export_b.csv" in out
    assert not list(telemetry.parent.glob("*.csv"))


def test_export_errors(telemetry, tmp_path, capsys):
    assert main(["export", str(telemetry), "--y", "nope", "-o", str(tmp_path / "x.csv")]) == 1
    assert "not found" in capsys.readouterr().err
    assert main(["export", str(tmp_path / "missing.parquet")]) == 2
    assert main(["export", str(telemetry), "--range", "1", "2", "3", "4"]) == 2


def test_ui_is_default_command(monkeypatch):
    called = {}
    monkeypatch.setattr("parquetry.cli._cmd_ui", lambda args: called.setdefault("file", args.file) or 0)
    main(["some.parquet"])
    assert called["file"] == "some.parquet"


def test_sample(tmp_path, capsys):
    path = tmp_path / "s.parquet"
    assert main(["sample", str(path), "--rows", "1000"]) == 0
    assert pl.read_parquet(path).height == 1000
    assert main(["sample", str(tmp_path / "s.csv"), "--rows", "100"]) == 0
    info = inspect_file(tmp_path / "s.csv")
    assert info.num_rows == 100 and info.guess_x() == "timestamp" and info.column("flight_phase").kind == "text"


@pytest.mark.parametrize(
    "cfg",
    [
        ProcessingConfig(x="time", y=["a", "b"]),
        ProcessingConfig(
            x="time",
            y=["a"],
            ranges=[TimeRange("2024-01-01T00:00:00", None, "x"), TimeRange(None, "2024-01-02T00:00:00")],
            aggregation=AggregationConfig(method="interval", every="5m", functions=["min", "max"]),
            sort=False,
            csv=CsvOptions(
                separator="\t", float_precision=2, time_format="custom", datetime_format="%H:%M", null_value="NA",
                quote_style="always", include_header=False, include_bom=True, line_terminator="crlf",
                rename={"a_min": "low"},
            ),
            output=OutputOptions(split_ranges=True),
        ),
        ProcessingConfig(x="__row_index__", y=["a"], range_mode="relative", ranges=[TimeRange(-5, 7.5)],
                         aggregation=AggregationConfig(method="every_nth", n=3)),
        ProcessingConfig(x="d", y=["a"], aggregation=AggregationConfig(method="target_points", points=99, functions=["std"]),
                         csv=CsvOptions(separator=";", decimal_comma=True)),
        ProcessingConfig(x="region", y=["a", "b"], aggregation=AggregationConfig(method="per_value", functions=["mean", "last"])),
        ProcessingConfig(x="time", y=["a", "b"], scaling={
            "a": Scaling(1 / 3.6), "b": Scaling(9 / 5, -40.5), "time": Scaling(offset="-2h30m"), "c=d": Scaling(offset=7),
        }),
    ],
)
def test_config_to_args_round_trip(cfg):
    args = build_parser().parse_args(["export", "in.parquet", *config_to_args(cfg)])
    rebuilt = config_from_args(args)
    expected = cfg.to_dict()
    expected["output"]["path"] = rebuilt.output.path
    assert rebuilt.to_dict() == expected


def test_command_line_quotes():
    cmd = command_line(["my file.parquet"], ProcessingConfig(x="t", y=["a b"]))
    if os.name == "nt":
        assert cmd == 'parquetry export "my file.parquet" --x t --y "a b"'
    else:
        assert cmd == "parquetry export 'my file.parquet' --x t --y 'a b'"
        assert command_line(["*.parquet"], config_path="c.json") == "parquetry export '*.parquet' --config c.json"


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX shell quoting")
def test_generated_command_writes_next_to_input(telemetry, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    cfg = ProcessingConfig(x="time", y=["speed"], range_mode="relative", ranges=[TimeRange(0, 1, "a"), TimeRange(2, 3, "b")])
    cfg.output.split_ranges = True
    cmd = command_line([str(telemetry)], cfg, output="{stem}_part.csv")
    assert "{dir}/{stem}_part.csv" in cmd
    assert main(shlex.split(cmd)[1:]) == 0
    assert sorted(p.name for p in telemetry.parent.glob("*.csv")) == ["telemetry_part_a.csv", "telemetry_part_b.csv"]
    assert not list(elsewhere.iterdir())


def test_export_with_scaling(telemetry, capsys):
    args = ["export", str(telemetry), "--y", "speed", "rpm", "--scale", "speed=1/2", "--offset", "speed=10",
            "--offset", "time=1h", "--n", "300", "-o", "-", "-q"]
    assert main(args) == 0
    assert capsys.readouterr().out.splitlines() == [
        "time,speed,rpm", "2024-01-01T01:00:00.000000,10.0,0", "2024-01-01T01:00:30.000000,160.0,600",
    ]
    for bad in (["--scale", "speed=0"], ["--scale", "=2"], ["--offset", "speed=soon"]):
        with pytest.raises(SystemExit):
            main(["export", str(telemetry), *bad])
    assert "must not be zero" in capsys.readouterr().err
    assert main(["export", str(telemetry), "--y", "label", "--scale", "label=2", "-o", "-"]) == 1
    assert "cannot be scaled" in capsys.readouterr().err
