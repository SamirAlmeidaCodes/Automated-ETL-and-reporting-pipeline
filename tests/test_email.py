"""The email is assembled without opening a network connection."""

from datetime import date, datetime, timezone
from email.message import EmailMessage

import pandas as pd

from etl.config import EmailConfig
from etl.report.emailer import ReportContext, build_message


def test_message_attaches_three_charts_and_omits_the_password(tmp_path) -> None:
    charts = []
    for name in (
        "temperature_2026-10-28.png",
        "precipitation_2026-10-28.png",
        "wind_2026-10-28.png",
    ):
        path = tmp_path / name
        path.write_bytes(b"\x89PNG\r\n" + name.encode("ascii"))
        charts.append(path)
    context = ReportContext(
        generated_at=datetime(2026, 10, 28, 6, 0, tzinfo=timezone.utc),
        lookback_start=date(2026, 10, 15),
        processed=2,
        inserted=1,
        updated=1,
        charts=tuple(charts),
        daily=pd.DataFrame(
            [
                {
                    "location_name": "Oslo",
                    "observation_date": date(2026, 10, 28),
                    "temp_mean_c": 4.5,
                    "precipitation_sum_mm": 0.0,
                    "wind_speed_mean_kmh": 8.0,
                }
            ]
        ),
    )
    config = EmailConfig(
        enabled=True,
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="mailer",
        smtp_password="super-secret-password",
        use_tls=True,
        use_ssl=False,
        from_addr="etl@example.com",
        to_addrs=("a@example.com", "b@example.com"),
        timeout_seconds=10,
        subject="Daily weather report {date}",
    )
    message = build_message(config, context)
    assert isinstance(message, EmailMessage)
    assert message["Subject"] == "Daily weather report 2026-10-28"
    assert message["From"] == "etl@example.com"
    assert message["To"] == "a@example.com, b@example.com"
    attachments = list(message.iter_attachments())
    assert [item.get_filename() for item in attachments] == [path.name for path in charts]
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert "Oslo" in body
    assert "1 inserted, 1 updated" in body
    assert "Open-Meteo.com" in body
    rendered = message.as_string()
    assert "super-secret-password" not in rendered
