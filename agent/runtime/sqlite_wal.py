"""Journal initialization shared by stores accessed by more than one backend."""

import sqlite3
import time


def enable_wal(db: sqlite3.Connection) -> None:
    """Honor the connection's busy timeout when SQLite skips it for a journal upgrade."""
    started = time.monotonic()
    try:
        db.execute("PRAGMA journal_mode=WAL").close()
        return
    except sqlite3.OperationalError as exc:
        if (getattr(exc, "sqlite_errorcode", 0) & 0xff) != sqlite3.SQLITE_BUSY:
            raise
        timeout_ms = int(db.execute("PRAGMA busy_timeout").fetchone()[0])
        deadline = started + timeout_ms / 1000
        if time.monotonic() >= deadline:
            raise
    db.execute("PRAGMA busy_timeout=0")
    try:
        while True:
            try:
                db.execute("PRAGMA journal_mode=WAL").close()
                return
            except sqlite3.OperationalError as exc:
                remaining = deadline - time.monotonic()
                if (getattr(exc, "sqlite_errorcode", 0) & 0xff) != sqlite3.SQLITE_BUSY or remaining <= 0:
                    raise
                time.sleep(min(0.025, remaining))
    finally:
        db.execute(f"PRAGMA busy_timeout={timeout_ms}")
