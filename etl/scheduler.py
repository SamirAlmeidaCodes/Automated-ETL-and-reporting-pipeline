"""Long-running APScheduler process. One cron job runs the ETL."""

from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from etl.config import load_config
from etl.errors import ConfigError
from etl.heartbeat import write_heartbeat
from etl.pipeline import run_pipeline
from etl.runtime import bootstrap, parse_args

logger = logging.getLogger(__name__)


def _scheduled_run(config_path: str) -> None:
    """Reload config.yaml so location and email edits apply without a restart."""
    try:
        load_dotenv(Path(config_path).resolve().parent / ".env", override=False)
        config = load_config(config_path)
        result = run_pipeline(config)
        if result.warnings:
            logger.error("Scheduled run finished with ingest errors: %s", "; ".join(result.warnings))
    except Exception:
        logger.exception("ETL run failed")


def main(argv: list[str] | None = None) -> int:
    """Start the blocking scheduler. Configuration errors exit before the loop."""
    args = parse_args(
        argv,
        prog="etl-scheduler",
        description="Run the weather ETL pipeline on a cron schedule.",
    )
    try:
        config = bootstrap(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    write_heartbeat(config.app.heartbeat_file)
    scheduler = BlockingScheduler(timezone=config.schedule.timezone)
    try:
        trigger = CronTrigger(
            timezone=config.schedule.timezone,
            **config.schedule.cron.as_kwargs(),
        )
    except ValueError as exc:
        logger.error("Invalid cron schedule: %s", exc)
        return 2

    scheduler.add_job(
        _scheduled_run,
        trigger=trigger,
        kwargs={"config_path": str(config.config_path)},
        id="daily_weather_etl",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=config.schedule.misfire_grace_seconds,
        replace_existing=True,
    )

    def _handle_sigterm(signum: int, _frame: object) -> None:
        logger.info("Received signal %s; shutting down the scheduler", signum)
        if scheduler.running:
            scheduler.shutdown(wait=False)
        else:
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)
    if config.schedule.run_on_start:
        logger.info("Running the pipeline once at startup")
        _scheduled_run(str(config.config_path))

    cron = config.schedule.cron
    now = datetime.now(ZoneInfo(config.schedule.timezone))
    upcoming = trigger.get_next_fire_time(None, now)
    logger.info(
        "Scheduler started. Cron is %s %s %s %s %s (%s). Next run at %s.",
        cron.minute,
        cron.hour,
        cron.day,
        cron.month,
        cron.day_of_week,
        config.schedule.timezone,
        upcoming.isoformat() if upcoming is not None else "unscheduled",
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
