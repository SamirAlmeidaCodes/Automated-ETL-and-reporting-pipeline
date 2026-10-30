"""SQLite table for one weather row per location and UTC date."""

from datetime import date, datetime

from sqlalchemy import Date, DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DailyWeather(Base):
    """Daily aggregate. The unique key is the upsert identity."""

    __tablename__ = "daily_weather"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    location_name: Mapped[str] = mapped_column(String(256), nullable=False)
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    temp_min_c: Mapped[float | None] = mapped_column(Float)
    temp_max_c: Mapped[float | None] = mapped_column(Float)
    temp_mean_c: Mapped[float | None] = mapped_column(Float)
    precipitation_sum_mm: Mapped[float | None] = mapped_column(Float)
    humidity_mean_pct: Mapped[float | None] = mapped_column(Float)
    wind_speed_mean_kmh: Mapped[float | None] = mapped_column(Float)
    observation_count: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "location_name",
            "observation_date",
            name="uq_daily_weather_location_date",
        ),
        Index("ix_daily_weather_observation_date", "observation_date"),
    )
