"""CSV input: detection, column types, processing and folders of CSV files."""

import datetime as dt

import polars as pl
import pytest

from parquetry.config import AggregationConfig, OutputOptions, ProcessingConfig
from parquetry.dataset import CSV_INFER_ROWS, inspect_file, sniff_csv
from parquetry.processing import exclude_outputs, export_file, export_query, find_inputs, load_plot_data

from .conftest import START


def write(path, text, encoding="utf-8"):
    path.write_text(text, encoding=encoding)
    return path


def test_inspect_csv(tmp_path):
    path = write(
        tmp_path / "data.csv",
        "﻿time,speed,count,mode,ok\n"
        "2024-01-01T00:00:00.000000,1.5,1,RUN,true\n"
        "2024-01-01T00:00:00.100000,NA,2,IDLE,false\n",
    )
    info = inspect_file(path)
    assert info.format == "csv" and info.format_label == "CSV (comma separated)"
    assert info.num_rows == 2
    assert {c.name: c.kind for c in info.columns} == {
        "time": "datetime", "speed": "numeric", "count": "numeric", "mode": "text", "ok": "boolean",
    }
    assert info.guess_x() == "time"
    df = info.scan().collect()
    assert df["time"][1] == dt.datetime(2024, 1, 1, 0, 0, 0, 100000)
    assert df["speed"].to_list() == [1.5, None]  # NA is a missing value


@pytest.mark.parametrize(
    "text, separator, decimal_comma",
    [
        ("a,b\n1.5,2\n", ",", False),
        ("a;b\n1,5;2\n", ";", True),
        ("a;b\n1.5;2\n", ";", False),
        ("a\tb\n1,5\t2\n", "\t", True),
        ("a|b|c\n1|2|3\n", "|", False),
        ('name,"x;y"\n"Doe; J",1\n"Roe; A",2\n', ",", False),  # separators inside quotes don't count
    ],
)
def test_sniff_csv(tmp_path, text, separator, decimal_comma):
    fmt = sniff_csv(write(tmp_path / "f.csv", text))
    assert (fmt.separator, fmt.decimal_comma) == (separator, decimal_comma)


def test_tsv_suffix_means_tabs(tmp_path):
    info = inspect_file(write(tmp_path / "f.tsv", "a\tb\n1\tx, y\n"))
    assert info.csv.separator == "\t" and info.names == ["a", "b"]


def test_late_values_widen_column_types(tmp_path):
    """Types are guessed from the first rows; values further down that do not fit widen the type."""
    n = CSV_INFER_ROWS + 10
    rows = [f"2024-01-01 00:{i // 60 % 60:02d}:{i % 60:02d};{i};{i},5;true" for i in range(n)]
    rows += ["2024-01-02 00:00:00;7,25;1;maybe", "yesterday;8;2;false"]
    path = write(tmp_path / "late.csv", "t;n;v;flag\n" + "\n".join(rows) + "\n")
    quick = inspect_file(path, verify=False)  # only the start of the file: what the file browser shows
    assert [str(c.dtype) for c in quick.columns][1:] == ["Int64", "Float64", "Boolean"]
    info = inspect_file(path)
    assert {c.name: c.dtype for c in info.columns} == {"t": pl.String, "n": pl.Float64, "v": pl.Float64, "flag": pl.String}
    df = info.scan().collect()
    assert df.height == n + 2
    assert df["n"][-2] == 7.25 and df["v"][-1] == 2.0


def test_unreadable_files(tmp_path):
    with pytest.raises(ValueError, match="not a readable CSV file"):
        inspect_file(write(tmp_path / "empty.csv", ""))
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")
    with pytest.raises(ValueError, match="neither a Parquet nor a CSV file"):
        inspect_file(tmp_path / "blob.bin")


def test_parquet_found_by_content(tmp_path):
    path = tmp_path / "data.bin"
    pl.DataFrame({"a": [1, 2]}).write_parquet(path)
    assert inspect_file(path).format == "parquet"


def test_csv_round_trip(telemetry, tmp_path):
    """A CSV written by an export reads back with the same types and values."""
    config = ProcessingConfig(x="time", y=["speed", "rpm", "on", "label"])
    out = tmp_path / "round.csv"
    export_file(telemetry, config, output=str(out))
    info = inspect_file(out)
    assert [c.kind for c in info.columns] == ["datetime", "numeric", "numeric", "boolean", "text"]
    assert export_query(info, config).collect().equals(export_query(inspect_file(telemetry), config).collect())


def test_csv_processing(tmp_path):
    lines = [f"{START + dt.timedelta(seconds=i)};{i},5;{'on' if i % 2 else 'off'}" for i in range(20)]
    path = write(tmp_path / "eu.csv", "zeit;wert;status\n" + "\n".join(lines) + "\n")
    config = ProcessingConfig(
        x="zeit", y=["wert", "status"], aggregation=AggregationConfig(method="interval", every="10s", functions=["max"])
    )
    df = export_query(inspect_file(path), config).collect()
    assert df["wert"].to_list() == [9.5, 19.5]
    assert df["status"].to_list() == ["on", "on"]  # text: the largest value
    data = load_plot_data(path, config)
    assert data.series[1].categories == ["on"]


def test_find_inputs_includes_csv(tmp_path, telemetry):
    csv = write(tmp_path / "b.csv", "a\n1\n")
    write(tmp_path / "notes.txt", "hello")
    assert find_inputs([tmp_path]) == [csv, telemetry]


def test_exclude_outputs(tmp_path):
    first = write(tmp_path / "flight.csv", "a\n1\n")
    earlier = write(tmp_path / "flight_export.csv", "a\n1\n")
    other = write(tmp_path / "other.csv", "a\n1\n")
    config = ProcessingConfig(x="a", y=["a"])
    assert exclude_outputs([first, earlier, other], config) == ([first, other], [earlier])
    config.output = OutputOptions(path=str(tmp_path / "out" / "{stem}.csv"))
    assert exclude_outputs([first, earlier, other], config) == ([first, earlier, other], [])
