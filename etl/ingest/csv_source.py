"""Read dropped CSV files and archive them after a successful load."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from pydantic import ValidationError

from etl.config import CsvConfig
from etl.errors import IngestError
from etl.validation import MAX_CSV_BYTES, CsvColumns, CsvShape, is_inside, safe_filename

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
        raise IngestError("CSV inbox is not a directory")
    inbox.mkdir(parents=True, exist_ok=True)
    paths = sorted(path for path in inbox.glob(config.pattern) if path.is_file())
    if not paths:
        logger.info("No CSV files matched %s", config.pattern)
        return CsvReadResult(frame=pd.DataFrame())

    frames: list[pd.DataFrame] = []
    files: list[Path] = []
    failures: list[str] = []
    if len(paths) > 1000:
        failures.append("csv inbox: too many files; only the first 1000 were considered")
        paths = paths[:1000]
    for path in paths:
        failure = _read_one(inbox, path)
        if isinstance(failure, str):
            logger.error("Skipping CSV file: %s", failure)
            failures.append(failure)
            continue
        frame, safe_name = failure
        frames.append(_with_source(frame))
        files.append(path)
        logger.info("Read %s rows from %s", len(frame), safe_name)

    if not frames:
        raise IngestError("All CSV files failed: " + "; ".join(failures))
    combined = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    return CsvReadResult(frame=combined, files=files, failures=failures)


def archive_files(paths: list[Path], archive_dir: Path) -> None:
    """Move processed files out of the inbox. Re-processing is still safe."""
    if not paths:
        return
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_root = archive_dir.resolve()
    # Microseconds keep two runs in the same second from overwriting each other.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for index, path in enumerate(paths, start=1):
        if path.is_symlink() or not path.is_file():
            logger.warning("CSV file is already gone; not archiving")
            continue
        try:
            name = safe_filename(path.name)
            target = resolve_archive_target(archive_root, f"{stamp}_{index}_{name}")
        except ValueError:
            logger.warning("Skipping archive for an unsafe file name")
            continue
        try:
            path.replace(target)
        except OSError:
            shutil.move(str(path), str(target))
        logger.info("Archived %s", name)


def resolve_archive_target(archive_root: Path, filename: str) -> Path:
    """Build an archive path that stays inside `archive_root`."""
    target = archive_root / filename
    if target.is_symlink():
        raise ValueError("archive target is a symlink")
    resolved = target.resolve()
    if not resolved.is_relative_to(archive_root) or resolved == archive_root:
        raise ValueError("archive target escaped the archive directory")
    return resolved


def _read_one(inbox: Path, path: Path) -> tuple[pd.DataFrame, str] | str:
    """Return a frame and a safe name, or a failure message with no internal path."""
    display = _display_name(path)
    if path.is_symlink():
        return _failure(display, "refusing to read a symlink")
    if not is_inside(inbox, path):
        return _failure(display, "refusing to read a file outside the inbox")
    if display is None:
        return "csv: refusing to read a file with an unsafe name"
    try:
        size = path.stat().st_size
    except OSError:
        return _failure(display, "could not be read")
    if size > MAX_CSV_BYTES:
        return _failure(display, "file is too large")
    try:
        frame = pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        return _failure(display, "could not be parsed")
    if frame.empty:
        return _failure(display, "file has no data rows")
    try:
        CsvColumns(names=[str(column) for column in frame.columns])
        CsvShape(rows=len(frame), columns=len(frame.columns))
    except ValidationError:
        return _failure(display, "unsupported columns or too many rows")
    return frame, display


def _display_name(path: Path) -> str | None:
    try:
        return safe_filename(path.name)
    except ValueError:
        return None


def _failure(display: str | None, reason: str) -> str:
    if display is None:
        return f"csv: {reason}"
    return f"csv {display}: {reason}"


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
