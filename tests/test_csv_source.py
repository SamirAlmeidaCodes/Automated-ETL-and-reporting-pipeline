"""CSV inbox reading and the decision to leave broken files in place."""

import pytest

from etl.config import load_config
from etl.errors import IngestError
from etl.ingest.csv_source import read_csv_inbox
from tests.conftest import write_app_config


def test_empty_inbox_is_a_successful_read(tmp_path) -> None:
    config = load_config(write_app_config(tmp_path))
    result = read_csv_inbox(config.ingest.csv)
    assert result.frame.empty
    assert result.files == []
    assert result.failures == []


def test_reader_adds_a_csv_source_and_skips_a_broken_file(tmp_path) -> None:
    config = load_config(write_app_config(tmp_path))
    inbox = config.ingest.csv.inbox_dir
    (inbox / "empty.csv").write_text("", encoding="utf-8")
    (inbox / "good.csv").write_text(
        "city,time,temperature_2m\nOslo,2026-10-28T00:00:00Z,4\n",
        encoding="utf-8",
    )
    result = read_csv_inbox(config.ingest.csv)
    assert len(result.files) == 1
    assert result.files[0].name == "good.csv"
    assert result.frame.iloc[0]["city"] == "Oslo"
    assert result.frame.iloc[0]["source"] == "csv"
    assert any("empty.csv" in failure for failure in result.failures)
    assert (inbox / "empty.csv").exists()


def test_every_file_failing_raises(tmp_path) -> None:
    config = load_config(write_app_config(tmp_path))
    (config.ingest.csv.inbox_dir / "empty.csv").write_text("", encoding="utf-8")
    with pytest.raises(IngestError, match="empty.csv"):
        read_csv_inbox(config.ingest.csv)
