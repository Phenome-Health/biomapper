"""Private on-disk state shared by jobs, references, and judgments.

Everything the server keeps under its home is owner-only: directories ``0700``, files ``0600``,
and every file is replaced atomically (temp file, fsync, ``os.replace``) so a crash never leaves a
torn record.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from pathlib import Path


def utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat()


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)


def write_private(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, readable by the owner only."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
