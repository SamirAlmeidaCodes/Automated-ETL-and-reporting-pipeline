"""Fetch hourly weather from the Open-Meteo forecast API."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import pandas as pd
import requests

from etl import __version__
from etl.config import ApiConfig, LocationConfig
from etl.errors import IngestError
from etl.retry import RetryableFetchError, call_with_retry

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
            response = session.get(api.base_url, params=params, timeout=api.timeout_seconds)
        except (requests.Timeout, requests.ConnectionError) as exc:
            raise RetryableFetchError(f"{location.name}: {exc}") from exc
        if response.status_code in RETRYABLE_STATUS:
            raise RetryableFetchError(f"{location.name}: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise IngestError(
                f"Open-Meteo rejected {location.name} (HTTP {response.status_code}): {_error_text(response)}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RetryableFetchError(f"{location.name}: response was not JSON") from exc
        if isinstance(payload, dict) and payload.get("error"):
            reason = str(payload.get("reason") or "unknown error")
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
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict) or "time" not in hourly:
        raise IngestError(f"Open-Meteo response for {location_name} is missing hourly.time")
    times = hourly["time"]
    if not isinstance(times, list):
        raise IngestError(f"Open-Meteo hourly.time for {location_name} was not a list")
    data: dict[str, object] = {
        "location": location_name,
        "timestamp": times,
        "source": "api",
    }
    for variable in variables:
        values = hourly.get(variable)
        if not isinstance(values, list):
            raise IngestError(f"Open-Meteo response for {location_name} is missing hourly.{variable}")
        if len(values) != len(times):
            raise IngestError(
                f"Open-Meteo hourly.{variable} for {location_name} has {len(values)} values for {len(times)} times"
            )
        data[variable] = values
    return pd.DataFrame(data)


def _error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, dict) and payload.get("reason"):
        return str(payload["reason"])[:300]
    return response.text[:300]


def _user_agent(app_name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._+-]+", "-", app_name).strip("-") or "weather-etl"
    return f"{token}/{__version__}"
