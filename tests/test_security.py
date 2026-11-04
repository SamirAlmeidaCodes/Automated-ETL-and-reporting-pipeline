"""Boundaries: secrets, paths, payloads, logs, and database parameters."""

import logging
import sys
from datetime import date, datetime, timezone

import pandas as pd
import pytest

from etl.config import EmailConfig, load_config
from etl.errors import ConfigError, EmailError, IngestError
from etl.ingest.api_source import _frame_from_payload
from etl.ingest.csv_source import read_csv_inbox, resolve_archive_target
from etl.load.repository import create_db_engine, fetch_daily, upsert_daily
from etl.logging_setup import setup_logging
from etl.report.emailer import ReportContext, build_message
from etl.runtime import install_exception_hook
from etl.schema import DAILY_COLUMNS
from etl.transform.weather import prepare_hourly
from tests.conftest import write_app_config


def test_literal_smtp_password_is_rejected(tmp_path) -> None:
    path = write_app_config(tmp_path, email={"smtp_password": "literal-secret"})
    with pytest.raises(ConfigError, match="environment"):
        load_config(path)


def test_smtp_password_default_secret_is_rejected(tmp_path) -> None:
    path = write_app_config(tmp_path, email={"smtp_password": "${SMTP_PASSWORD:-literal-secret}"})
    with pytest.raises(ConfigError, match="default secret"):
        load_config(path)


def test_api_url_must_be_https_without_credentials(tmp_path) -> None:
    plain = write_app_config(tmp_path, ingest={"api": {"base_url": "http://api.open-meteo.com/v1/forecast"}})
    with pytest.raises(ConfigError, match="https"):
        load_config(plain)
    embedded = write_app_config(
        tmp_path,
        ingest={"api": {"base_url": "https://user:secret@api.open-meteo.com/v1/forecast"}},
    )
    with pytest.raises(ConfigError, match="https"):
        load_config(embedded)


def test_sqlite_url_rejects_query_strings(tmp_path) -> None:
    path = write_app_config(tmp_path, database={"url": "sqlite:///data/weather.db?mode=rw"})
    with pytest.raises(ConfigError, match="sqlite"):
        load_config(path)


def test_cron_field_rejects_shell_metacharacters(tmp_path) -> None:
    path = write_app_config(tmp_path, schedule={"cron": {"minute": "0;reboot"}})
    with pytest.raises(ConfigError, match="cron"):
        load_config(path)


def test_header_injection_in_email_address_is_rejected(tmp_path) -> None:
    path = write_app_config(tmp_path, email={"from_addr": "etl@example.com\r\nBcc: other@example.com"})
    with pytest.raises(ConfigError, match="single line"):
        load_config(path)


def test_debug_logging_is_disabled_outside_development(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ETL_ENV", "production")
    path = write_app_config(tmp_path, logging={"level": "DEBUG"})
    config = load_config(path)
    assert config.logging.level == "INFO"
    assert config.logging.debug_suppressed is True

    monkeypatch.setenv("ETL_ENV", "development")
    assert load_config(path).logging.level == "DEBUG"


def test_symlink_outside_the_inbox_is_not_read(tmp_path) -> None:
    config = load_config(write_app_config(tmp_path))
    outside = tmp_path / "secret.csv"
    outside.write_text("location,timestamp\nSecret,2026-01-01T00:00:00Z\n", encoding="utf-8")
    link = config.ingest.csv.inbox_dir / "link.csv"
    link.symlink_to(outside)
    with pytest.raises(IngestError, match="symlink") as raised:
        read_csv_inbox(config.ingest.csv)
    assert str(outside) not in str(raised.value)
    assert "Secret" not in str(raised.value)


def test_archive_target_cannot_escape(tmp_path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    with pytest.raises(ValueError):
        resolve_archive_target(archive.resolve(), "../evil.csv")


def test_control_characters_are_stripped_from_location_names() -> None:
    frame = prepare_hourly(
        pd.DataFrame(
            {
                "location": ["Oslo\nBcc: attacker"],
                "timestamp": ["2026-01-01T00:00:00Z"],
                "temperature_c": [1.0],
            }
        )
    )
    assert frame.iloc[0]["location"] == "OsloBcc: attacker"
    assert "\n" not in frame.iloc[0]["location"]


def test_api_payload_accepts_numbers_and_hides_rejected_values() -> None:
    frame = _frame_from_payload(
        "Oslo",
        {"hourly": {"time": ["2026-01-01T00:00"], "temperature_2m": [1], "precipitation": [None]}},
        ("temperature_2m", "precipitation"),
    )
    assert frame.iloc[0]["temperature_2m"] == 1
    assert frame.iloc[0]["location"] == "Oslo"

    injected = "1; drop table daily_weather"
    with pytest.raises(IngestError, match="incomplete") as raised:
        _frame_from_payload(
            "Oslo",
            {"hourly": {"time": ["2026-01-01T00:00"], "temperature_2m": [injected]}},
            ("temperature_2m",),
        )
    assert injected not in str(raised.value)


def test_upsert_binds_location_text(tmp_path) -> None:
    engine = create_db_engine("sqlite:///" + (tmp_path / "weather.db").as_posix())
    assert engine.hide_parameters is True
    hostile = "Oslo'; drop table daily_weather;--"
    frame = pd.DataFrame(
        [
            {
                "location_name": hostile,
                "observation_date": date(2026, 1, 1),
                "temp_min_c": 0.0,
                "temp_max_c": 2.0,
                "temp_mean_c": 1.0,
                "precipitation_sum_mm": 0.0,
                "humidity_mean_pct": 50.0,
                "wind_speed_mean_kmh": 10.0,
                "observation_count": 24,
                "source": "api",
            }
        ]
    )
    upsert_daily(engine, frame.loc[:, list(DAILY_COLUMNS)])
    stored = fetch_daily(engine, since=date(2026, 1, 1))
    assert stored.iloc[0]["location_name"] == hostile
    assert len(fetch_daily(engine, since=date(2020, 1, 1))) == 1


def test_log_records_do_not_keep_secrets_or_tracebacks(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SMTP_PASSWORD", "super-secret-password")
    path = write_app_config(
        tmp_path,
        email={"smtp_password": "${SMTP_PASSWORD:-}"},
        logging={"file": str(tmp_path / "etl.log")},
    )
    config = load_config(path)
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_level = root.level
    try:
        setup_logging(config.logging)
        logging.getLogger("etl.test").info("delivery used %s", "super-secret-password")
        logging.getLogger("etl.test").error("password=%s", "super-secret-password")
        try:
            raise RuntimeError("boom /tmp/internal")
        except RuntimeError:
            logging.getLogger("etl.test").exception("should not keep this traceback")
    finally:
        root.handlers.clear()
        for handler in previous_handlers:
            root.addHandler(handler)
        root.setLevel(previous_level)
    text = (tmp_path / "etl.log").read_text(encoding="utf-8")
    assert "super-secret-password" not in text
    assert "[redacted]" in text
    assert "Traceback" not in text
    assert "/tmp/internal" not in text


def test_excepthook_omits_traceback_and_paths(capsys) -> None:
    previous = sys.excepthook
    try:
        install_exception_hook()
        sys.excepthook(RuntimeError, RuntimeError("secret /home/private/config"), None)
    finally:
        sys.excepthook = previous
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "secret" not in captured.err
    assert "/home/private/config" not in captured.err
    assert "Unexpected error" in captured.err


def test_email_refuses_an_unexpected_attachment(tmp_path) -> None:
    rogue = tmp_path / "notes.txt"
    rogue.write_text("secret", encoding="utf-8")
    context = ReportContext(
        generated_at=datetime(2026, 10, 28, tzinfo=timezone.utc),
        lookback_start=date(2026, 10, 1),
        processed=0,
        inserted=0,
        updated=0,
        charts=(rogue,),
        daily=pd.DataFrame(),
    )
    config = EmailConfig(
        enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="",
        smtp_password="",
        use_tls=True,
        use_ssl=False,
        from_addr="etl@example.com",
        to_addrs=("ops@example.com",),
        timeout_seconds=5,
        subject="Daily weather report {date}",
    )
    with pytest.raises(EmailError, match="unexpected"):
        build_message(config, context)
