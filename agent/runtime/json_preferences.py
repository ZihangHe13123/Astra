"""Cross-process read/modify/write for the small shared preferences document."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

from .instance_lock import InstanceAlreadyRunning, InstanceLock


def update_preferences(path: Path, change: Callable[[dict], None]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(path.with_suffix(path.suffix + ".lock"))
    deadline = time.monotonic() + 5
    while True:
        try:
            lock.acquire()
            break
        except InstanceAlreadyRunning as exc:
            if time.monotonic() >= deadline:
                raise OSError("Settings are being updated by another Astra instance; try again.") from exc
            time.sleep(0.02)
    temporary = ""
    try:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            value = {}
        data = value if isinstance(value, dict) else {}
        change(data)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return path
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
        lock.release()
