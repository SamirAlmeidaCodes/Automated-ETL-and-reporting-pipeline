"""Render the three daily report charts."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from etl.validation import resolve_child

_PALETTE = ("#1b4f72", "#b9770e", "#117a65", "#6c3483", "#922b21", "#1a5276")
_EMPTY_MESSAGE = "No observations in the lookback window"


def render_report(df: pd.DataFrame, output_dir: Path, report_date: date) -> list[Path]:
    """Write temperature, precipitation, and wind charts. Files are overwritten on the same day."""
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = report_date.isoformat()
    frame = _prepare(df)
    return [
        _line_chart(
            frame,
            "temp_mean_c",
            "Daily mean temperature",
            "Temperature (°C)",
            resolve_child(output_dir, f"temperature_{stamp}.png"),
        ),
        _bar_chart(
            frame,
            "precipitation_sum_mm",
            "Daily precipitation",
            "Precipitation (mm)",
            resolve_child(output_dir, f"precipitation_{stamp}.png"),
        ),
        _line_chart(
            frame,
            "wind_speed_mean_kmh",
            "Daily mean wind speed",
            "Wind speed (km/h)",
            resolve_child(output_dir, f"wind_{stamp}.png"),
        ),
    ]


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    frame = df.copy()
    frame["observation_date"] = pd.to_datetime(frame["observation_date"])
    frame = frame.drop_duplicates(["location_name", "observation_date"], keep="last")
    return frame.sort_values(["location_name", "observation_date"], kind="mergesort")


def _line_chart(frame: pd.DataFrame, column: str, title: str, ylabel: str, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(10, 5.4))
    locations = _locations(frame)
    colors = _colors(locations)
    plotted = False
    for location in locations:
        group = frame.loc[frame["location_name"] == location].dropna(subset=[column])
        if group.empty:
            continue
        ax.plot(
            group["observation_date"],
            group[column],
            marker="o",
            linewidth=2,
            markersize=4.5,
            label=location,
            color=colors[location],
        )
        plotted = True
    if not plotted:
        return _empty_chart(fig, ax, path, title)
    _decorate(ax, title, ylabel)
    ax.legend(frameon=False, loc="best")
    fig.autofmt_xdate(rotation=30, ha="right")
    return _save(fig, path)


def _bar_chart(frame: pd.DataFrame, column: str, title: str, ylabel: str, path: Path) -> Path:
    fig, ax = plt.subplots(figsize=(10, 5.4))
    locations = _locations(frame)
    if frame.empty or not locations:
        return _empty_chart(fig, ax, path, title)
    colors = _colors(locations)
    days = list(pd.to_datetime(frame["observation_date"]).drop_duplicates().sort_values())
    count = len(locations)
    width = 0.8 / count
    bases = list(range(len(days)))
    plotted = False
    for index, location in enumerate(locations):
        group = frame.loc[frame["location_name"] == location].set_index("observation_date")
        heights: list[float] = []
        for day in days:
            if day not in group.index or pd.isna(group.at[day, column]):
                heights.append(0.0)
            else:
                heights.append(float(group.at[day, column]))
                plotted = True
        offset = index - (count - 1) / 2
        positions = [base + offset * width for base in bases]
        ax.bar(positions, heights, width=width * 0.92, label=location, color=colors[location])
    if not plotted:
        return _empty_chart(fig, ax, path, title)
    step = max(1, len(days) // 12)
    shown = bases[::step]
    ax.set_xticks(shown)
    ax.set_xticklabels(
        [pd.Timestamp(days[index]).strftime("%Y-%m-%d") for index in shown],
        rotation=30,
        ha="right",
    )
    _decorate(ax, title, ylabel)
    ax.legend(frameon=False, loc="best")
    return _save(fig, path)


def _decorate(ax: plt.Axes, title: str, ylabel: str) -> None:
    ax.set_title(title, loc="left", fontsize=14, pad=10)
    ax.set_ylabel(ylabel)
    ax.set_xlabel("UTC date    ·    Weather data by Open-Meteo.com")
    ax.grid(True, axis="y", alpha=0.3)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _empty_chart(fig: plt.Figure, ax: plt.Axes, path: Path, title: str) -> Path:
    ax.clear()
    ax.set_axis_off()
    ax.set_title(title, loc="left", fontsize=14)
    ax.text(0.5, 0.5, _EMPTY_MESSAGE, ha="center", va="center", fontsize=12, transform=ax.transAxes)
    return _save(fig, path)


def _locations(frame: pd.DataFrame) -> list[str]:
    if frame.empty or "location_name" not in frame.columns:
        return []
    return sorted(str(name) for name in frame["location_name"].dropna().unique())


def _colors(locations: list[str]) -> dict[str, str]:
    return {name: _PALETTE[index % len(_PALETTE)] for index, name in enumerate(locations)}


def _save(fig: plt.Figure, path: Path) -> Path:
    if path.is_symlink():
        raise OSError("refusing to write a chart through a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return path
