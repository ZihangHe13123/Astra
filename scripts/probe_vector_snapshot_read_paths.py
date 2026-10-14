"""Fixed, isolated SQLite read-path attribution; no production budget changes."""
from __future__ import annotations

import argparse
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
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("use a fresh output path")
    args.output.mkdir(parents=True)
    folder = Path(tempfile.mkdtemp(prefix="astra-vector-read-paths-"))
    os.environ["ASTRA_HOME"] = str(folder / "home")
    os.environ["ASTRA_EMBEDDING_BACKEND"] = "mlx"
    import numpy as np
    from agent.runtime.context_index import semantic_index, vector_index

    counts, dim, measured, warmups, budget_ms = {"session": 5000, "memory": 32, "activity": 1200}, 2560, 20, 2, 75
    path = folder / "vectors.db"
    archives = {source: folder / f"{source}.db" for source in ("session", "memory")}
    tail = struct.pack(f"<{dim - 1}f", *([0.5] * (dim - 1)))
    store = semantic_index.EvidenceVectorStore(path)
    for source, archive in archives.items():
        store._connection.executemany(
            "INSERT INTO context_memory_vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ((source, semantic_index.archive_key(archive), str(index), "revision", semantic_index._LAYOUT,
              float(index), float(index), struct.pack("<f", index) + tail) for index in range(counts[source])),
        )
    store._connection.executemany(
        "INSERT INTO context_index_vectors(summary_id, content_hash, dim, vec) VALUES (?, ?, ?, ?)",
        ((str(index), "revision", dim, struct.pack("<f", index) + tail) for index in range(counts["activity"])),
    )
    store._connection.commit()
    store.close()
    identity = semantic_index.embedder.embedding_identity()
    selection = " WHERE source=? AND archive=? AND layout=? ORDER BY timestamp DESC, item_id LIMIT ?"
    params = ("session", semantic_index.archive_key(archives["session"]), semantic_index._LAYOUT, counts["session"])
    full_query = "SELECT item_id, revision, timestamp, available_at, vec FROM context_memory_vectors" + selection
    selected_query = "SELECT rowid, item_id, revision, timestamp, available_at FROM context_memory_vectors" + selection
    expected = tuple((str(i), "revision", float(i), float(i)) for i in reversed(range(counts["session"])))
    variants = ("production", "fresh_row", "fresh_tuple", "selected_logical", "selected_rowid", "blobopen_rowid", "reused_row")
    output = {
        "platform": platform.platform(), "python": sys.version, "numpy": np.__version__,
        "fixture": str(folder), "counts": counts, "dim": dim, "source": "session",
        "measured_rounds": measured, "warmup_rounds": warmups, "sql_budget_ms": budget_ms,
        "payload_bytes": counts["session"] * dim * 4,
        "interpretation": [
            "Application snapshot cache is cleared; OS page cache is not evicted.",
            "Serial read-path attribution only; this is not concurrent broker acceptance.",
            "Production uses the real reader unchanged. Replicas use its readonly connection setup plus an explicit read transaction and strict final deadline check.",
            "All replica SQL and incremental BLOB reads share one 75 ms perf_counter deadline, matching the precise production clock. Historical 6028e46f results used monotonic and remain separate evidence. Connection setup/close and binary matrix decode are outside that deadline, matching the production SQL budget boundary.",
            "selected_logical and selected_rowid first select identical metadata, then fetch the identical rowid set; only the BLOB query order changes. blobopen_rowid uses the same rowid order with incremental readonly reads.",
            "reused_row releases its transaction and resets its deadline each round. It retains both mmap and the SQLite pager cache, so it does not isolate mmap alone or demonstrate first-ever load performance.",
            "First float identifies each item; all other values are 0.5. Every completed result is checked outside timers for exact metadata, logical order, and all vector values.",
            "Each round rotates variant order. Deadline samples remain failures regardless of their elapsed time; p95_all_ms is not a pass criterion.",
        ],
        "variants": {name: {"samples": []} for name in variants},
    }
    with semantic_index.open_readonly(path, deadline_ms=2000) as db:
        selected_ids = [row[0] for row in db.execute(selected_query, params)]
        payload_query = ("SELECT rowid, vec FROM context_memory_vectors WHERE rowid IN ("
                         + ",".join("?" for _ in selected_ids) + ") ORDER BY ")
        output["metadata"] = {
            "sqlite_version": sqlite3.sqlite_version,
            "compile_options": [row[0] for row in db.execute("PRAGMA compile_options")],
            "mmap_size": db.execute("PRAGMA mmap_size").fetchone()[0],
            "page_size": db.execute("PRAGMA page_size").fetchone()[0],
            "database_bytes": path.stat().st_size,
            "production_query_plan": [list(row) for row in db.execute("EXPLAIN QUERY PLAN " + full_query, params)],
            "selection_query_plan": [list(row) for row in db.execute("EXPLAIN QUERY PLAN " + selected_query, params)],
            "payload_query_plans": {
                ordering: [list(row) for row in db.execute("EXPLAIN QUERY PLAN " + payload_query + ordering, selected_ids)]
                for ordering in ("timestamp DESC, item_id", "rowid")
            },
            "blobopen_available": hasattr(db, "blobopen"),
        }

    def save() -> None:
        (args.output / "results.json").write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")

    @contextmanager
    def phase(sample, name):
        start = time.perf_counter()
        try:
            yield
        finally:
            sample["phases_ms"][name] = (time.perf_counter() - start) * 1000

    def check_result(result) -> None:
        metadata, matrix = result
        assert metadata == expected, "metadata/order differs from the production selected set"
        assert matrix.shape == (counts["session"], dim)
        np.testing.assert_array_equal(matrix[:, 0], np.arange(counts["session"] - 1, -1, -1, dtype=np.float32))
        assert bool(np.all(matrix[:, 1:] == np.float32(0.5))), "vector/item association differs"

    def replica(name, sample, reused):
        context = None
        db = reused
        rows = None
        blobs = None
        metadata = None
        try:
            if db is None:
                with phase(sample, "connection_setup"):
                    context = semantic_index.open_readonly(path, deadline_ms=budget_ms)
                    db = context.__enter__()
            db.row_factory = None if name == "fresh_tuple" else sqlite3.Row
            deadline = time.perf_counter() + budget_ms / 1000
            db.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 1000)

            def check_deadline():
                if time.perf_counter() >= deadline:
                    raise TimeoutError("75 ms read deadline")

            with phase(sample, "sql_read_total"):
                with phase(sample, "transaction_identity"):
                    db.execute("BEGIN")
                    model = db.execute("SELECT identity FROM embedding_metadata LIMIT 1").fetchone()
                    if model is None or model[0] != identity:
                        raise ValueError("Vector model identity mismatch")
                if name in ("fresh_row", "fresh_tuple", "reused_row"):
                    with phase(sample, "select_execute"):
                        cursor = db.execute(full_query, params)
                    with phase(sample, "select_fetchall"):
                        rows = cursor.fetchall()
                else:
                    with phase(sample, "metadata_select"):
                        selected = db.execute(selected_query, params).fetchall()
                    ids = [row[0] for row in selected]
                    sample["selected_rows"] = len(ids)
                    if name == "blobopen_rowid":
                        payloads = {}
                        with phase(sample, "incremental_blob_reads"):
                            for rowid in sorted(ids):
                                check_deadline()
                                with db.blobopen("context_memory_vectors", "vec", rowid, readonly=True) as blob:
                                    payloads[rowid] = blob.read()
                                # Incremental BLOB reads do not necessarily invoke the SQL progress handler.
                                check_deadline()
                    else:
                        ordering = "rowid" if name == "selected_rowid" else "timestamp DESC, item_id"
                        query = ("SELECT rowid, vec FROM context_memory_vectors WHERE rowid IN ("
                                 + ",".join("?" for _ in ids) + ") ORDER BY " + ordering)
                        with phase(sample, "blob_select_execute"):
                            cursor = db.execute(query, ids)
                        with phase(sample, "blob_select_fetchall"):
                            payload_rows = cursor.fetchall()
                        payloads = {row[0]: row[1] for row in payload_rows}
                    check_deadline()
                check_deadline()
        finally:
            if db is not None:
                db.set_progress_handler(None, 0)
                with phase(sample, "transaction_release"):
                    if db.in_transaction:
                        db.rollback()
                if context is not None:
                    with phase(sample, "connection_close"):
                        context.__exit__(None, None, None)
        with phase(sample, "restore_and_metadata"):
            if rows is not None:
                metadata = tuple((str(row[0]), str(row[1]), float(row[2]), float(row[3])) for row in rows)
                blobs = [row[4] for row in rows]
            else:
                metadata = tuple((str(row[1]), str(row[2]), float(row[3]), float(row[4])) for row in selected)
                blobs = [payloads[row[0]] for row in selected]
        with phase(sample, "matrix_decode"):
            if any(len(blob) > 65536 for blob in blobs):
                raise ValueError("Invalid vector dimensions")
            matrix = vector_index.matrix_from_blobs(blobs)
            matrix.flags.writeable = False
        return metadata, matrix

    # Reuse only the connection: never retain a read transaction across samples.
    with semantic_index.open_readonly(path, deadline_ms=2000) as reused:
        reused.set_progress_handler(None, 0)
        for index in range(measured + warmups):
            offset = index % len(variants)
            order = variants[offset:] + variants[:offset]
            for position, name in enumerate(order):
                with semantic_index._cache_lock:
                    semantic_index._cache.clear()
                gc.collect()
                sample = {"round": index, "warmup": index < warmups, "position": position, "phases_ms": {}}
                start = time.perf_counter()
                result = None
                try:
                    if name == "production":
                        result = semantic_index.read_snapshot(path, "session", archives["session"])
                    elif name == "blobopen_rowid" and not output["metadata"]["blobopen_available"]:
                        sample["status"] = "unsupported"
                    else:
                        result = replica(name, sample, reused if name == "reused_row" else None)
                    if result is not None:
                        sample["status"] = "complete"
                except (sqlite3.Error, TimeoutError) as error:
                    sample["status"] = "deadline" if isinstance(error, TimeoutError) or "interrupt" in str(error).lower() else "sqlite_error"
                    sample["error"] = str(error)
                finally:
                    sample["elapsed_ms"] = (time.perf_counter() - start) * 1000
                if result is not None:
                    check_result(result)
                    sample["verified_rows"] = len(result[0])
                del result
                output["variants"][name]["samples"].append(sample)
                save()
    for name, data in output["variants"].items():
        samples = [sample for sample in data["samples"] if not sample["warmup"]]
        times = sorted(sample["elapsed_ms"] for sample in samples)
        complete = sorted(sample["elapsed_ms"] for sample in samples if sample["status"] == "complete")
        phases = sorted({phase for sample in samples for phase in sample["phases_ms"]})
        data["summary"] = {
            "complete": len(complete), "deadline": sum(sample["status"] == "deadline" for sample in samples),
            "other_statuses": sorted({sample["status"] for sample in samples} - {"complete", "deadline"}),
            "p95_all_ms": times[math.ceil(len(times) * .95) - 1],
            "p95_complete_ms": complete[math.ceil(len(complete) * .95) - 1] if complete else None,
            "median_phases_ms": {phase: float(np.median([sample["phases_ms"][phase] for sample in samples
                                                         if phase in sample["phases_ms"]])) for phase in phases},
        }
        print(json.dumps({"variant": name, **data["summary"]}), flush=True)
    save()
    print(args.output / "results.json", flush=True)


if __name__ == "__main__":
    main()
