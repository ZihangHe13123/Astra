"""Fixed, isolated snapshot diagnostics; never changes production acceptance budgets."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import gc
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import struct
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=6)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 20:
        parser.error("rounds must be between 1 and 20")
    if args.output.exists():
        parser.error("use a fresh output path")
    args.output.mkdir(parents=True)
    folder = Path(tempfile.mkdtemp(prefix="astra-vector-snapshot-probe-"))
    os.environ["ASTRA_HOME"] = str(folder / "home")
    os.environ["ASTRA_EMBEDDING_BACKEND"] = "mlx"
    import numpy as np
    from agent.runtime.context_index import semantic_index, vector_index

    counts, dim = {"session": 5000, "memory": 32, "activity": 1200}, 2560
    path = folder / "vectors.db"
    archives = {source: folder / f"{source}.db" for source in ("session", "memory")}
    blob = struct.pack(f"<{dim}f", *([0.5] * dim))
    store = semantic_index.EvidenceVectorStore(path)
    for source, archive in archives.items():
        store._connection.executemany(
            "INSERT INTO context_memory_vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ((source, semantic_index.archive_key(archive), str(index), "revision", semantic_index._LAYOUT,
              float(index), float(index), blob) for index in range(counts[source])),
        )
    store._connection.executemany(
        "INSERT INTO context_index_vectors(summary_id, content_hash, dim, vec) VALUES (?, ?, ?, ?)",
        ((str(index), "revision", dim, blob) for index in range(counts["activity"])),
    )
    store._connection.commit()
    store.close()
    query = ("SELECT item_id, revision, timestamp, available_at, vec FROM context_memory_vectors "
             "WHERE source=? AND archive=? AND layout=? ORDER BY timestamp DESC, item_id LIMIT ?")
    parameters = ("session", semantic_index.archive_key(archives["session"]), semantic_index._LAYOUT, 5000)
    original_connect = sqlite3.connect
    original_open = semantic_index.open_readonly
    original_matrix = vector_index.matrix_from_blobs
    original_version = semantic_index.file_version
    original_archive = semantic_index.archive_key
    with original_open(path, deadline_ms=2000) as db:
        mmap_row = db.execute("PRAGMA mmap_size").fetchone()
        metadata = {
            "sqlite_version": sqlite3.sqlite_version,
            "compile_options": [row[0] for row in db.execute("PRAGMA compile_options")],
            "mmap_size_after_production_setup": mmap_row[0] if mmap_row is not None else None,
            "page_size": db.execute("PRAGMA page_size").fetchone()[0],
            "query_plan": [list(row) for row in db.execute("EXPLAIN QUERY PLAN " + query, parameters)],
            "database_bytes": path.stat().st_size,
        }
    output = {"platform": platform.platform(), "python": sys.version, "numpy": np.__version__,
              "fixture": str(folder), "counts": counts, "dim": dim,
              "payload_bytes": {source: count * dim * 4 for source, count in counts.items()},
              "metadata": metadata, "rounds": args.rounds, "warmup_rounds": 2, "experiments": [],
              "interpretation": "Original modes use the unchanged 75 ms reader. The 2000 ms diagnostic modes only locate work truncated by that budget; they are not acceptance passes. Instrumented modes have observer overhead and are kept separate from original modes."}
    local = threading.local()

    def save() -> None:
        (args.output / "results.json").write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")

    def event(name: str, started: float, **details) -> None:
        row = getattr(local, "row", None)
        if row is not None:
            row["phases"].append({"name": name, "ms": (time.perf_counter() - started) * 1000, **details})

    class Cursor(sqlite3.Cursor):
        def execute(self, sql, parameters=()):
            self.label = "vectors" if " vec " in sql or ", vec FROM" in sql else "metadata"
            started = time.perf_counter()
            try:
                return super().execute(sql, parameters)
            finally:
                event("sql_execute_" + self.label, started)

        def fetchall(self):
            started = time.perf_counter()
            try:
                rows = super().fetchall()
                event("sql_fetchall_" + self.label, started, rows=len(rows))
                return rows
            except Exception:
                event("sql_fetchall_" + self.label, started, interrupted=True)
                raise

        def fetchone(self):
            started = time.perf_counter()
            try:
                return super().fetchone()
            finally:
                event("sql_fetchone_" + self.label, started)

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            return self.cursor(factory=Cursor).execute(sql, parameters)

        def close(self):
            started = time.perf_counter()
            try:
                return super().close()
            finally:
                event("sql_close", started)

    def connect(*values, **kwargs):
        kwargs["factory"] = Connection
        started = time.perf_counter()
        try:
            return original_connect(*values, **kwargs)
        finally:
            event("sql_connect", started)

    def matrix(blobs):
        # Same binary operations as production, separated for attribution.
        if not blobs:
            return np.zeros((0, 0), dtype=np.float32)
        started = time.perf_counter()
        dimensions = {len(value) for value in blobs}
        if len(dimensions) != 1 or any(size == 0 or size % 4 for size in dimensions):
            raise ValueError("Invalid vector dimensions")
        event("matrix_validate_shape", started)
        started = time.perf_counter()
        buffer = b"".join(blobs)
        event("matrix_join_copy", started, bytes=len(buffer))
        started = time.perf_counter()
        result = np.frombuffer(buffer, dtype="<f4").reshape(len(blobs), -1)
        event("matrix_view", started)
        started = time.perf_counter()
        finite = bool(np.all(np.isfinite(result)))
        event("matrix_finite_check", started)
        if not finite:
            raise ValueError("Nonfinite vector snapshot")
        return result

    def version(value):
        started = time.perf_counter()
        try:
            return original_version(value)
        finally:
            event("file_version", started)

    def archive(value):
        started = time.perf_counter()
        try:
            return original_archive(value)
        finally:
            event("archive_key_and_resolve", started)

    @contextmanager
    def diagnostic_open(value, deadline_ms=75):
        with original_open(value, deadline_ms=2000) as db:
            yield db

    def read(source, barrier, profiled):
        row = {"source": source, "phases": []}
        local.row = row if profiled else None
        if barrier is not None:
            barrier.wait()
        started = time.perf_counter()
        try:
            if source == "activity":
                rows, values, _revisions = vector_index.read_vector_snapshot(path)
            else:
                rows, values = semantic_index.read_snapshot(path, source, archives[source])
            assert len(rows) == counts[source] and values.shape == (counts[source], dim)
            row.update(status="complete", rows=len(rows), shape=list(values.shape))
        except sqlite3.OperationalError as error:
            row.update(status="deadline" if "interrupt" in str(error) else "sqlite_error", error=str(error))
        finally:
            row["elapsed_ms"] = (time.perf_counter() - started) * 1000
            local.row = None
        return row

    modes = [("original_serial_75ms", False, False, False), ("original_parallel_75ms", True, False, False),
             ("profile_serial_75ms", False, True, False), ("profile_parallel_75ms", True, True, False),
             ("diagnostic_serial_2000ms", False, True, True), ("diagnostic_parallel_2000ms", True, True, True)]
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            for name, parallel, profiled, diagnostic in modes:
                sqlite3.connect = connect if profiled else original_connect
                vector_index.matrix_from_blobs = matrix if profiled else original_matrix
                semantic_index.matrix_from_blobs = matrix if profiled else original_matrix
                semantic_index.file_version = version if profiled else original_version
                semantic_index.archive_key = archive if profiled else original_archive
                semantic_index.open_readonly = diagnostic_open if diagnostic else original_open
                experiment = {"name": name, "parallel": parallel, "instrumented": profiled,
                              "diagnostic_only": diagnostic, "samples": []}
                output["experiments"].append(experiment)
                for index in range(args.rounds + 2):
                    with semantic_index._cache_lock:
                        semantic_index._cache.clear()
                    gc.collect()
                    if parallel:
                        barrier = threading.Barrier(3)
                        futures = [pool.submit(read, source, barrier, profiled) for source in counts]
                        rows = [future.result(timeout=10) for future in futures]
                    else:
                        rows = [read(source, None, profiled) for source in counts]
                    experiment["samples"].append({"round": index, "warmup": index < 2, "sources": rows})
                    save()
                summary = {}
                for source in counts:
                    rows = [r for sample in experiment["samples"] if not sample["warmup"] for r in sample["sources"] if r["source"] == source]
                    times = sorted(r["elapsed_ms"] for r in rows)
                    summary[source] = {"complete": sum(r["status"] == "complete" for r in rows),
                                       "deadline": sum(r["status"] == "deadline" for r in rows),
                                       "p95_elapsed_ms": times[math.ceil(len(times) * .95) - 1]}
                experiment["summary"] = summary
                print(json.dumps({"mode": name, "summary": summary}), flush=True)
                save()
    finally:
        sqlite3.connect = original_connect
        vector_index.matrix_from_blobs = original_matrix
        semantic_index.matrix_from_blobs = original_matrix
        semantic_index.open_readonly = original_open
        semantic_index.file_version = original_version
        semantic_index.archive_key = original_archive
    print(args.output / "results.json", flush=True)


if __name__ == "__main__":
    main()
