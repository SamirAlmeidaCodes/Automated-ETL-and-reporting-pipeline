"""Liveness file used by the Docker healthcheck."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path


def write_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(stamp + "\n", encoding="utf-8")
