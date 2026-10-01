"""Synthetic telemetry data for trying out parquetry."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import numpy as np
import polars as pl

from .dataset import CSV_SUFFIXES


def generate_sample(
    path: str | os.PathLike,
    rows: int = 1_000_000,
    rate_hz: float = 100.0,
    start: dt.datetime = dt.datetime(2024, 6, 1, 8, 0, 0),
    seed: int = 7,
) -> Path:
    """Write a flight-test style telemetry file with ``rows`` samples at ``rate_hz``.

    The file is Parquet unless *path* ends in ``.csv`` or ``.tsv``.
    """
    rng = np.random.default_rng(seed)
    step_us = round(1e6 / rate_hz)
    t = np.arange(rows, dtype=np.int64) * step_us
    duration = max(t[-1] / 1e6, 1.0) if rows else 1.0
    phase = (t / 1e6) / duration  # 0..1 over the flight

    # altitude profile: taxi, climb, cruise, descent, taxi
    altitude = np.interp(phase, [0, 0.08, 0.3, 0.75, 0.93, 1.0], [0, 0, 3500, 3600, 0, 0])
    altitude = np.maximum(altitude + rng.normal(0, 1.5, rows), 0)
    airspeed = np.interp(phase, [0, 0.07, 0.1, 0.3, 0.75, 0.9, 0.93, 1.0], [5, 8, 60, 75, 82, 70, 20, 5])
    airspeed += rng.normal(0, 0.8, rows)
    pressure = 1013.25 * (1 - 2.25577e-5 * altitude) ** 5.25588 + rng.normal(0, 0.15, rows)
    temperature = 18.0 - 0.0065 * altitude + 0.8 * np.sin(2 * np.pi * phase * 7) + rng.normal(0, 0.1, rows)
    rpm = np.interp(phase, [0, 0.07, 0.1, 0.3, 0.75, 0.9, 1.0], [900, 1200, 2500, 2400, 2300, 1500, 900])
    rpm += 25 * np.sin(2 * np.pi * (t / 1e6) / 3.0) + rng.normal(0, 8, rows)
    vibration = np.abs(rng.normal(0, 0.05, rows)) * (rpm / 1500)
    spikes = rng.random(rows) < 2e-5
    vibration[spikes] += rng.uniform(0.5, 2.5, spikes.sum())
    battery = 12.6 - 0.6 * phase + rng.normal(0, 0.02, rows)
    battery[rng.random(rows) < 1e-4] = np.nan  # sensor dropouts
    gear_down = (phase < 0.1) | (phase > 0.88)
    labels = np.array(["taxi", "climb", "cruise", "descent", "taxi"])
    flight_phase = labels[np.searchsorted([0.08, 0.3, 0.75, 0.93], phase, side="right")]

    df = pl.DataFrame(
        {
            "timestamp": pl.Series(t, dtype=pl.Int64).cast(pl.Duration("us")) + start,
            "altitude_m": altitude,
            "airspeed_mps": airspeed,
            "pressure_hpa": pressure,
            "temperature_c": temperature,
            "engine_rpm": rpm.round(0).astype(np.int32),
            "vibration_g": vibration,
            "battery_v": battery,
            "gear_down": gear_down,
            "flight_phase": flight_phase,
            "sample_id": np.arange(rows, dtype=np.int64),
        }
    ).with_columns(pl.col("battery_v").fill_nan(None))
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() in CSV_SUFFIXES:
        df.write_csv(path, separator="\t" if path.suffix.lower() == ".tsv" else ",")
    else:
        df.write_parquet(path, compression="zstd", row_group_size=250_000, statistics=True)
    return path
