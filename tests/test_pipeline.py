"""One CSV run loads SQLite, writes three charts, and a second run updates the same day."""

from datetime import date, datetime, timezone

from etl.config import load_config
from etl.load.repository import create_db_engine, fetch_daily
from etl.pipeline import run_pipeline
from tests.conftest import write_app_config


def _write_csv(inbox, stamp: str, temp: str) -> None:
    (inbox / "sample.csv").write_text(
        "location,timestamp,temperature_2m,precipitation,wind_speed_10m\n"
        f"Oslo,{stamp},{temp},1.25,8\n",
        encoding="utf-8",
    )


def test_pipeline_loads_csv_and_updates_on_rerun(tmp_path) -> None:
    config = load_config(write_app_config(tmp_path))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    _write_csv(config.ingest.csv.inbox_dir, stamp, "5.5")

    first = run_pipeline(config)
    assert first.warnings == []
    assert first.inserted == 1
    assert first.updated == 0
    assert first.emailed is False
    assert len(first.chart_paths) == 3
    for path in first.chart_paths:
        assert path.exists()
        assert path.read_bytes().startswith(b"\x89PNG")
    assert list(config.ingest.csv.inbox_dir.glob("*.csv")) == []
    assert len(list(config.ingest.csv.archive_dir.glob("*.csv"))) == 1

    _write_csv(config.ingest.csv.inbox_dir, stamp, "9.0")
    second = run_pipeline(config)
    assert second.warnings == []
    assert second.inserted == 0
    assert second.updated == 1
    assert len(list(config.report.output_dir.glob("*.png"))) == 3
    assert len(list(config.ingest.csv.archive_dir.glob("*.csv"))) == 2

    stored = fetch_daily(create_db_engine(config.database.url), since=date(2000, 1, 1))
    assert len(stored) == 1
    assert stored.iloc[0]["location_name"] == "Oslo"
    assert stored.iloc[0]["temp_mean_c"] == 9.0
    assert stored.iloc[0]["source"] == "csv"


def test_broken_csv_stays_in_the_inbox_and_marks_the_run(tmp_path) -> None:
    config = load_config(write_app_config(tmp_path))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    inbox = config.ingest.csv.inbox_dir
    (inbox / "empty.csv").write_text("", encoding="utf-8")
    _write_csv(inbox, stamp, "4.0")

    result = run_pipeline(config)
    assert result.inserted == 1
    assert any("empty.csv" in warning for warning in result.warnings)
    assert (inbox / "empty.csv").exists()
    assert not (inbox / "sample.csv").exists()
