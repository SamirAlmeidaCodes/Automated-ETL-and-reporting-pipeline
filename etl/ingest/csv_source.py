"""Read dropped CSV files and archive them after a successful load."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from etl.config import CsvConfig
from etl.errors import IngestError

logger = logging.getLogger(__name__)


@dataclass
class CsvReadResult:
    frame: pd.DataFrame
    files: list[Path] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def read_csv_inbox(config: CsvConfig) -> CsvReadResult:
    """Read every matching file in the inbox.

    A file that cannot be parsed stays in the inbox. An empty inbox is a
    successful read with zero rows. If every file fails, this raises.
    """
    inbox = config.inbox_dir
    if inbox.exists() and not inbox.is_dir():
        raise IngestError(f"CSV inbox is not a directory: {inbox}")
    inbox.mkdir(parents=True, exist_ok=True)
    paths = sorted(path for path in inbox.glob(config.pattern) if path.is_file())
    if not paths:
        logger.info("No CSV files matched %s in %s", config.pattern, inbox)
        return CsvReadResult(frame=pd.DataFrame())

    frames: list[pd.DataFrame] = []
    files: list[Path] = []
    failures: list[str] = []
    for path in paths:
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig")
        except Exception as exc:
            message = f"csv {path.name}: {exc}"
            logger.error("Skipping CSV file: %s", message)
            failures.append(message)
            continue
        if frame.empty:
            message = f"csv {path.name}: file has no data rows"
            logger.error("Skipping CSV file: %s", message)
            failures.append(message)
            continue
        frames.append(_with_source(frame))
        files.append(path)
        logger.info("Read %s rows from %s", len(frame), path.name)

    if not frames:
        raise IngestError("All CSV files failed: " + "; ".join(failures))
    combined = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    return CsvReadResult(frame=combined, files=files, failures=failures)


def archive_files(paths: list[Path], archive_dir: Path) -> None:
    """Move processed files out of the inbox. Re-processing is still safe."""
    if not paths:
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for index, path in enumerate(paths, start=1):
        if not path.is_file():
            logger.warning("CSV file is already gone; not archiving %s", path)
            continue
        target = archive_dir / f"{stamp}_{index}_{path.name}"
        try:
            path.replace(target)
        except OSError:
            shutil.move(str(path), str(target))
        logger.info("Archived %s to %s", path.name, target)


def _with_source(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    source_column = next((column for column in result.columns if str(column).strip().lower() == "source"), None)
    if source_column is None:
        result["source"] = "csv"
        return result
    result[source_column] = result[source_column].map(lambda value: "csv" if _blank(value) else value)
    return result


def _blank(value: object) -> bool:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        return False
    return str(value).strip() == ""
