"""Pydantic checks for config, files, CSV headers, and API payloads.

These models sit on the trust boundary. Callers turn ValidationError into
a pipeline error and do not log the rejected value.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_CSV_BYTES = 25 * 1024 * 1024
MAX_CSV_ROWS = 250_000
MAX_CSV_COLUMNS = 64
MAX_HTTP_BYTES = 8 * 1024 * 1024
MAX_CHART_BYTES = 5 * 1024 * 1024

_CRON = re.compile(r"[0-9A-Za-z*,/\-]+")
CHART_NAME = re.compile(r"(?:temperature|precipitation|wind)_\d{4}-\d{2}-\d{2}\.png")


def _has_line_break(value: str) -> bool:
    return any(character in value for character in "\r\n\x00")


def safe_filename(name: str) -> str:
    """Return a single path segment that cannot escape its directory."""
    base = Path(name).name
    if (
        base != name
        or base in {".", ".."}
        or base.startswith(".")
        or ".." in base
        or len(base) > 180
        or not any(character.isalnum() for character in base)
    ):
        raise ValueError("unsafe filename")
    if any(ord(character) < 32 or character in "/\\\x7f" for character in base):
        raise ValueError("unsafe filename")
    return base


def is_inside(root: Path, candidate: Path) -> bool:
    """True when `candidate` is a real file or path strictly inside `root`.

    Symlinks are rejected. `resolve` still collapses `..` for ordinary paths.
    """
    try:
        root_resolved = root.resolve()
        if candidate.is_symlink():
            return False
        candidate_resolved = candidate.resolve()
    except OSError:
        return False
    return candidate_resolved.is_relative_to(root_resolved) and candidate_resolved != root_resolved


def resolve_child(root: Path, filename: str) -> Path:
    """Join a checked filename onto `root` and refuse a path that escapes it."""
    safe = safe_filename(filename)
    base = root.resolve()
    candidate = (base / safe).resolve()
    if not candidate.is_relative_to(base) or candidate == base:
        raise ValueError("path escaped the directory")
    return candidate


def _printable(value: object, limit: int) -> str:
    text = "".join(character for character in str(value) if character.isprintable()).strip()
    return text[:limit]


class LocationLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=256)

    @field_validator("value", mode="before")
    @classmethod
    def printable(cls, value: object) -> str:
        return _printable(value, 256)


class SourceLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=128)

    @field_validator("value", mode="before")
    @classmethod
    def printable(cls, value: object) -> str:
        return _printable(value, 128)


class CronField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=64)

    @field_validator("value")
    @classmethod
    def cron_charset(cls, value: str) -> str:
        if not _CRON.fullmatch(value):
            raise ValueError("invalid cron field")
        return value


class ApiEndpoint(BaseModel):
    """HTTPS forecast URL. Credentials and fragments are rejected."""

    model_config = ConfigDict(extra="forbid")
    base_url: str = Field(min_length=8, max_length=500)

    @field_validator("base_url")
    @classmethod
    def https_without_credentials(cls, value: str) -> str:
        if _has_line_break(value) or any(character.isspace() for character in value):
            raise ValueError("invalid url")
        parsed = urlsplit(value.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("https required")
        if parsed.username or parsed.password or parsed.fragment or "@" in parsed.netloc:
            raise ValueError("credentials are not allowed")
        return value.rstrip("/")


class SqliteUrl(BaseModel):
    """A plain sqlite file URL. Query strings and URI-mode paths are rejected."""

    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=10, max_length=2048)

    @field_validator("url")
    @classmethod
    def plain_file(cls, value: str) -> str:
        if _has_line_break(value) or "?" in value or "#" in value:
            raise ValueError("invalid sqlite url")
        if not value.startswith("sqlite:///"):
            raise ValueError("invalid sqlite url")
        raw_path = value[len("sqlite:///") :]
        if raw_path != ":memory:" and (not raw_path or raw_path.startswith("file:")):
            raise ValueError("invalid sqlite url")
        return value


class EmailSettings(BaseModel):
    """SMTP fields that are later copied into message headers or a login call."""

    model_config = ConfigDict(extra="ignore")
    enabled: bool
    smtp_host: str = Field(max_length=253)
    smtp_port: int = Field(ge=1, le=65535)
    smtp_user: str = Field(max_length=320)
    smtp_password: str = Field(max_length=1024)
    use_tls: bool
    use_ssl: bool
    from_addr: str = Field(max_length=320)
    to_addrs: tuple[str, ...]
    timeout_seconds: float = Field(ge=1, le=300)
    subject: str = Field(min_length=1, max_length=200)

    @field_validator("smtp_host", "smtp_user", "smtp_password", "from_addr", "subject")
    @classmethod
    def single_line(cls, value: str) -> str:
        if _has_line_break(value):
            raise ValueError("control character")
        return value

    @field_validator("smtp_host")
    @classmethod
    def host_name(cls, value: str) -> str:
        if value and (any(character.isspace() for character in value) or "/" in value or "\\" in value or "@" in value):
            raise ValueError("invalid host")
        return value

    @field_validator("to_addrs")
    @classmethod
    def recipients(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for address in value:
            if _has_line_break(address) or len(address) > 320:
                raise ValueError("invalid recipient")
        return value

    @model_validator(mode="after")
    def one_transport(self) -> EmailSettings:
        if self.use_tls and self.use_ssl:
            raise ValueError("tls and ssl are exclusive")
        return self


class CsvColumns(BaseModel):
    model_config = ConfigDict(extra="forbid")
    names: list[str] = Field(min_length=1, max_length=MAX_CSV_COLUMNS)

    @field_validator("names")
    @classmethod
    def column_names(cls, names: list[str]) -> list[str]:
        for name in names:
            text = str(name).strip()
            if not text or len(text) > 128 or _has_line_break(text):
                raise ValueError("unsafe column name")
            if any(not character.isprintable() or character in "/\\" for character in text):
                raise ValueError("unsafe column name")
        return names


class CsvShape(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: int = Field(ge=1, le=MAX_CSV_ROWS)
    columns: int = Field(ge=1, le=MAX_CSV_COLUMNS)


class OpenMeteoHourly(BaseModel):
    """Hourly object from Open-Meteo. Series must be aligned numeric lists."""

    model_config = ConfigDict(extra="allow")
    time: list[str] = Field(max_length=20_000)

    @field_validator("time")
    @classmethod
    def timestamps(cls, values: list[str]) -> list[str]:
        for item in values:
            if not isinstance(item, str) or not item or len(item) > 40 or _has_line_break(item):
                raise ValueError("invalid timestamp")
            if any(not character.isprintable() for character in item):
                raise ValueError("invalid timestamp")
        return values

    @model_validator(mode="after")
    def aligned_series(self) -> OpenMeteoHourly:
        extra: dict[str, Any] = self.__pydantic_extra__ or {}
        if len(extra) > 16:
            raise ValueError("too many variables")
        expected = len(self.time)
        for key, values in extra.items():
            if not isinstance(key, str) or not key or len(key) > 128 or _has_line_break(key):
                raise ValueError("invalid variable")
            if any(not character.isprintable() or character in "/\\" for character in key):
                raise ValueError("invalid variable")
            # Ignore non-list metadata. Requested variables are checked by the caller.
            if not isinstance(values, list):
                continue
            if len(values) != expected:
                raise ValueError("series length mismatch")
            for item in values:
                if item is None:
                    continue
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise ValueError("series must be numeric")
                if isinstance(item, float) and not math.isfinite(item):
                    raise ValueError("series must be finite")
        return self
