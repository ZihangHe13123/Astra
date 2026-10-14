"""Incremental session/record embedding maintenance, outside the reply path."""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import threading
from pathlib import Path
from collections.abc import Callable

from ..instance_lock import InstanceAlreadyRunning, InstanceLock
from . import embedder
from .semantic_index import file_version, rebuild_source, warm_snapshot
from .vector_index import default_vectors_db_path

_log = logging.getLogger(__name__)
_stop = threading.Event()
_threads: dict[tuple[str, ...], threading.Thread] = {}
_registry_lock = threading.Lock()
atexit.register(_stop.set)


def start_background(session_path: Path, memory_path: Path, vectors_path: Path,
                     *, enabled: Callable[[], bool] = lambda: True) -> threading.Thread | None:
    """Maintain already-enabled indexes; never bootstrap or download a model."""
    if not embedder._enabled() or not vectors_path.is_file():
        return None
    key = tuple(str(path.resolve()) for path in (session_path, memory_path, vectors_path))
    with _registry_lock:
        previous = _threads.get(key)
        if previous is not None and previous.is_alive():
            return previous
        if sum(thread.is_alive() for thread in _threads.values()) >= 4:
            return None

        def work() -> None:
            versions: dict[str, tuple] = {}
            owner = InstanceLock(vectors_path.with_suffix(vectors_path.suffix + ".memory-index.lock"))

            def cancelled() -> bool:
                return (_stop.is_set() or not enabled() or not embedder._enabled()
                        or not vectors_path.is_file() or embedder.ready_embedder() is None)

            while not _stop.is_set():
                backend = embedder.ready_embedder()
                if backend is not None and not cancelled():
                    try:
                        owner.acquire()
                    except InstanceAlreadyRunning:
                        pass
                    else:
                        try:
                            for source, path in (("memory", memory_path), ("session", session_path)):
                                if not path.is_file() or cancelled():
                                    continue
                                version = file_version(path)
                                if versions.get(source) == version:
                                    continue
                                try:
                                    report = rebuild_source(path, vectors_path, source, backend=backend, max_encode=16)
                                    if not report["pending"]:
                                        versions[source] = version
                                except Exception as exc:
                                    _log.warning("Memory vector maintenance deferred (%s)", type(exc).__name__)
                        finally:
                            owner.release()
                    # Every process needs its own read cache, including when
                    # another process owns writing or canonical inputs did not
                    # change. Keep the bounded fill outside the writer lease.
                    for source, path in (("memory", memory_path), ("session", session_path)):
                        if cancelled():
                            break
                        if not path.is_file():
                            continue
                        try:
                            warm_snapshot(vectors_path, source, path, cancelled=cancelled)
                        except Exception as exc:
                            if not cancelled():
                                _log.warning("Memory vector snapshot deferred (%s)", type(exc).__name__)
                if _stop.wait(30):
                    return

        thread = threading.Thread(target=work, name="astra-memory-index", daemon=True)
        _threads[key] = thread
        thread.start()
        return thread


def main(argv: list[str] | None = None) -> int:
    from agent.cli.environment import load_project_env
    from agent.runtime.memory import default_memory_path
    from .factory import create_context_index_broker
    from agent.cli.context_index_preferences import load_context_index_preferences

    root = Path(__file__).resolve().parents[3]
    load_project_env(root)
    broker = create_context_index_broker(load_context_index_preferences(), root)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-db", type=Path, default=broker.semantic_reader.session_path if broker.semantic_reader else None)
    parser.add_argument("--memory-db", type=Path, default=default_memory_path())
    parser.add_argument("--vectors-db", type=Path, default=default_vectors_db_path())
    parser.add_argument("--source", choices=("session", "memory", "all"), default="all")
    parser.add_argument("--max-encode", type=int, default=128, help="per-source encoding cap; rerun to continue")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.max_encode <= 5000:
        parser.error("--max-encode must be between 1 and 5000")
    report = {}
    for source, path in (("session", args.sessions_db), ("memory", args.memory_db)):
        if args.source not in ("all", source):
            continue
        if path is None or not path.is_file():
            report[source] = {"state": "absent"}
            continue
        try:
            report[source] = {"state": "ready", **rebuild_source(
                path, args.vectors_db, source, max_encode=args.max_encode, force=args.rebuild,
            )}
        except Exception as exc:
            report[source] = {"state": "deferred", "error": type(exc).__name__}
    print(json.dumps(report, sort_keys=True))
    return int(any(value["state"] == "deferred" for value in report.values()))


if __name__ == "__main__":
    raise SystemExit(main())
