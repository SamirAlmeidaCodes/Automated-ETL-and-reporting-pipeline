"""Liveness file used by the Docker healthcheck."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path


def write_heartbeat(path: Path) -> None:
    """Write the liveness stamp. A symlink is refused so the write cannot be redirected."""
    if path.is_symlink():
        raise OSError("heartbeat path must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o640)
    try:
        os.write(descriptor, (stamp + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)
