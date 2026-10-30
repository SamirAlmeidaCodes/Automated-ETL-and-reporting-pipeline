"""Config parsing, environment placeholders, and path resolution."""

import pytest

from etl.config import load_config
from etl.errors import ConfigError
from tests.conftest import REPO_ROOT, write_app_config

_SMTP_VARS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
    "SMTP_TO",
    "SMTP_USE_TLS",
    "SMTP_USE_SSL",
)


def test_repository_config_loads(monkeypatch) -> None:
    for name in _SMTP_VARS:
        monkeypatch.delenv(name, raising=False)
    config = load_config(REPO_ROOT / "config.yaml")
    assert config.ingest.sources == ("csv", "api")
    assert [item.name for item in config.ingest.api.locations] == ["New York", "London", "Tokyo"]
    assert config.email.enabled is False
    assert config.schedule.cron.hour == "6"
    assert config.database.url.endswith("/data/weather.db")


def test_relative_sqlite_path_is_resolved_beside_the_config(tmp_path) -> None:
    path = write_app_config(tmp_path, database={"url": "sqlite:///data/weather.db"})
    config = load_config(path)
    assert config.database.url.endswith((tmp_path / "data" / "weather.db").as_posix())


def test_environment_placeholder_uses_default_override_and_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ETL_TEST_CITY", raising=False)
    path = write_app_config(
        tmp_path,
        ingest={
            "sources": ["api"],
            "api": {
                "locations": [
                    {"name": "${ETL_TEST_CITY:-Paris}", "latitude": 48.8566, "longitude": 2.3522}
                ]
            },
        },
    )
    assert load_config(path).ingest.api.locations[0].name == "Paris"
    monkeypatch.setenv("ETL_TEST_CITY", "Oslo")
    assert load_config(path).ingest.api.locations[0].name == "Oslo"
    monkeypatch.setenv("ETL_TEST_CITY", "")
    assert load_config(path).ingest.api.locations[0].name == "Paris"


def test_missing_environment_variable_without_a_default_raises(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("ETL_TEST_REQUIRED", raising=False)
    path = write_app_config(
        tmp_path,
        ingest={
            "sources": ["api"],
            "api": {
                "locations": [
                    {"name": "${ETL_TEST_REQUIRED}", "latitude": 0, "longitude": 0}
                ]
            },
        },
    )
    with pytest.raises(ConfigError, match="ETL_TEST_REQUIRED"):
        load_config(path)


def test_latitude_out_of_range_raises(tmp_path) -> None:
    path = write_app_config(
        tmp_path,
        ingest={
            "sources": ["api"],
            "api": {"locations": [{"name": "Bad", "latitude": 120, "longitude": 0}]},
        },
    )
    with pytest.raises(ConfigError, match="Latitude"):
        load_config(path)


def test_email_enabled_requires_a_host(tmp_path) -> None:
    path = write_app_config(tmp_path, email={"enabled": True})
    with pytest.raises(ConfigError, match="smtp_host"):
        load_config(path)


def test_boolean_strings(tmp_path) -> None:
    disabled = write_app_config(tmp_path, email={"enabled": "false"})
    assert load_config(disabled).email.enabled is False
    enabled = write_app_config(
        tmp_path,
        email={
            "enabled": "true",
            "smtp_host": "smtp.example.com",
            "from_addr": "etl@example.com",
            "to_addrs": ["ops@example.com"],
        },
    )
    assert load_config(enabled).email.enabled is True


def test_unknown_source_raises(tmp_path) -> None:
    path = write_app_config(tmp_path, ingest={"sources": ["s3"]})
    with pytest.raises(ConfigError, match="s3"):
        load_config(path)


def test_missing_config_file_raises(tmp_path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")
