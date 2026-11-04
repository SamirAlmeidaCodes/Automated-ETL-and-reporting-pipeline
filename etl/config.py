"""Load config.yaml and resolve paths and environment placeholders."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import ValidationError

from etl.errors import ConfigError
from etl.redaction import register_secret
from etl.schema import OPEN_METEO_VARIABLES
from etl.validation import ApiEndpoint, CronField, EmailSettings, SqliteUrl

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SUPPORTED_SOURCES = ("csv", "api")
_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}
# DEBUG is kept only for an explicit local environment. Everything else is production.
_DEV_ENVS = {"dev", "development", "test"}


@dataclass(frozen=True)
class LocationConfig:
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class ApiConfig:
    base_url: str
    timeout_seconds: float
    retries: int
    backoff_seconds: float
    past_days: int
    forecast_days: int
    hourly_variables: tuple[str, ...]
    locations: tuple[LocationConfig, ...]


@dataclass(frozen=True)
class CsvConfig:
    inbox_dir: Path
    pattern: str
    archive_dir: Path


@dataclass(frozen=True)
class IngestConfig:
    sources: tuple[str, ...]
    csv: CsvConfig
    api: ApiConfig


@dataclass(frozen=True)
class CronConfig:
    minute: str
    hour: str
    day: str
    month: str
    day_of_week: str

    def as_kwargs(self) -> dict[str, str]:
        return {
            "minute": self.minute,
            "hour": self.hour,
            "day": self.day,
            "month": self.month,
            "day_of_week": self.day_of_week,
        }


@dataclass(frozen=True)
class ScheduleConfig:
    timezone: str
    cron: CronConfig
    run_on_start: bool
    misfire_grace_seconds: int


@dataclass(frozen=True)
class DatabaseConfig:
    url: str


@dataclass(frozen=True)
class ReportConfig:
    output_dir: Path
    lookback_days: int


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    use_tls: bool
    use_ssl: bool
    from_addr: str
    to_addrs: tuple[str, ...]
    timeout_seconds: float
    subject: str


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    file: Path
    max_bytes: int
    backup_count: int
    debug_suppressed: bool = False


@dataclass(frozen=True)
class AppInfo:
    name: str
    heartbeat_file: Path


@dataclass(frozen=True)
class AppConfig:
    config_path: Path
    base_dir: Path
    app: AppInfo
    schedule: ScheduleConfig
    ingest: IngestConfig
    database: DatabaseConfig
    report: ReportConfig
    email: EmailConfig
    logging: LoggingConfig


def load_config(path: str | Path | None = None) -> AppConfig:
    """Read the YAML config. Relative paths are resolved from the file's directory."""
    config_path = _config_path(path)
    if not config_path.is_file():
        raise ConfigError("Config file not found")
    try:
        raw_text = config_path.read_text(encoding="utf-8")
        loaded = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError("Could not parse the config file") from exc
    except OSError as exc:
        raise ConfigError("Could not read the config file") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{config_path} must contain a mapping at the top level")
    _reject_literal_secrets(loaded)
    data = _interpolate(loaded)
    base_dir = config_path.parent
    return AppConfig(
        config_path=config_path,
        base_dir=base_dir,
        app=_parse_app(data, base_dir),
        schedule=_parse_schedule(data),
        ingest=_parse_ingest(data, base_dir),
        database=_parse_database(data, base_dir),
        report=_parse_report(data, base_dir),
        email=_parse_email(data),
        logging=_parse_logging(data, base_dir),
    )


def _config_path(path: str | Path | None) -> Path:
    if path is None:
        path = os.environ.get("ETL_CONFIG", "config.yaml")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve()


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(_replace_env, value)
    if isinstance(value, list):
        return [_interpolate(item) for item in value]
    if isinstance(value, dict):
        return {key: _interpolate(item) for key, item in value.items()}
    return value


def _reject_literal_secrets(raw: dict[str, Any]) -> None:
    """SMTP password must be an environment placeholder, never a literal."""
    email = raw.get("email")
    if not isinstance(email, dict):
        return
    _require_env_placeholder(email.get("smtp_password"), field="email.smtp_password")


def _require_env_placeholder(value: Any, *, field: str) -> None:
    if value is None or value == "":
        return
    if not isinstance(value, str):
        raise ConfigError(f"{field} must be set from an environment variable")
    match = _ENV_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ConfigError(f"{field} must be set from an environment variable")
    # ${VAR:-secret} would commit the secret as the default. An empty default is fine.
    if match.group(2):
        raise ConfigError(f"{field} must not include a default secret")


def _replace_env(match: re.Match[str]) -> str:
    name = match.group(1)
    has_default = match.group(2) is not None
    current = os.environ.get(name)
    # Match shell ${VAR:-default}: an empty value uses the default.
    if current:
        return current
    if has_default:
        return match.group(2)
    if current is not None:
        return current
    raise ConfigError(f"Environment variable {name} is not set and has no default")


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    return value


def _parse_app(data: dict[str, Any], base_dir: Path) -> AppInfo:
    section = _section(data, "app")
    name = _as_str(section.get("name"), field="app.name", default="weather-etl")
    if len(name) > 64:
        raise ConfigError("app.name is too long")
    heartbeat = _resolve_path(
        _as_str(section.get("heartbeat_file"), field="app.heartbeat_file", default="data/heartbeat.txt"),
        base_dir,
    )
    return AppInfo(name=name, heartbeat_file=heartbeat)


def _parse_schedule(data: dict[str, Any]) -> ScheduleConfig:
    section = _section(data, "schedule")
    timezone_name = _as_str(section.get("timezone"), field="schedule.timezone", default="UTC")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"Unknown schedule.timezone {timezone_name!r}") from exc
    cron_section = section.get("cron", {})
    if cron_section is None:
        cron_section = {}
    if not isinstance(cron_section, dict):
        raise ConfigError("schedule.cron must be a mapping")
    cron = CronConfig(
        minute=_as_cron(cron_section.get("minute"), default="0", field="schedule.cron.minute"),
        hour=_as_cron(cron_section.get("hour"), default="6", field="schedule.cron.hour"),
        day=_as_cron(cron_section.get("day"), default="*", field="schedule.cron.day"),
        month=_as_cron(cron_section.get("month"), default="*", field="schedule.cron.month"),
        day_of_week=_as_cron(
            cron_section.get("day_of_week"),
            default="*",
            field="schedule.cron.day_of_week",
        ),
    )
    return ScheduleConfig(
        timezone=timezone_name,
        cron=cron,
        run_on_start=_as_bool(section.get("run_on_start"), field="schedule.run_on_start", default=True),
        misfire_grace_seconds=_as_int(
            section.get("misfire_grace_seconds"),
            field="schedule.misfire_grace_seconds",
            default=3600,
            minimum=0,
            maximum=86400,
        ),
    )


def _parse_ingest(data: dict[str, Any], base_dir: Path) -> IngestConfig:
    section = _section(data, "ingest")
    if "sources" not in section and "source" not in section:
        raise ConfigError("ingest.sources is required")
    sources = _parse_sources(section.get("sources", section.get("source")))
    csv_section = _section(section, "csv")
    api_section = _section(section, "api")
    inbox = _resolve_path(
        _as_str(csv_section.get("inbox_dir"), field="ingest.csv.inbox_dir", default="data/inbox"),
        base_dir,
    )
    archive = _resolve_path(
        _as_str(csv_section.get("archive_dir"), field="ingest.csv.archive_dir", default="data/archive"),
        base_dir,
    )
    pattern = _as_str(csv_section.get("pattern"), field="ingest.csv.pattern", default="*.csv")
    if (
        not pattern
        or not pattern.endswith(".csv")
        or "/" in pattern
        or "\\" in pattern
        or ".." in pattern
        or pattern.startswith("~")
    ):
        raise ConfigError("ingest.csv.pattern must be a filename glob ending in .csv")
    if inbox == archive or _is_inside(archive, inbox) or _is_inside(inbox, archive):
        raise ConfigError("ingest.csv inbox_dir and archive_dir must be separate directories")
    api = _parse_api(api_section, require_locations="api" in sources)
    return IngestConfig(
        sources=sources,
        csv=CsvConfig(inbox_dir=inbox, pattern=pattern, archive_dir=archive),
        api=api,
    )


def _parse_sources(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        raise ConfigError("ingest.sources must be a list of source names")
    ordered: list[str] = []
    for item in items:
        name = str(item).strip().lower()
        if name not in _SUPPORTED_SOURCES:
            supported = ", ".join(_SUPPORTED_SOURCES)
            raise ConfigError(f"Unknown ingest source {name!r}. Supported sources: {supported}")
        if name not in ordered:
            ordered.append(name)
    if not ordered:
        raise ConfigError("ingest.sources must list at least one source")
    return tuple(ordered)


def _parse_api(section: dict[str, Any], *, require_locations: bool) -> ApiConfig:
    base_url = _as_str(
        section.get("base_url"),
        field="ingest.api.base_url",
        default="https://api.open-meteo.com/v1/forecast",
    )
    try:
        base_url = ApiEndpoint(base_url=base_url).base_url
    except ValidationError:
        raise ConfigError("ingest.api.base_url must be an https URL without credentials") from None
    variables = section.get("hourly_variables", list(OPEN_METEO_VARIABLES))
    if not isinstance(variables, list) or not variables:
        raise ConfigError("ingest.api.hourly_variables must be a non-empty list")
    cleaned: list[str] = []
    for variable in variables:
        name = str(variable).strip()
        if name not in OPEN_METEO_VARIABLES:
            supported = ", ".join(OPEN_METEO_VARIABLES)
            raise ConfigError(f"Unsupported hourly variable {name!r}. Supported variables: {supported}")
        if name not in cleaned:
            cleaned.append(name)
    locations = _parse_locations(section.get("locations", []))
    if require_locations and not locations:
        raise ConfigError("ingest.api.locations must list at least one location when api is a source")
    past_days = _as_int(section.get("past_days"), field="ingest.api.past_days", default=7, minimum=0, maximum=92)
    forecast_days = _as_int(
        section.get("forecast_days"),
        field="ingest.api.forecast_days",
        default=1,
        minimum=0,
        maximum=16,
    )
    if past_days + forecast_days < 1:
        raise ConfigError("ingest.api past_days and forecast_days cannot both be 0")
    return ApiConfig(
        base_url=base_url.rstrip("/"),
        timeout_seconds=_as_float(
            section.get("timeout_seconds"),
            field="ingest.api.timeout_seconds",
            default=30.0,
            minimum=1.0,
            maximum=300.0,
        ),
        retries=_as_int(section.get("retries"), field="ingest.api.retries", default=3, minimum=1, maximum=8),
        backoff_seconds=_as_float(
            section.get("backoff_seconds"),
            field="ingest.api.backoff_seconds",
            default=2.0,
            minimum=0.0,
            maximum=30.0,
        ),
        past_days=past_days,
        forecast_days=forecast_days,
        hourly_variables=tuple(cleaned),
        locations=tuple(locations),
    )


def _parse_locations(value: Any) -> list[LocationConfig]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConfigError("ingest.api.locations must be a list")
    locations: list[LocationConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"ingest.api.locations[{index}] must be a mapping")
        name = _as_str(item.get("name"), field=f"ingest.api.locations[{index}].name")
        if name in seen:
            raise ConfigError(f"Duplicate location name {name!r}")
        seen.add(name)
        latitude = _as_float(item.get("latitude"), field=f"ingest.api.locations[{index}].latitude")
        longitude = _as_float(item.get("longitude"), field=f"ingest.api.locations[{index}].longitude")
        if math.isnan(latitude) or math.isinf(latitude) or not -90 <= latitude <= 90:
            raise ConfigError(f"Latitude for {name} must be between -90 and 90")
        if math.isnan(longitude) or math.isinf(longitude) or not -180 <= longitude <= 180:
            raise ConfigError(f"Longitude for {name} must be between -180 and 180")
        locations.append(LocationConfig(name=name, latitude=latitude, longitude=longitude))
    if len(locations) > 100:
        raise ConfigError("ingest.api.locations cannot list more than 100 locations")
    return locations


def _parse_database(data: dict[str, Any], base_dir: Path) -> DatabaseConfig:
    section = _section(data, "database")
    url = _as_str(section.get("url"), field="database.url")
    if not url.startswith("sqlite:///"):
        raise ConfigError("database.url must be a sqlite:/// URL")
    return DatabaseConfig(url=_resolve_sqlite_url(url, base_dir))


def _resolve_sqlite_url(url: str, base_dir: Path) -> str:
    raw_path = url[len("sqlite:///") :]
    if raw_path == ":memory:":
        try:
            return SqliteUrl(url=url).url
        except ValidationError:
            raise ConfigError("database.url must be a sqlite:/// URL") from None
    if not raw_path or raw_path.startswith("file:"):
        raise ConfigError("database.url must point at a database file")
    path = Path(raw_path)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    else:
        path = path.resolve()
    try:
        return SqliteUrl(url="sqlite:///" + path.as_posix()).url
    except ValidationError:
        raise ConfigError("database.url must be a sqlite:/// URL") from None


def _parse_report(data: dict[str, Any], base_dir: Path) -> ReportConfig:
    section = _section(data, "report")
    output = _resolve_path(
        _as_str(section.get("output_dir"), field="report.output_dir", default="output/reports"),
        base_dir,
    )
    return ReportConfig(
        output_dir=output,
        lookback_days=_as_int(
            section.get("lookback_days"),
            field="report.lookback_days",
            default=14,
            minimum=1,
            maximum=3660,
        ),
    )


def _parse_email(data: dict[str, Any]) -> EmailConfig:
    section = _section(data, "email")
    enabled = _as_bool(section.get("enabled"), field="email.enabled", default=False)
    use_tls = _as_bool(section.get("use_tls"), field="email.use_tls", default=True)
    use_ssl = _as_bool(section.get("use_ssl"), field="email.use_ssl", default=False)
    if use_tls and use_ssl:
        raise ConfigError("email.use_tls and email.use_ssl cannot both be true")
    host = _as_str(section.get("smtp_host"), field="email.smtp_host", default="")
    user = _as_str(section.get("smtp_user"), field="email.smtp_user", default="")
    password = _as_str(section.get("smtp_password"), field="email.smtp_password", default="")
    sender = _as_str(section.get("from_addr"), field="email.from_addr", default="")
    recipients = _parse_recipients(section.get("to_addrs", []))
    subject = _as_str(section.get("subject"), field="email.subject", default="Daily weather report {date}")
    port = _as_int(section.get("smtp_port"), field="email.smtp_port", default=587, minimum=1, maximum=65535)
    timeout = _as_float(
        section.get("timeout_seconds"),
        field="email.timeout_seconds",
        default=30.0,
        minimum=1.0,
        maximum=300.0,
    )
    if enabled:
        if not host:
            raise ConfigError("email.smtp_host is required when email is enabled")
        if "@" not in sender:
            raise ConfigError("email.from_addr must be an email address when email is enabled")
        if not recipients:
            raise ConfigError("email.to_addrs must list at least one recipient when email is enabled")
        for recipient in recipients:
            if any(character in recipient for character in "\r\n\x00"):
                raise ConfigError("email.to_addrs contains an invalid address")
            if "@" not in recipient:
                raise ConfigError(f"Invalid recipient address {recipient!r}")
    register_secret(password)
    try:
        EmailSettings(
            enabled=enabled,
            smtp_host=host,
            smtp_port=port,
            smtp_user=user,
            smtp_password=password,
            use_tls=use_tls,
            use_ssl=use_ssl,
            from_addr=sender,
            to_addrs=tuple(recipients),
            timeout_seconds=timeout,
            subject=subject,
        )
    except ValidationError:
        raise ConfigError("email settings are invalid") from None
    return EmailConfig(
        enabled=enabled,
        smtp_host=host,
        smtp_port=port,
        smtp_user=user,
        smtp_password=password,
        use_tls=use_tls,
        use_ssl=use_ssl,
        from_addr=sender,
        to_addrs=tuple(recipients),
        timeout_seconds=timeout,
        subject=subject,
    )


def _parse_recipients(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        raise ConfigError("email.to_addrs must be a list of addresses")
    recipients: list[str] = []
    for item in items:
        for part in item.split(","):
            address = part.strip()
            if address:
                recipients.append(address)
    return recipients


def _parse_logging(data: dict[str, Any], base_dir: Path) -> LoggingConfig:
    section = _section(data, "logging")
    requested = _as_str(section.get("level"), field="logging.level", default="INFO").upper()
    if requested not in _LOG_LEVELS:
        raise ConfigError(f"logging.level must be one of {', '.join(sorted(_LOG_LEVELS))}")
    environment = os.environ.get("ETL_ENV", "production").strip().lower()
    suppressed = False
    level = requested
    if environment not in _DEV_ENVS and requested == "DEBUG":
        level = "INFO"
        suppressed = True
    return LoggingConfig(
        level=level,
        file=_resolve_path(_as_str(section.get("file"), field="logging.file", default="logs/etl.log"), base_dir),
        max_bytes=_as_int(
            section.get("max_bytes"),
            field="logging.max_bytes",
            default=5_242_880,
            minimum=1024,
            maximum=52_428_800,
        ),
        debug_suppressed=suppressed,
        backup_count=_as_int(
            section.get("backup_count"),
            field="logging.backup_count",
            default=5,
            minimum=1,
            maximum=50,
        ),
    )


def _resolve_path(value: str, base_dir: Path) -> Path:
    if any(character in value for character in "\r\n\x00"):
        raise ConfigError("paths must not contain control characters")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return path != parent


def _as_str(value: Any, *, field: str, default: str | None = None) -> str:
    if value is None:
        if default is None:
            raise ConfigError(f"{field} is required")
        return default
    text = str(value).strip()
    if text == "" and default is not None:
        return default
    if text == "":
        raise ConfigError(f"{field} is required")
    if any(character in text for character in "\r\n\x00"):
        raise ConfigError(f"{field} must be a single line")
    return text


def _as_bool(value: Any, *, field: str, default: bool | None = None) -> bool:
    if value is None or value == "":
        if default is None:
            raise ConfigError(f"{field} is required")
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    raise ConfigError(f"{field} must be a boolean")


def _as_int(
    value: Any,
    *,
    field: str,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if value is None or value == "":
        if default is None:
            raise ConfigError(f"{field} is required")
        number = default
    else:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{field} must be an integer") from exc
    if minimum is not None and number < minimum:
        raise ConfigError(f"{field} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ConfigError(f"{field} must be <= {maximum}")
    return number


def _as_float(
    value: Any,
    *,
    field: str,
    default: float | None = None,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if value is None or value == "":
        if default is None:
            raise ConfigError(f"{field} is required")
        number = default
    else:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{field} must be a number") from exc
    if math.isnan(number) or math.isinf(number):
        raise ConfigError(f"{field} must be a finite number")
    if minimum is not None and number < minimum:
        raise ConfigError(f"{field} must be >= {minimum}")
    if maximum is not None and number > maximum:
        raise ConfigError(f"{field} must be <= {maximum}")
    return number


def _as_cron(value: Any, *, default: str, field: str) -> str:
    if value is None or value == "":
        text = default
    else:
        text = str(value).strip()
    if not text:
        raise ConfigError(f"{field} cannot be empty")
    try:
        return CronField(value=text).value
    except ValidationError:
        raise ConfigError(f"{field} is not a valid cron field") from None
