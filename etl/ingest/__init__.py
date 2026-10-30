"""Ingest CSV files and the Open-Meteo API into one raw table."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from etl.config import AppConfig
from etl.errors import IngestError
from etl.ingest.api_source import fetch_weather
from etl.ingest.csv_source import read_csv_inbox

logger = logging.getLogger(__name__)


@dataclass
class RawExtract:
    frame: pd.DataFrame
    csv_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def extract(config: AppConfig) -> RawExtract:
    """Read each configured source. Later sources override earlier rows during transform.

    A source that fails outright is skipped when another source succeeds. The
    failure is returned in `warnings` so the run can still load the data it has
    and report a non-zero status. If every source fails, this raises.
    """
    frames: list[pd.DataFrame] = []
    csv_files: list[Path] = []
    warnings: list[str] = []
    successes = 0
    for source in config.ingest.sources:
        try:
            if source == "csv":
                result = read_csv_inbox(config.ingest.csv)
                csv_files.extend(result.files)
                warnings.extend(result.failures)
                if not result.frame.empty:
                    frames.append(result.frame)
            elif source == "api":
                result = fetch_weather(config.ingest.api, config.app.name)
                warnings.extend(result.failures)
                if not result.frame.empty:
                    frames.append(result.frame)
            else:
                raise IngestError(f"Unknown ingest source {source}")
        except IngestError as exc:
            warnings.append(f"{source}: {exc}")
            logger.error("Ingest source %s failed: %s", source, exc)
            continue
        successes += 1
    if successes == 0:
        raise IngestError("All ingest sources failed: " + "; ".join(warnings))
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    logger.info("Ingested %s raw rows from %s source(s)", len(combined), successes)
    return RawExtract(frame=combined, csv_files=csv_files, warnings=warnings)
