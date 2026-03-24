"""SHA-256 file hashing for change detection."""

from __future__ import annotations

import hashlib
from pathlib import Path

_BUF_SIZE = 65536  # 64 KB chunks


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_BUF_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
