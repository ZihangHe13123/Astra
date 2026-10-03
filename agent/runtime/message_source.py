"""Read-only content identity for canonical message inspection and navigation."""
from __future__ import annotations

import hashlib
import json


def message_source_ref(message: dict, index: int) -> dict:
    """A content-verified canonical position shared by runtime and GUI readers."""
    digest = hashlib.sha256()
    for chunk in json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":")).iterencode(message):
        for offset in range(0, len(chunk), 64 * 1024):
            digest.update(chunk[offset:offset + 64 * 1024].encode("utf-8"))
    return {"index": index, "digest": digest.hexdigest()}
