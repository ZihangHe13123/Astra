"""Small read-only SQLite connection primitive for context-index sources."""

import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

from .models import RetrievalStage


def database_error_category(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "deadline"
    if isinstance(exc, sqlite3.DatabaseError):
        return "deadline" if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT or "interrupt" in str(exc).lower() else "database_error"
    return "source_error"


@contextmanager
def observe_stage(stages: list[RetrievalStage], name: str) -> Iterator[None]:
    """Record fixed stage names and categories, never exception messages."""
    started = time.perf_counter()
    error = ""
    try:
        yield
    except Exception as exc:
        error = database_error_category(exc)
        raise
    finally:
        stages.append(RetrievalStage(name, max(0.0, (time.perf_counter() - started) * 1000), error))


class ReadBudget:
    """Reserve part of one read deadline for later independent channels."""

    def __init__(self, connection: sqlite3.Connection, deadline_ms: int):
        self.connection = connection
        # Python 3.11's Windows monotonic clock advances in ~15.6 ms ticks;
        # perf_counter is also monotonic and preserves these short budgets.
        self.deadline = time.perf_counter() + max(0, deadline_ms) / 1000
        self.stages: list[RetrievalStage] = []
        self._set_deadline(self.deadline)

    def _set_deadline(self, deadline: float) -> None:
        self.connection.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 1000)

    @contextmanager
    def pause_for_compute(self) -> Iterator[None]:
        """No SQL in this block; retain unused read time during bounded encode wait."""
        remaining = max(0.0, self.deadline - time.perf_counter())
        try:
            yield
        finally:
            self.deadline = time.perf_counter() + remaining
            self._set_deadline(self.deadline)

    @contextmanager
    def stage(self, name: str, share: float = 1.0) -> Iterator[None]:
        started = time.perf_counter()
        self._set_deadline(min(self.deadline, started + max(0, self.deadline - started) * share))
        try:
            with observe_stage(self.stages, name):
                if started >= self.deadline:
                    raise sqlite3.OperationalError("interrupted")
                yield
        finally:
            # A stage timeout is local. Unused/reserved time remains usable,
            # but no phase can extend the original total source deadline.
            self._set_deadline(self.deadline)


def set_read_window(
    connection: sqlite3.Connection, cutoff: float = float("inf"), start: float = 0.0,
    end: float = 0.0, *, message_id: int | None = None,
) -> None:
    """Connection-local bounds, applied in SQL before candidate-pool LIMITs."""
    before = min(cutoff, end - 0.000001) if end else cutoff
    connection.create_function("context_index_before", 0, lambda: before, deterministic=True)
    connection.create_function("context_index_after", 0, lambda: start, deterministic=True)
    connection.create_function("context_index_cutoff", 0, lambda: cutoff, deterministic=True)
    connection.create_function("context_index_query_id", 0, lambda: message_id or 0, deterministic=True)


@contextmanager
def open_readonly(
    path: Path, deadline_ms: int = 75, *, cancelled: Callable[[], bool] | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open an existing SQLite database without creating or mutating it."""
    database_path = Path(path)
    if not database_path.is_file():
        raise FileNotFoundError(database_path)

    uri = f"file:{quote(str(database_path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.05)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        # Large text archives and vector snapshots should not copy every page
        # through SQLite's small per-connection cache. Bound the read-only map
        # to 128 MiB; SQLite preserves WAL visibility and falls back to normal
        # reads beyond this limit or on platforms without mmap support.
        connection.execute("PRAGMA mmap_size=134217728")
        set_read_window(connection)
        deadline = time.perf_counter() + max(0, deadline_ms) / 1000
        connection.set_progress_handler(
            lambda: int((cancelled is not None and cancelled()) or time.perf_counter() >= deadline),
            1000,
        )
        try:
            yield connection
        finally:
            connection.set_progress_handler(None, 0)
    finally:
        connection.close()
