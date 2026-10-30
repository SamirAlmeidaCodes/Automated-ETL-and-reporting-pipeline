"""Process startup: environment file, config, and logging."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from etl import __version__
from etl.config import AppConfig, load_config
from etl.logging_setup import setup_logging

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None, *, prog: str, description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=prog, description=description)
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml. Defaults to the ETL_CONFIG environment variable or ./config.yaml.",
    )
    return parser.parse_args(argv)


def bootstrap(config_arg: str | None) -> AppConfig:
    """Load .env, parse config, and attach the rotating log handler."""
    candidate = Path(config_arg or os.environ.get("ETL_CONFIG", "config.yaml")).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Do not override variables that the operator already exported.
    load_dotenv(candidate.resolve().parent / ".env", override=False)
    load_dotenv(override=False)
    config = load_config(candidate)
    setup_logging(config.logging)
    logger.info("Starting %s %s", config.app.name, __version__)
    logger.info(
        "Config %s sources=%s email_enabled=%s database=%s",
        config.config_path,
        ",".join(config.ingest.sources),
        config.email.enabled,
        config.database.url,
    )
    return config
