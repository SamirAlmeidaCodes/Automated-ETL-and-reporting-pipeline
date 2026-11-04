"""Fetch hourly weather from the Open-Meteo forecast API."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field

import pandas as pd
import requests
from pydantic import ValidationError

from etl import __version__
from etl.config import ApiConfig, LocationConfig
from etl.errors import IngestError
from etl.redaction import public_text
from etl.retry import RetryableFetchError, call_with_retry
from etl.validation import MAX_HTTP_BYTES, OpenMeteoHourly

logger = logging.getLogger(__name__)

# These statuses are usually temporary. Other 4xx responses are not retried.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class ApiReadResult:
    frame: pd.DataFrame
    failures: list[str] = field(default_factory=list)


def fetch_weather(api: ApiConfig, app_name: str) -> ApiReadResult:
    """Fetch every configured location.

    One location is retried on its own. If at least one location returns data,
    the others are reported as failures and the frame still contains the rows
    that succeeded. If every location fails, this raises.
    """
    if not api.locations:
        raise IngestError("No API locations are configured")
    frames: list[pd.DataFrame] = []
    failures: list[str] = []
    with requests.Session() as session:
        session.max_redirects = 0
        session.headers.update(
            {
                "User-Agent": _user_agent(app_name),
                "Accept": "application/json",
            }
        )
        for location in api.locations:
            try:
                frames.append(_fetch_location(session, api, location))
            except IngestError as exc:
                logger.error("%s", exc)
                failures.append(str(exc))
    if not frames:
        detail = "; ".join(failures) if failures else "no locations configured"
        raise IngestError(f"All API locations failed: {detail}")
    if failures:
        logger.error("Continuing after %s API location failure(s)", len(failures))
    combined = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0]
    return ApiReadResult(frame=combined, failures=failures)


def _fetch_location(session: requests.Session, api: ApiConfig, location: LocationConfig) -> pd.DataFrame:
    params = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "hourly": ",".join(api.hourly_variables),
        "timezone": "UTC",
        "past_days": api.past_days,
        "forecast_days": api.forecast_days,
        # Fixed so the transform and charts stay in °C, mm, and km/h.
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
    }

    def once() -> pd.DataFrame:
        try:
            response = session.get(
                api.base_url,
                params=params,
                timeout=api.timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
        except (requests.Timeout, requests.ConnectionError):
            raise RetryableFetchError(f"{location.name}: temporary network error") from None
        try:
            if response.status_code in RETRYABLE_STATUS:
                raise RetryableFetchError(f"{location.name}: HTTP {response.status_code}")
            if response.status_code >= 300:
                reason = _safe_error_reason(response)
                message = f"Open-Meteo rejected {location.name} (HTTP {response.status_code})"
                if reason:
                    message = f"{message}: {reason}"
                raise IngestError(message)
            try:
                raw = _read_limited(response)
            except IngestError:
                raise IngestError(f"Open-Meteo response for {location.name} was too large") from None
            except UnicodeError:
                raise RetryableFetchError(f"{location.name}: response was not JSON") from None
            try:
                payload = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError):
                raise RetryableFetchError(f"{location.name}: response was not JSON") from None
        finally:
            response.close()
        if isinstance(payload, dict) and payload.get("error"):
            reason = public_text(payload.get("reason") or "unknown error", limit=120)
            raise IngestError(f"Open-Meteo rejected {location.name}: {reason}")
        return _frame_from_payload(location.name, payload, api.hourly_variables)

    try:
        frame = call_with_retry(
            once,
            attempts=api.retries,
            backoff_seconds=api.backoff_seconds,
            retry_on=(RetryableFetchError,),
            sleep=time.sleep,
            operation=f"fetch {location.name}",
        )
    except RetryableFetchError as exc:
        raise IngestError(f"Failed to fetch {location.name} after {api.retries} attempts: {exc}") from exc
    logger.info("Fetched %s hourly rows for %s", len(frame), location.name)
    return frame


def _frame_from_payload(location_name: str, payload: object, variables: tuple[str, ...]) -> pd.DataFrame:
    if not isinstance(payload, dict):
        raise IngestError(f"Open-Meteo response for {location_name} was not a JSON object")
    try:
        hourly = OpenMeteoHourly.model_validate(payload.get("hourly"))
    except ValidationError:
        raise IngestError(f"Open-Meteo response for {location_name} was incomplete") from None
    data: dict[str, object] = {
        "location": location_name,
        "timestamp": hourly.time,
        "source": "api",
    }
    series = hourly.__pydantic_extra__ or {}
    for variable in variables:
        values = series.get(variable)
        if not isinstance(values, list):
            raise IngestError(f"Open-Meteo response for {location_name} is missing hourly.{variable}")
        data[variable] = values
    return pd.DataFrame(data)


def _read_limited(response: requests.Response) -> str:
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > MAX_HTTP_BYTES:
                raise IngestError("response was too large")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=65_536):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_HTTP_BYTES:
            raise IngestError("response was too large")
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


def _safe_error_reason(response: requests.Response) -> str:
    try:
        raw = _read_limited(response)
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, IngestError, ValueError):
        return ""
    if isinstance(payload, dict) and payload.get("reason"):
        return public_text(payload["reason"], limit=120)
    return ""


def _user_agent(app_name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._+-]+", "-", app_name).strip("-") or "weather-etl"
    return f"{token}/{__version__}"
