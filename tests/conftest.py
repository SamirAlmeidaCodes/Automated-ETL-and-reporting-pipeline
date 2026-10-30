"""Shared fixtures. The matplotlib backend is set before any chart import."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLBACKEND", "Agg")

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def write_app_config(directory: Path, **overrides: Any) -> Path:
    """Write a complete config.yaml under `directory` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    inbox = directory / "inbox"
    archive = directory / "archive"
    reports = directory / "reports"
    logs = directory / "logs"
    for path in (inbox, archive, reports, logs):
        path.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "app": {
            "name": "weather-etl-test",
            "heartbeat_file": str(directory / "heartbeat.txt"),
        },
        "schedule": {
            "timezone": "UTC",
            "cron": {
                "minute": "0",
                "hour": "6",
                "day": "*",
                "month": "*",
                "day_of_week": "*",
            },
            "run_on_start": False,
            "misfire_grace_seconds": 60,
        },
        "ingest": {
            "sources": ["csv"],
            "csv": {
                "inbox_dir": str(inbox),
                "pattern": "*.csv",
                "archive_dir": str(archive),
            },
            "api": {
                "base_url": "https://api.open-meteo.com/v1/forecast",
                "timeout_seconds": 5,
                "retries": 1,
                "backoff_seconds": 0,
                "past_days": 1,
                "forecast_days": 1,
                "hourly_variables": [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "precipitation",
                    "wind_speed_10m",
                ],
                "locations": [],
            },
        },
        "database": {"url": "sqlite:///" + (directory / "weather.db").as_posix()},
        "report": {"output_dir": str(reports), "lookback_days": 14},
        "email": {
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 587,
            "smtp_user": "",
            "smtp_password": "",
            "use_tls": True,
            "use_ssl": False,
            "from_addr": "",
            "to_addrs": [],
            "timeout_seconds": 5,
            "subject": "Daily weather report {date}",
        },
        "logging": {
            "level": "INFO",
            "file": str(logs / "etl.log"),
            "max_bytes": 1_048_576,
            "backup_count": 1,
        },
    }
    _merge(payload, overrides)
    path = directory / "config.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _merge(base: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        current = base.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            _merge(current, value)
        else:
            base[key] = value
