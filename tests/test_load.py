"""Upserts must replace an existing location and UTC date instead of inserting another row."""

from datetime import date, timedelta

import pandas as pd
import pytest

from etl.load.repository import create_db_engine, fetch_daily, upsert_daily
from etl.schema import DAILY_COLUMNS


def _row(location: str, observed: date, temp: float) -> dict[str, object]:
    return {
        "location_name": location,
        "observation_date": observed,
        "temp_min_c": temp - 1,
        "temp_max_c": temp + 1,
        "temp_mean_c": temp,
        "precipitation_sum_mm": 0.0,
        "humidity_mean_pct": 50.0,
        "wind_speed_mean_kmh": 10.0,
        "observation_count": 24,
        "source": "api",
    }


def test_upsert_inserts_then_updates_in_place(tmp_path) -> None:
    engine = create_db_engine("sqlite:///" + (tmp_path / "weather.db").as_posix())
    frame = pd.DataFrame([_row("Oslo", date(2026, 10, 1), 3.0)])
    first = upsert_daily(engine, frame)
    assert (first.processed, first.inserted, first.updated) == (1, 1, 0)

    frame.loc[0, "temp_mean_c"] = 9.0
    second = upsert_daily(engine, frame)
    assert (second.processed, second.inserted, second.updated) == (1, 0, 1)

    stored = fetch_daily(engine, since=date(2026, 10, 1))
    assert len(stored) == 1
    assert stored.iloc[0]["temp_mean_c"] == pytest.approx(9.0)
    assert stored.iloc[0]["location_name"] == "Oslo"


def test_same_date_for_two_locations_stays_two_rows(tmp_path) -> None:
    engine = create_db_engine("sqlite:///" + (tmp_path / "weather.db").as_posix())
    frame = pd.DataFrame(
        [
            _row("Oslo", date(2026, 10, 1), 3.0),
            _row("Paris", date(2026, 10, 1), 8.0),
        ]
    )
    result = upsert_daily(engine, frame)
    assert result.inserted == 2
    stored = fetch_daily(engine, since=date(2026, 10, 1))
    assert set(stored["location_name"]) == {"Oslo", "Paris"}


def test_fetch_respects_the_lookback_start(tmp_path) -> None:
    engine = create_db_engine("sqlite:///" + (tmp_path / "weather.db").as_posix())
    frame = pd.DataFrame(
        [
            _row("Oslo", date(2026, 10, 1), 3.0),
            _row("Oslo", date(2026, 10, 5), 6.0),
        ]
    )
    upsert_daily(engine, frame)
    stored = fetch_daily(engine, since=date(2026, 10, 5))
    assert list(stored["observation_date"]) == [date(2026, 10, 5)]


def test_empty_frame_upserts_nothing(tmp_path) -> None:
    engine = create_db_engine("sqlite:///" + (tmp_path / "weather.db").as_posix())
    result = upsert_daily(engine, pd.DataFrame(columns=list(DAILY_COLUMNS)))
    assert (result.processed, result.inserted, result.updated) == (0, 0, 0)
    assert fetch_daily(engine, since=date(2020, 1, 1)).empty


def test_upsert_chunks_more_than_one_batch(tmp_path) -> None:
    engine = create_db_engine("sqlite:///" + (tmp_path / "weather.db").as_posix())
    start = date(2020, 1, 1)
    frame = pd.DataFrame([_row("Oslo", start + timedelta(days=offset), float(offset)) for offset in range(201)])
    first = upsert_daily(engine, frame)
    assert (first.inserted, first.updated) == (201, 0)
    frame["temp_mean_c"] = 42.0
    second = upsert_daily(engine, frame)
    assert (second.inserted, second.updated) == (0, 201)
    stored = fetch_daily(engine, since=start)
    assert len(stored) == 201
    assert stored["temp_mean_c"].eq(42.0).all()
