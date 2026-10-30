"""Build and send the daily report email."""

from __future__ import annotations

import logging
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pandas as pd

from etl.config import EmailConfig
from etl.errors import EmailError

logger = logging.getLogger(__name__)


@dataclass
class ReportContext:
    generated_at: datetime
    lookback_start: date
    processed: int
    inserted: int
    updated: int
    charts: tuple[Path, ...]
    daily: pd.DataFrame


def send_report(config: EmailConfig, context: ReportContext) -> None:
    """Send the report. SMTP failures raise EmailError and do not include the password."""
    if not config.enabled:
        logger.info("Email is disabled; charts were saved and not sent")
        return
    message = build_message(config, context)
    try:
        _deliver(config, message)
    except (smtplib.SMTPException, OSError) as exc:
        raise EmailError(f"Could not send the report email: {exc}") from exc
    logger.info("Sent the report to %s", ", ".join(config.to_addrs))


def build_message(config: EmailConfig, context: ReportContext) -> EmailMessage:
    generated = _as_utc(context.generated_at)
    message = EmailMessage()
    message["Subject"] = config.subject.replace("{date}", generated.date().isoformat())
    message["From"] = config.from_addr
    message["To"] = ", ".join(config.to_addrs)
    message["Date"] = format_datetime(generated)
    message.set_content(_body(context))
    for path in context.charts:
        message.add_attachment(
            path.read_bytes(),
            maintype="image",
            subtype="png",
            filename=path.name,
        )
    return message


def _deliver(config: EmailConfig, message: EmailMessage) -> None:
    if config.use_ssl:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=config.timeout_seconds) as smtp:
            _login_and_send(config, smtp, message)
        return
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.timeout_seconds) as smtp:
        smtp.ehlo()
        if config.use_tls:
            smtp.starttls()
            smtp.ehlo()
        _login_and_send(config, smtp, message)


def _login_and_send(config: EmailConfig, smtp: smtplib.SMTP, message: EmailMessage) -> None:
    if config.smtp_user:
        smtp.login(config.smtp_user, config.smtp_password)
    smtp.send_message(message)


def _body(context: ReportContext) -> str:
    generated = _as_utc(context.generated_at)
    lines = [
        f"Daily weather report for {generated:%Y-%m-%d} {generated:%H:%M} UTC",
        "",
        (
            f"This run wrote {context.processed} daily rows "
            f"({context.inserted} inserted, {context.updated} updated)."
        ),
        f"Charts cover observations from {context.lookback_start.isoformat()} onward.",
        "",
    ]
    frame = context.daily
    if frame.empty:
        lines.append("No daily rows are stored in this window.")
    else:
        lines.append("Latest day in the window:")
        lines.append(f"{'Location':<24} {'Date':<12} {'Temp C':>8} {'Precip mm':>10} {'Wind km/h':>10}")
        latest = frame.groupby("location_name")["observation_date"].max()
        for location in sorted(latest.index, key=str):
            day = latest.loc[location]
            match = frame[(frame["location_name"] == location) & (frame["observation_date"] == day)]
            row = match.iloc[-1]
            lines.append(
                f"{str(location)[:24]:<24} {_date_text(day):<12} "
                f"{_num(row['temp_mean_c']):>8} {_num(row['precipitation_sum_mm']):>10} "
                f"{_num(row['wind_speed_mean_kmh']):>10}"
            )
    lines.extend(
        [
            "",
            "Three charts are attached: temperature, precipitation, and wind.",
            "Weather data by Open-Meteo.com (https://open-meteo.com/).",
        ]
    )
    return "\n".join(lines) + "\n"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _date_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _num(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        if pd.isna(value):
            return "n/a"
    except (TypeError, ValueError):
        return "n/a"
    return f"{float(value):.1f}"
