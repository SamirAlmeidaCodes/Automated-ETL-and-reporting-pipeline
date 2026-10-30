"""Normalize hourly weather rows and roll them up to one row per UTC day."""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

from etl.errors import TransformError
from etl.schema import DAILY_COLUMNS, HOURLY_COLUMNS, METRIC_COLUMNS

logger = logging.getLogger(__name__)

# First alias in each list wins when several names are present.
_ALIAS_PRIORITY: dict[str, tuple[str, ...]] = {
    "location": ("location", "location_name", "city", "site", "station"),
    "timestamp": ("timestamp", "time", "datetime", "observed_at", "observation_time"),
    "temperature_c": ("temperature_c", "temperature_2m", "temperature", "temp_c", "temp"),
    "humidity_pct": ("humidity_pct", "relative_humidity_2m", "relative_humidity", "humidity"),
    "precipitation_mm": ("precipitation_mm", "precipitation", "precip_mm", "precip", "rain_mm"),
    "wind_speed_kmh": ("wind_speed_kmh", "wind_speed_10m", "wind_speed", "wind_kmh", "wind"),
    "source": ("source",),
}

_METRIC_DECIMALS = 4


def transform(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize timestamps to UTC, collapse duplicate hours, and aggregate by day."""
    return aggregate_daily(prepare_hourly(df))


def prepare_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """Return canonical hourly rows: one row per location and UTC timestamp.

    Naive timestamps are treated as UTC. Rows that share a location and an
    instant collapse to one row: the later row wins, and a null measurement
    falls back to the earlier row. The input frame is not modified.
    """
    if not isinstance(df, pd.DataFrame):
        raise TransformError("Expected a pandas DataFrame")
    if df.empty:
        return _empty_hourly()

    frame = _canonicalize_columns(df)
    missing = [column for column in ("location", "timestamp") if column not in frame.columns]
    if missing:
        raise TransformError("Input is missing required columns: " + ", ".join(missing))
    for column in METRIC_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
    if "source" not in frame.columns:
        frame["source"] = pd.NA

    frame = frame.loc[:, list(HOURLY_COLUMNS)].copy()
    frame["location"] = frame["location"].map(_clean_location)
    frame["timestamp"] = _parse_timestamps(frame["timestamp"])
    for column in METRIC_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    before = len(frame)
    frame = frame.dropna(subset=["location", "timestamp"])
    dropped = before - len(frame)
    if dropped:
        logger.warning(
            "Dropped %s rows with a missing location or an unparseable timestamp",
            dropped,
        )
    if frame.empty:
        return _empty_hourly()

    frame = deduplicate(frame, keys=["location", "timestamp"])
    frame["source"] = frame["source"].map(_clean_source)
    frame = frame.sort_values(["location", "timestamp"], kind="mergesort").reset_index(drop=True)
    return frame.loc[:, list(HOURLY_COLUMNS)]


def normalize_timestamps(df: pd.DataFrame, column: str = "timestamp") -> pd.DataFrame:
    """Parse `column` and convert it to UTC. Unparseable values become NaT."""
    if column not in df.columns:
        raise TransformError(f"Missing timestamp column {column!r}")
    frame = df.copy()
    frame[column] = _parse_timestamps(frame[column])
    return frame


def deduplicate(df: pd.DataFrame, keys: list[str] | None = None) -> pd.DataFrame:
    """Collapse rows that share `keys`.

    Row order is the priority order. For each column that is not a key, the
    last non-null value is kept. Keys themselves are taken from the last row.
    """
    key_cols = list(keys or ["location", "timestamp"])
    missing = [key for key in key_cols if key not in df.columns]
    if missing:
        raise TransformError("Cannot deduplicate; missing columns: " + ", ".join(missing))
    if df.empty:
        return df.copy()

    frame = df.reset_index(drop=True).copy()
    frame["_seq"] = range(len(frame))
    fill_cols = [column for column in frame.columns if column not in set(key_cols) | {"_seq"}]
    frame = frame.sort_values([*key_cols, "_seq"], kind="mergesort")
    if fill_cols:
        filled = frame.groupby(key_cols, dropna=False, sort=False)[fill_cols].ffill()
        frame.loc[:, fill_cols] = filled
    frame = frame.drop_duplicates(subset=key_cols, keep="last")
    return frame.drop(columns=["_seq"]).reset_index(drop=True)


def aggregate_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Roll prepared hourly rows into one row per location and UTC date.

    Precipitation is a sum. Temperature, humidity, and wind are averages.
    A day whose measurements are all null stays null rather than becoming zero.
    `observation_count` is the number of hourly rows, including null metrics.
    """
    if not isinstance(df, pd.DataFrame):
        raise TransformError("Expected a pandas DataFrame")
    if df.empty:
        return _empty_daily()
    missing = [column for column in HOURLY_COLUMNS if column not in df.columns]
    if missing:
        raise TransformError(
            "aggregate_daily expects prepared hourly columns; missing " + ", ".join(missing)
        )

    frame = df.loc[:, list(HOURLY_COLUMNS)].copy()
    if not pd.api.types.is_datetime64_any_dtype(frame["timestamp"]):
        raise TransformError("timestamp must be a datetime. Call prepare_hourly or transform first.")
    frame["timestamp"] = _ensure_utc(frame["timestamp"])
    frame = frame.dropna(subset=["location", "timestamp"])
    if frame.empty:
        return _empty_daily()

    frame["observation_date"] = frame["timestamp"].dt.date
    frame["_one"] = 1
    grouped = frame.groupby(["location", "observation_date"], sort=True, dropna=False)
    aggregated = pd.DataFrame(
        {
            "temp_min_c": grouped["temperature_c"].min(),
            "temp_max_c": grouped["temperature_c"].max(),
            "temp_mean_c": grouped["temperature_c"].mean(),
            "precipitation_sum_mm": grouped["precipitation_mm"].sum(min_count=1),
            "humidity_mean_pct": grouped["humidity_pct"].mean(),
            "wind_speed_mean_kmh": grouped["wind_speed_kmh"].mean(),
            "observation_count": grouped["_one"].sum(),
            "source": grouped["source"].agg(_join_sources),
        }
    )
    result = aggregated.reset_index().rename(columns={"location": "location_name"})
    for column in (
        "temp_min_c",
        "temp_max_c",
        "temp_mean_c",
        "precipitation_sum_mm",
        "humidity_mean_pct",
        "wind_speed_mean_kmh",
    ):
        result[column] = pd.to_numeric(result[column], errors="coerce").round(_METRIC_DECIMALS)
    result["observation_count"] = result["observation_count"].astype(int)
    result["source"] = result["source"].map(_clean_source)
    return result.loc[:, list(DAILY_COLUMNS)].reset_index(drop=True)


def _canonicalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    if frame.columns.duplicated().any():
        frame = frame.loc[:, ~frame.columns.duplicated()].copy()
    for canonical, aliases in _ALIAS_PRIORITY.items():
        if canonical in frame.columns:
            extras = [alias for alias in aliases if alias != canonical and alias in frame.columns]
            if extras:
                frame = frame.drop(columns=extras)
            continue
        for alias in aliases:
            if alias in frame.columns:
                frame = frame.rename(columns={alias: canonical})
                extras = [other for other in aliases if other != canonical and other in frame.columns]
                if extras:
                    frame = frame.drop(columns=extras)
                break
    return frame


def _parse_timestamps(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        parsed = pd.to_datetime(series, utc=True)
    else:
        parsed = pd.to_datetime(series, utc=True, errors="coerce", format="mixed")
    return _ensure_utc(parsed)


def _ensure_utc(series: pd.Series) -> pd.Series:
    if not pd.api.types.is_datetime64_any_dtype(series):
        return series
    timezone = series.dt.tz
    if timezone is None:
        return series.dt.tz_localize("UTC")
    return series.dt.tz_convert("UTC")


def _clean_location(value: Any) -> str | None:
    if _is_missing(value):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def _clean_source(value: Any) -> str:
    if _is_missing(value):
        return "unknown"
    text = str(value).strip()
    return text or "unknown"


def _join_sources(values: pd.Series) -> str:
    names = sorted({_clean_source(value) for value in values if not _is_missing(value) and str(value).strip()})
    return ",".join(names) if names else "unknown"


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(missing) if isinstance(missing, bool) else False


def _empty_hourly() -> pd.DataFrame:
    return pd.DataFrame(columns=list(HOURLY_COLUMNS))


def _empty_daily() -> pd.DataFrame:
    return pd.DataFrame(columns=list(DAILY_COLUMNS))
