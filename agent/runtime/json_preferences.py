"""Cross-process read/modify/write for the small shared preferences document."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable

from .instance_lock import InstanceAlreadyRunning, InstanceLock


class UnreadablePreferences(OSError):
    """The file exists but does not hold a JSON object, so a change written over it would lose what it holds."""


def update_preferences(path: Path, change: Callable[[dict], None], *, keep_unreadable: bool = False) -> Path:
    """Apply ``change`` to the JSON object in ``path`` under a cross-process lock, and write it back atomically.

    A missing file starts as {}. One that is there but does not hold a JSON object (empty, or a syntax error
    while someone edits it) is replaced too, unless ``keep_unreadable``: then nothing is written and
    UnreadablePreferences says so.
    """
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
        value: object
        try:
            text = path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            value = {}
        except (OSError, ValueError):
            value = None
        else:
            try:
                value = json.loads(text) if text.strip() else (None if keep_unreadable else {})
            except ValueError:
                value = None
        if not isinstance(value, dict):
            if keep_unreadable:
                raise UnreadablePreferences(f"{path.name} is not a JSON object; it was left as it is")
            value = {}
        data = value
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
