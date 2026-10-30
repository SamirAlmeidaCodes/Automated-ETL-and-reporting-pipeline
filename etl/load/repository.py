"""Create the SQLite database and upsert daily weather rows."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from etl.errors import LoadError
from etl.load.models import Base, DailyWeather
from etl.schema import DAILY_COLUMNS, UPDATE_COLUMNS

logger = logging.getLogger(__name__)

_CHUNK_SIZE = 200
_REQUIRED = {"location_name", "observation_date", "observation_count", "source"}


@dataclass(frozen=True)
class UpsertResult:
    processed: int
    inserted: int
    updated: int


def create_db_engine(url: str) -> Engine:
    """Open SQLite and make sure the parent directory of a file database exists."""
    if url.startswith("sqlite:///"):
        raw_path = url[len("sqlite:///") :]
        if raw_path and raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, poolclass=NullPool)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite)
    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def upsert_daily(engine: Engine, frame: pd.DataFrame) -> UpsertResult:
    """Insert daily rows, or update them when the location and UTC date exist.

    `processed` counts rows sent to SQLite, including rows that replaced an
    existing key. A second run of the same dates updates those rows in place.
    """
    init_db(engine)
    if frame.empty:
        return UpsertResult(processed=0, inserted=0, updated=0)
    loaded_at = datetime.now(timezone.utc).replace(tzinfo=None)
    records = _records(frame, loaded_at)
    insert_stmt = sqlite_insert(DailyWeather)
    statement = insert_stmt.on_conflict_do_update(
        index_elements=["location_name", "observation_date"],
        set_={column: getattr(insert_stmt.excluded, column) for column in UPDATE_COLUMNS},
    )
    with Session(engine) as session:
        with session.begin():
            before = session.scalar(select(func.count()).select_from(DailyWeather)) or 0
            for start in range(0, len(records), _CHUNK_SIZE):
                session.execute(statement, records[start : start + _CHUNK_SIZE])
            after = session.scalar(select(func.count()).select_from(DailyWeather)) or 0
    inserted = int(after) - int(before)
    processed = len(records)
    updated = processed - inserted
    logger.debug("Upserted %s daily rows (%s inserted, %s updated)", processed, inserted, updated)
    return UpsertResult(processed=processed, inserted=inserted, updated=updated)


def fetch_daily(engine: Engine, *, since: date) -> pd.DataFrame:
    """Return stored daily rows on or after `since`, ordered for the charts."""
    init_db(engine)
    statement = (
        select(DailyWeather)
        .where(DailyWeather.observation_date >= since)
        .order_by(DailyWeather.location_name, DailyWeather.observation_date)
    )
    with Session(engine) as session:
        rows = session.scalars(statement).all()
    if not rows:
        return pd.DataFrame(columns=list(DAILY_COLUMNS))
    records = []
    for row in rows:
        record: dict[str, Any] = {}
        for column in DAILY_COLUMNS:
            value = getattr(row, column)
            if column == "observation_date" and isinstance(value, str):
                value = date.fromisoformat(value[:10])
            record[column] = value
        records.append(record)
    return pd.DataFrame.from_records(records)


def _records(frame: pd.DataFrame, loaded_at: datetime) -> list[dict[str, Any]]:
    missing = [column for column in DAILY_COLUMNS if column not in frame.columns]
    if missing:
        raise LoadError("Daily frame is missing columns: " + ", ".join(missing))
    records: list[dict[str, Any]] = []
    subset = frame.loc[:, list(DAILY_COLUMNS)]
    for row in subset.itertuples(index=False):
        record = {column: _python_value(column, getattr(row, column)) for column in DAILY_COLUMNS}
        record["loaded_at"] = loaded_at
        records.append(record)
    return records


def _python_value(column: str, value: Any) -> Any:
    if _is_null(value):
        if column in _REQUIRED:
            raise LoadError(f"Daily row is missing {column}")
        return None
    if column == "observation_date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])
    if column == "observation_count":
        return int(value)
    if column in {"location_name", "source"}:
        return str(value)
    return float(value)


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(missing) if isinstance(missing, bool) else False


def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()
