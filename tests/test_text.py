"""Text columns: as parameters, as a category x axis, and in the data table."""

import datetime as dt

import numpy as np
import polars as pl
import pytest

from parquetry.config import AggregationConfig, ProcessingConfig
from parquetry.dataset import inspect_file
from parquetry.processing import (
    estimate_rows,
    export_query,
    load_plot_data,
    load_table_data,
    merge_plot_data,
    preview_csv,
)

from .conftest import START


@pytest.fixture
def modes(tmp_path):
    """100 s at 1 Hz with a text state column (and a gap of nulls)."""
    n = 100
    states = ["idle"] * 30 + ["run"] * 40 + [None] * 10 + ["stop"] * 20
    df = pl.DataFrame(
        {"t": [START + dt.timedelta(seconds=i) for i in range(n)], "state": states, "v": np.arange(n, dtype=float)}
    )
    path = tmp_path / "modes.parquet"
    df.write_parquet(path)
    return path


@pytest.fixture
def sales(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text("region,revenue,grade\nNorth,10,b\nSouth,20,a\nNorth,30,a\nEast,5,c\n")
    return path


def test_text_parameters_are_exported(modes):
    config = ProcessingConfig(x="t", y=["state", "v"])
    df = export_query(inspect_file(modes), config).collect()
    assert df["state"].to_list()[28:32] == ["idle", "idle", "run", "run"]
    assert preview_csv(modes, config, rows=1).splitlines()[1] == "2024-01-01T00:00:00.000000,idle,0.0"


def test_text_aggregation_functions(modes):
    info = inspect_file(modes)
    agg = AggregationConfig(method="interval", every="20s", functions=["mean", "last", "count"])
    df = export_query(info, ProcessingConfig(x="t", y=["state", "v"], aggregation=agg)).collect()
    # mean does not apply to text: only last and count are written for it
    assert df.columns == ["t", "state_last", "state_count", "v_mean", "v_last", "v_count"]
    assert df["state_last"].to_list() == ["idle", "run", "run", None, "stop"]
    assert df["state_count"].to_list() == [20, 20, 20, 10, 20]
    agg.functions = ["mean"]  # no function applies: the first value of each bucket
    df = export_query(info, ProcessingConfig(x="t", y=["state"], aggregation=agg)).collect()
    assert df["state"].to_list() == ["idle", "idle", "run", "run", "stop"]


def test_text_plot_positions(modes):
    data = load_plot_data(modes, ProcessingConfig(x="t", y=["state", "v"]))
    state = data.series[0]
    assert state.categories == ["idle", "run", "stop"]  # sorted
    assert state.y[0] == 0 and state.y[30] == 1 and np.isnan(state.y[75]) and state.y[-1] == 2
    assert data.series[1].categories is None


def test_text_envelope_and_detail_keep_positions(modes):
    config = ProcessingConfig(x="t", y=["state"])
    overview = load_plot_data(modes, config, max_points=10, envelope_buckets=5)
    assert overview.reduced and overview.series[0].categories == ["idle", "run", "stop"]
    lo = overview.bounds[0]
    detail = load_plot_data(modes, config, view=(lo + 40, lo + 50))
    assert detail.series[0].categories == ["run"] and set(detail.series[0].y) == {0.0}
    merged = merge_plot_data(overview, detail)
    window = (merged.series[0].x >= lo + 40) & (merged.series[0].x <= lo + 50)
    assert set(merged.series[0].y[window]) == {1.0}  # "run" in the overview's categories


def test_category_x_keeps_file_order(sales):
    info = inspect_file(sales)
    config = ProcessingConfig(x="region", y=["revenue", "grade"])
    df = export_query(info, config).collect()
    assert df["region"].to_list() == ["North", "South", "North", "East"]  # not sorted
    config.aggregation = AggregationConfig(method="per_value", functions=["mean", "min"])
    df = export_query(info, config).collect()
    assert df.columns == ["region", "revenue_mean", "revenue_min", "grade_min"]
    assert df.rows() == [("North", 20.0, 10, "a"), ("South", 20.0, 20, "a"), ("East", 5.0, 5, "c")]
    assert estimate_rows(info, config) == 3


def test_category_plot(sales):
    data = load_plot_data(sales, ProcessingConfig(x="region", y=["revenue", "grade"]))
    assert data.x_kind == "category" and data.x_categories == ["North", "South", "East"]
    assert data.bounds == (0.0, 2.0)
    revenue, grade = data.series
    assert revenue.x.tolist() == [0, 1, 0, 2] and revenue.y.tolist() == [10, 20, 30, 5]
    assert grade.categories == ["a", "b", "c"] and grade.y.tolist() == [1, 0, 0, 2]


def test_category_plot_reduced(sales):
    data = load_plot_data(sales, ProcessingConfig(x="region", y=["revenue"]), max_points=2)
    assert data.reduced
    revenue = data.series[0]
    assert revenue.x.tolist() == [0, 0, 1, 1, 2, 2]
    assert revenue.y.tolist() == [10, 30, 20, 20, 5, 5]  # lowest and highest per category


def test_per_value_for_numeric_x(numeric_x):
    config = ProcessingConfig(
        x="distance", y=["force"], aggregation=AggregationConfig(method="per_value", functions=["count"])
    )
    assert export_query(inspect_file(numeric_x), config).collect().height == 100


def test_table_data(modes):
    config = ProcessingConfig(x="t", y=["state", "v"])
    table = load_table_data(modes, config, limit=20)
    assert table.truncated and table.rows == 100 and not table.windowed
    assert table.frame.columns == ["t", "state", "v"] and table.frame["state"][0] == "idle"
    lo = load_plot_data(modes, config).bounds[0]
    table = load_table_data(modes, config, view=(lo + 29, lo + 31))
    assert table.windowed and not table.truncated
    assert table.frame["state"].to_list() == ["idle", "run", "run"]


def test_table_data_category_x_ignores_view(sales):
    table = load_table_data(sales, ProcessingConfig(x="region", y=["grade"]), view=(0.5, 1.5))
    assert table.rows == 4 and not table.windowed
