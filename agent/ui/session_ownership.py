"""A shared kernel lease while any local context owns a writable session."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import unicodedata
import weakref
from pathlib import Path

from agent.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock

_leases: weakref.WeakValueDictionary[str, SessionLease] = weakref.WeakValueDictionary()
_guard = threading.RLock()


class SessionLease:
    def __init__(self, path: Path, key: str):
        digest = hashlib.sha256(key.encode()).hexdigest()
        lock = InstanceLock(path.parent / ".writers" / f"{digest}.lock")
        try:
            lock.acquire()
        except InstanceAlreadyRunning as exc:
            raise OSError(f"Session '{path.stem}' is in use by another Astra instance. "
                               "Close that session there, or view its history read-only.") from exc
        # Context copies share this object. A staged candidate cannot release
        # the live context's lease; the kernel releases it on process death.
        self._finalizer = weakref.finalize(self, lock.release)


def claim_session(path: str | Path) -> SessionLease:
    canonical = Path(path).expanduser().resolve().with_suffix(".json")
    key = unicodedata.normalize("NFC", os.path.normcase(str(canonical)))
    if sys.platform == "darwin":
        key = key.casefold()
    with _guard:
        lease = _leases.get(key)
        if lease is None:
            lease = SessionLease(canonical, key)
            _leases[key] = lease
        return lease
