"""Rotating file logs plus a console handler."""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING

from etl.errors import ConfigError
from etl.redaction import RedactingFilter

if TYPE_CHECKING:
    from etl.config import LoggingConfig


class _UtcFormatter(logging.Formatter):
    converter = time.gmtime


def setup_logging(config: LoggingConfig) -> None:
    """Replace the root handlers so a reloaded process does not double-log."""
    if config.file.is_symlink():
        raise ConfigError("logging.file must not be a symlink")
    config.file.parent.mkdir(parents=True, exist_ok=True)
    formatter = _UtcFormatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    file_handler = RotatingFileHandler(
        config.file,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    redactor = RedactingFilter()
    file_handler.addFilter(redactor)
    stream_handler.addFilter(redactor)
    try:
        os.chmod(config.file, 0o640)
    except OSError:
        pass

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(config.level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    # These libraries are chatty at INFO and drown out the pipeline log.
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
