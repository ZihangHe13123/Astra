"""Opaque identity that distinguishes equal session names in different namespaces."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def session_key(path: str | Path | None) -> str:
    if not path:
        return ""
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode()).hexdigest()
