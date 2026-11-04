"""Process startup: environment file, config, and logging."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from etl import __version__
from etl.config import AppConfig, load_config
from etl.logging_setup import setup_logging

logger = logging.getLogger(__name__)


def install_exception_hook() -> None:
    """Replace the default hook so a crash does not print a traceback or path."""

    def _hook(exc_type: type[BaseException], exc: BaseException, _tb: object) -> None:
        if exc_type in (KeyboardInterrupt, SystemExit):
            sys.__excepthook__(exc_type, exc, None)
            return
        logging.getLogger("etl").error(
            "Unhandled error (%s). Internal details were not written.",
            getattr(exc_type, "__name__", "error"),
        )
        print(
            "Unexpected error. The process stopped without writing internal details.",
            file=sys.stderr,
        )

    sys.excepthook = _hook


def harden_process() -> None:
    """Create new files as owner-writable and group-readable, not world-readable."""
    os.umask(0o027)
    install_exception_hook()


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
    harden_process()
    candidate = Path(config_arg or os.environ.get("ETL_CONFIG", "config.yaml")).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    # Do not override variables that the operator already exported.
    # .env is local. It is not copied into the image and must not be committed.
    load_dotenv(candidate.resolve().parent / ".env", override=False)
    load_dotenv(override=False)
    config = load_config(candidate)
    setup_logging(config.logging)
    logger.info("Starting %s %s", config.app.name, __version__)
    logger.info(
        "Config file=%s sources=%s email_enabled=%s log_level=%s",
        config.config_path.name,
        ",".join(config.ingest.sources),
        config.email.enabled,
        config.logging.level,
    )
    if config.logging.debug_suppressed:
        logger.warning("DEBUG logging is disabled outside development")
    return config
