"""Keep secrets and tracebacks out of log records."""

from __future__ import annotations

import logging
import re
import threading

_SECRET_MIN_LENGTH = 8
_secrets: set[str] = set()
_lock = threading.Lock()

# Assignment-style secrets from libraries or a misconfigured format string.
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|smtp_password)"
    r"(\s*[:=]\s*)(\S+)"
)
_BEARER = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*")


def register_secret(value: str | None) -> None:
    """Remember a secret so later log lines can be scrubbed.

    Short values are ignored so a placeholder cannot blot out ordinary words.
    """
    if not value:
        return
    secret = value.strip()
    if len(secret) < _SECRET_MIN_LENGTH:
        return
    with _lock:
        if len(_secrets) >= 32 and secret not in _secrets:
            return
        _secrets.add(secret)


def known_secrets() -> tuple[str, ...]:
    with _lock:
        return tuple(sorted(_secrets, key=len, reverse=True))


def redact_text(value: str) -> str:
    """Replace known secrets and common credential assignments."""
    text = value
    for secret in known_secrets():
        if secret in text:
            text = text.replace(secret, "[redacted]")
    text = _ASSIGNMENT.sub(r"\1\2[redacted]", text)
    text = _BEARER.sub(r"\1[redacted]", text)
    return text


def public_text(value: object, *, limit: int = 160) -> str:
    """A single-line fragment safe to put in an error message."""
    text = "".join(character for character in str(value) if character.isprintable()).strip()
    text = redact_text(text)
    if len(text) > limit:
        return text[:limit]
    return text


class RedactingFilter(logging.Filter):
    """Drop exception tracebacks and scrub secrets before a record is emitted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = "Log record omitted"
        record.msg = redact_text(message)
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True
