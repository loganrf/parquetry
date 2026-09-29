from __future__ import annotations

import datetime as dt

import numpy as np
import polars as pl
import pytest

START = dt.datetime(2024, 1, 1, 0, 0, 0)


@pytest.fixture
def telemetry(tmp_path):
    """600 rows at 10 Hz (one minute) with a datetime x axis."""
    n = 600
    t = [START + dt.timedelta(milliseconds=100 * i) for i in range(n)]
    df = pl.DataFrame(
        {
            "time": t,
            "speed": np.arange(n, dtype=np.float64),
            "temp": np.where(np.arange(n) % 100 == 50, None, 20.0 + np.arange(n) % 10).tolist(),
            "rpm": np.arange(n, dtype=np.int32) * 2,
            "on": (np.arange(n) % 2 == 0),
            "label": ["a"] * n,
        }
    )
    path = tmp_path / "telemetry.parquet"
    df.write_parquet(path, row_group_size=100)
    return path


@pytest.fixture
def unsorted(tmp_path):
    """Rows in reverse time order."""
    n = 50
    t = [START + dt.timedelta(seconds=i) for i in range(n)][::-1]
    df = pl.DataFrame({"time": t, "value": np.arange(n, dtype=np.float64)[::-1]})
    path = tmp_path / "unsorted.parquet"
    df.write_parquet(path)
    return path


@pytest.fixture
def numeric_x(tmp_path):
    df = pl.DataFrame({"distance": np.linspace(0.0, 99.0, 100), "force": np.arange(100.0) ** 2})
    path = tmp_path / "numeric.parquet"
    df.write_parquet(path)
    return path


@pytest.fixture
def tz_aware(tmp_path):
    t = pl.datetime_range(START, START + dt.timedelta(hours=5), "1h", eager=True, time_zone="Europe/Berlin")
    df = pl.DataFrame({"ts": t, "v": np.arange(len(t), dtype=np.float64)})
    path = tmp_path / "tz.parquet"
    df.write_parquet(path)
    return path
