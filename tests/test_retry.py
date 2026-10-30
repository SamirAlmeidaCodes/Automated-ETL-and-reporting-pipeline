"""Retry budget and exponential backoff, without sleeping for real."""

import pytest

from etl.retry import RetryableFetchError, call_with_retry


def test_retries_then_returns_the_successful_call() -> None:
    calls = {"count": 0}
    delays: list[float] = []

    def flaky() -> str:
        calls["count"] += 1
        if calls["count"] < 3:
            raise RetryableFetchError("temporary")
        return "ok"

    result = call_with_retry(
        flaky,
        attempts=3,
        backoff_seconds=0.5,
        retry_on=(RetryableFetchError,),
        sleep=delays.append,
        operation="fetch",
    )
    assert result == "ok"
    assert calls["count"] == 3
    assert delays == [0.5, 1.0]


def test_exhausted_attempts_raise_the_last_error() -> None:
    delays: list[float] = []

    def always_fail() -> None:
        raise RetryableFetchError("down")

    with pytest.raises(RetryableFetchError, match="down"):
        call_with_retry(
            always_fail,
            attempts=2,
            backoff_seconds=1,
            retry_on=(RetryableFetchError,),
            sleep=delays.append,
            operation="fetch",
        )
    assert delays == [1.0]


def test_unexpected_errors_are_not_retried() -> None:
    calls = {"count": 0}

    def boom() -> None:
        calls["count"] += 1
        raise ValueError("bad request")

    with pytest.raises(ValueError, match="bad request"):
        call_with_retry(
            boom,
            attempts=4,
            backoff_seconds=1,
            retry_on=(RetryableFetchError,),
            sleep=lambda _delay: None,
            operation="fetch",
        )
    assert calls["count"] == 1


def test_backoff_is_capped() -> None:
    delays: list[float] = []

    def always_fail() -> None:
        raise RetryableFetchError("down")

    with pytest.raises(RetryableFetchError):
        call_with_retry(
            always_fail,
            attempts=6,
            backoff_seconds=20,
            retry_on=(RetryableFetchError,),
            sleep=delays.append,
            operation="fetch",
        )
    assert delays == [20.0, 30.0, 30.0, 30.0, 30.0]
