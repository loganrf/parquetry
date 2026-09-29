import json
import os
import shlex

import polars as pl
import pytest

from parquetry.cli import build_parser, command_line, config_from_args, config_to_args, main
from parquetry.config import AggregationConfig, CsvOptions, OutputOptions, ProcessingConfig, TimeRange


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
    assert lines[0] == "time,speed,temp,rpm,on"
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
        # values argparse would otherwise mistake for options
        ProcessingConfig(
            x="-x", y=["-a", "b"], range_mode="relative",
            ranges=[TimeRange("-5m", "10m", "-warmup"), TimeRange(-5e-06, 0.5)],
            csv=CsvOptions(separator="-", null_value="-nan", rename={"-a": "-b"}),
        ),
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


def test_export_refuses_colliding_outputs(telemetry, tmp_path, capsys):
    copy = tmp_path / "copy.parquet"
    copy.write_bytes(telemetry.read_bytes())
    cfg_path = ProcessingConfig(x="time", y=["speed"], output=OutputOptions(path="export.csv")).save(tmp_path / "c.json")
    assert main(["export", str(telemetry), str(copy), "--config", str(cfg_path)]) == 2
    assert "would both be written" in capsys.readouterr().err
    assert not list(tmp_path.glob("*.csv"))


def test_stdout_is_written_as_utf8_bytes(telemetry, capsysbinary):
    assert main(["export", str(telemetry), "--y", "speed", "--range", "-", "2024-01-01T00:00:00", "-o", "-",
                 "--crlf", "--bom", "--rename", "speed=Geschwindigkeit µ"]) == 0
    out = capsysbinary.readouterr().out
    assert out == "\ufefftime,Geschwindigkeit µ\r\n2024-01-01T00:00:00.000000,0.0\r\n".encode()
