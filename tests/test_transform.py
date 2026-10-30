"""Offline tests for timestamp normalization, dedupe, and daily aggregation."""

from datetime import date

import pandas as pd
import pytest

from etl.errors import TransformError
from etl.schema import DAILY_COLUMNS
from etl.transform.weather import aggregate_daily, normalize_timestamps, prepare_hourly, transform


def test_later_row_wins_and_nulls_fall_back() -> None:
    # 20:00 in UTC-4 is 00:00 UTC the next calendar day.
    raw = pd.DataFrame(
        {
            "location": ["New York", "New York"],
            "timestamp": ["2026-10-27T20:00:00-04:00", "2026-10-28T00:00:00Z"],
            "temperature_c": [15.0, None],
            "precipitation_mm": [None, 1.2],
            "source": ["csv", "api"],
        }
    )
    daily = transform(raw)
    assert len(daily) == 1
    row = daily.iloc[0]
    assert row["location_name"] == "New York"
    assert row["observation_date"] == date(2026, 10, 28)
    assert row["temp_min_c"] == pytest.approx(15.0)
    assert row["temp_max_c"] == pytest.approx(15.0)
    assert row["temp_mean_c"] == pytest.approx(15.0)
    assert row["precipitation_sum_mm"] == pytest.approx(1.2)
    assert row["source"] == "api"
    assert row["observation_count"] == 1


def test_later_non_null_value_replaces_earlier_value() -> None:
    raw = pd.DataFrame(
        {
            "location": ["A", "A"],
            "timestamp": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00+00:00"],
            "temperature_c": [10.0, 18.0],
        }
    )
    hourly = prepare_hourly(raw)
    assert len(hourly) == 1
    assert hourly.iloc[0]["temperature_c"] == pytest.approx(18.0)
    assert hourly.iloc[0]["timestamp"] == pd.Timestamp("2026-01-01T00:00:00Z")


def test_daily_bucket_uses_utc_not_the_local_date() -> None:
    raw = pd.DataFrame(
        {
            "location": ["Berlin", "Berlin", "Berlin"],
            "timestamp": [
                "2026-03-28T23:30:00+01:00",
                "2026-03-29T00:30:00+01:00",
                "2026-03-29T01:30:00+01:00",
            ],
            "temperature_c": [8.0, 9.0, 12.0],
        }
    )
    daily = transform(raw)
    assert list(daily["observation_date"]) == [date(2026, 3, 28), date(2026, 3, 29)]
    first = daily.iloc[0]
    assert first["temp_min_c"] == pytest.approx(8.0)
    assert first["temp_max_c"] == pytest.approx(9.0)
    assert first["temp_mean_c"] == pytest.approx(8.5)
    assert first["observation_count"] == 2
    assert daily.iloc[1]["temp_mean_c"] == pytest.approx(12.0)
    assert daily.iloc[1]["observation_count"] == 1


def test_naive_timestamps_are_treated_as_utc() -> None:
    raw = pd.DataFrame(
        {
            "location": ["A"],
            "timestamp": ["2026-05-01 12:00:00"],
            "temperature_c": [7.5],
        }
    )
    hourly = prepare_hourly(raw)
    assert hourly.iloc[0]["timestamp"] == pd.Timestamp("2026-05-01T12:00:00Z")
    assert transform(raw).iloc[0]["observation_date"] == date(2026, 5, 1)


def test_normalize_timestamps_converts_an_offset_to_utc() -> None:
    frame = normalize_timestamps(pd.DataFrame({"timestamp": ["2026-01-01T01:00:00+02:00"]}))
    assert frame.iloc[0]["timestamp"] == pd.Timestamp("2025-12-31T23:00:00Z")


def test_column_aliases_and_header_case() -> None:
    raw = pd.DataFrame(
        {
            "City": ["Paris"],
            "Time": ["2026-02-02T00:00:00Z"],
            "Temperature_2m": [3.0],
            "Relative_Humidity_2m": [80],
            "Precipitation": [0.2],
            "Wind_Speed_10m": [11],
        }
    )
    row = transform(raw).iloc[0]
    assert row["location_name"] == "Paris"
    assert row["temp_mean_c"] == pytest.approx(3.0)
    assert row["humidity_mean_pct"] == pytest.approx(80.0)
    assert row["precipitation_sum_mm"] == pytest.approx(0.2)
    assert row["wind_speed_mean_kmh"] == pytest.approx(11.0)


def test_location_whitespace_is_stripped_before_dedupe() -> None:
    raw = pd.DataFrame(
        {
            "location": [" Oslo", "Oslo "],
            "timestamp": ["2026-04-01T00:00:00Z", "2026-04-01T00:00:00Z"],
            "temperature_c": [1.0, 4.0],
        }
    )
    hourly = prepare_hourly(raw)
    assert len(hourly) == 1
    assert hourly.iloc[0]["location"] == "Oslo"
    assert hourly.iloc[0]["temperature_c"] == pytest.approx(4.0)


def test_location_names_are_case_sensitive() -> None:
    raw = pd.DataFrame(
        {
            "location": ["Oslo", "oslo"],
            "timestamp": ["2026-04-01T00:00:00Z", "2026-04-01T01:00:00Z"],
            "temperature_c": [1.0, 2.0],
        }
    )
    assert set(transform(raw)["location_name"]) == {"Oslo", "oslo"}


def test_locations_are_aggregated_separately() -> None:
    raw = pd.DataFrame(
        {
            "location": ["A", "B"],
            "timestamp": ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
            "temperature_c": [1.0, 9.0],
        }
    )
    daily = transform(raw).set_index("location_name")
    assert daily.loc["A", "temp_mean_c"] == pytest.approx(1.0)
    assert daily.loc["B", "temp_mean_c"] == pytest.approx(9.0)


def test_unparseable_timestamps_are_dropped() -> None:
    raw = pd.DataFrame(
        {
            "location": ["A", "A", None],
            "timestamp": ["not-a-date", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"],
            "temperature_c": [1.0, 2.0, 3.0],
        }
    )
    hourly = prepare_hourly(raw)
    assert len(hourly) == 1
    assert hourly.iloc[0]["temperature_c"] == pytest.approx(2.0)


def test_empty_frame_has_the_daily_schema() -> None:
    result = transform(pd.DataFrame())
    assert list(result.columns) == list(DAILY_COLUMNS)
    assert result.empty


def test_all_null_precipitation_stays_null_and_rows_still_count() -> None:
    raw = pd.DataFrame(
        {
            "location": ["A", "A"],
            "timestamp": ["2026-06-01T00:00:00Z", "2026-06-01T01:00:00Z"],
            "temperature_c": [1.0, None],
            "precipitation_mm": [None, None],
        }
    )
    row = transform(raw).iloc[0]
    assert row["temp_mean_c"] == pytest.approx(1.0)
    assert row["temp_min_c"] == pytest.approx(1.0)
    assert row["temp_max_c"] == pytest.approx(1.0)
    assert pd.isna(row["precipitation_sum_mm"])
    assert pd.isna(row["humidity_mean_pct"])
    assert row["observation_count"] == 2
    assert row["source"] == "unknown"


def test_precipitation_sum_skips_nulls_and_sources_are_joined() -> None:
    raw = pd.DataFrame(
        {
            "location": ["A", "A", "A"],
            "timestamp": [
                "2026-06-01T00:00:00Z",
                "2026-06-01T01:00:00Z",
                "2026-06-01T02:00:00Z",
            ],
            "temperature_c": [10.0, 20.0, 30.0],
            "precipitation_mm": [None, 1.5, 2.5],
            "humidity_pct": [40, 60, None],
            "wind_speed_kmh": [10, 20, 30],
            "source": ["csv", "api", "csv"],
        }
    )
    row = transform(raw).iloc[0]
    assert row["temp_min_c"] == pytest.approx(10)
    assert row["temp_max_c"] == pytest.approx(30)
    assert row["temp_mean_c"] == pytest.approx(20)
    assert row["precipitation_sum_mm"] == pytest.approx(4.0)
    assert row["humidity_mean_pct"] == pytest.approx(50)
    assert row["wind_speed_mean_kmh"] == pytest.approx(20)
    assert row["observation_count"] == 3
    assert row["source"] == "api,csv"


def test_non_numeric_metric_becomes_null() -> None:
    raw = pd.DataFrame(
        {
            "location": ["A"],
            "timestamp": ["2026-01-01T00:00:00Z"],
            "temperature_c": ["hot"],
        }
    )
    row = transform(raw).iloc[0]
    assert pd.isna(row["temp_mean_c"])
    assert row["observation_count"] == 1


def test_transform_does_not_mutate_the_input() -> None:
    raw = pd.DataFrame(
        {
            "location": ["A"],
            "timestamp": ["2026-01-01T00:00:00Z"],
            "temperature_c": [5.0],
        }
    )
    original = raw.copy(deep=True)
    transform(raw)
    pd.testing.assert_frame_equal(raw, original)


def test_missing_location_column_raises() -> None:
    raw = pd.DataFrame({"timestamp": ["2026-01-01T00:00:00Z"], "temperature_c": [1.0]})
    with pytest.raises(TransformError, match="location"):
        transform(raw)


def test_aggregate_daily_requires_prepared_columns() -> None:
    raw = pd.DataFrame({"location": ["A"], "timestamp": ["2026-01-01T00:00:00Z"]})
    with pytest.raises(TransformError, match="prepared hourly"):
        aggregate_daily(raw)


def test_transform_rejects_a_non_dataframe() -> None:
    with pytest.raises(TransformError, match="DataFrame"):
        transform(["not", "a", "frame"])  # type: ignore[arg-type]
