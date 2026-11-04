"""Run one ingest, transform, load, and report cycle."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from etl.config import AppConfig
from etl.errors import ConfigError, EtlError, TransformError
from etl.heartbeat import write_heartbeat
from etl.ingest import extract
from etl.ingest.csv_source import archive_files
from etl.load.repository import create_db_engine, fetch_daily, upsert_daily
from etl.redaction import redact_text
from etl.report.charts import render_report
from etl.report.emailer import ReportContext, send_report
from etl.runtime import bootstrap, harden_process, parse_args
from etl.transform.weather import aggregate_daily, prepare_hourly

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    source_rows: int
    hourly_rows: int
    daily_rows: int
    processed: int
    inserted: int
    updated: int
    chart_paths: list[Path]
    emailed: bool
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        charts = ", ".join(path.name for path in self.chart_paths)
        return (
            f"source_rows={self.source_rows} hourly_rows={self.hourly_rows} "
            f"daily_rows={self.daily_rows} upserted={self.processed} "
            f"inserted={self.inserted} updated={self.updated} "
            f"charts=[{charts}] emailed={self.emailed} warnings={len(self.warnings)}"
        )


def run_pipeline(config: AppConfig) -> PipelineResult:
    """Execute one full run. Partial ingest problems are returned as warnings."""
    write_heartbeat(config.app.heartbeat_file)
    try:
        return _run_pipeline(config)
    finally:
        write_heartbeat(config.app.heartbeat_file)


def _run_pipeline(config: AppConfig) -> PipelineResult:
    logger.info("ETL run started")
    extracted = extract(config)
    if extracted.warnings:
        logger.error("Ingest completed with errors: %s", "; ".join(extracted.warnings))

    hourly = prepare_hourly(extracted.frame)
    if len(extracted.frame) > 0 and hourly.empty:
        raise TransformError("Every ingested row was dropped. Check location names and timestamps.")
    daily = aggregate_daily(hourly)
    logger.info(
        "Transform produced %s hourly rows and %s daily rows from %s raw rows",
        len(hourly),
        len(daily),
        len(extracted.frame),
    )

    generated_at = datetime.now(timezone.utc)
    since = generated_at.date() - timedelta(days=config.report.lookback_days - 1)
    engine = create_db_engine(config.database.url)
    try:
        upsert_result = upsert_daily(engine, daily)
        report_frame = fetch_daily(engine, since=since)
    finally:
        engine.dispose()
    logger.info(
        "Loaded %s daily rows (%s inserted, %s updated)",
        upsert_result.processed,
        upsert_result.inserted,
        upsert_result.updated,
    )

    try:
        archive_files(extracted.csv_files, config.ingest.csv.archive_dir)
    except OSError:
        extracted.warnings.append("csv archive: could not move processed files")
        logger.error("Could not archive CSV files; they will be read again on the next run")

    chart_paths = render_report(report_frame, config.report.output_dir, generated_at.date())
    logger.info("Wrote charts: %s", ", ".join(path.name for path in chart_paths))

    emailed = False
    if config.email.enabled:
        send_report(
            config.email,
            ReportContext(
                generated_at=generated_at,
                lookback_start=since,
                processed=upsert_result.processed,
                inserted=upsert_result.inserted,
                updated=upsert_result.updated,
                charts=tuple(chart_paths),
                daily=report_frame,
            ),
        )
        emailed = True
    else:
        logger.info("Email disabled; skipping SMTP delivery")

    result = PipelineResult(
        source_rows=len(extracted.frame),
        hourly_rows=len(hourly),
        daily_rows=len(daily),
        processed=upsert_result.processed,
        inserted=upsert_result.inserted,
        updated=upsert_result.updated,
        chart_paths=chart_paths,
        emailed=emailed,
        warnings=list(extracted.warnings),
    )
    logger.info("ETL run finished: %s", result.summary())
    return result


def main(argv: list[str] | None = None) -> int:
    """Run the pipeline once and return a process exit code."""
    harden_process()
    args = parse_args(
        argv,
        prog="etl",
        description="Run the weather ETL pipeline once.",
    )
    try:
        config = bootstrap(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {redact_text(str(exc))}", file=sys.stderr)
        return 2
    try:
        result = run_pipeline(config)
    except EtlError as exc:
        logger.error("ETL run failed: %s", redact_text(str(exc)))
        return 1
    except Exception:
        logger.error("ETL run failed due to an unexpected error")
        return 1
    return 1 if result.warnings else 0
