"""Isolated 75 ms snapshot deadline-clock comparison; no production changes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import gc
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def sample_clock(clock):
    until = time.perf_counter() + .1
    previous = clock()
    changes, iterations, positive = 0, 0, []
    while time.perf_counter() < until:
        current = clock()
        iterations += 1
        if current > previous:
            changes += 1
            if len(positive) < 256:
                positive.append((current - previous) * 1000)
        previous = current
    return {"sampling_ms": 100, "calls": iterations, "positive_changes": changes,
            "recorded_positive_ticks": len(positive), "positive_tick_ms": {
                "min": min(positive) if positive else None,
                "median": statistics.median(positive) if positive else None,
                "max": max(positive) if positive else None,
            }}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("use a fresh output path")
    args.output.mkdir(parents=True)
    folder = Path(tempfile.mkdtemp(prefix="astra-vector-clock-"))
    os.environ["ASTRA_HOME"] = str(folder / "home")
    os.environ["ASTRA_EMBEDDING_BACKEND"] = "mlx"
    import numpy as np
    from agent.runtime.context_index import semantic_index, sqlite_reader, vector_index

    counts, dim, measured, warmups = {"session": 5000, "memory": 32, "activity": 1200}, 2560, 20, 2
    path = folder / "vectors.db"
    archives = {source: folder / f"{source}.db" for source in ("session", "memory")}
    tail = struct.pack(f"<{dim - 1}f", *([.5] * (dim - 1)))
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
    output = {
        "platform": platform.platform(), "python": sys.version, "numpy": np.__version__,
        "sqlite_version": sqlite3.sqlite_version, "fixture": str(folder), "counts": counts, "dim": dim,
        "measured_pairs_per_mode": measured, "warmup_pairs_per_mode": warmups,
        "source_sql_deadline_ms": {"session": 75, "memory": 75, "activity": None},
        "clock_info": {name: vars(time.get_clock_info(name)) for name in ("monotonic", "perf_counter")},
        "tick_samples": {name: sample_clock(getattr(time, name)) for name in ("monotonic", "perf_counter")},
        "interpretation": [
            "Only sqlite_reader.time is replaced by a local clock proxy; the standard library time module is never modified.",
            "Both clocks have the same passive observer. The monotonic group reproduces the historical clock; the perf_counter group uses the precise clock. The75ms value, progress interval, query, connection setup and matrix decoding are unchanged.",
            "Application snapshot cache is cleared between samples; OS pagecache is not evicted. No retries or production cache/lock changes.",
            "Serial and barrier-synchronized three-source parallel modes each contain20 measured pairs with alternating clock order and2 retained warmup pairs.",
            "Only session and memory use sqlite_reader and its75ms deadline; activity retains its original reader without that SQL deadline.",
            "Actual expiry ms is measured by perf_counter from the first clock read that constructs the deadline. The last clock observation is not the full SQL duration.",
            "Exact metadata, logical order and every matrix value are checked after all three readers finish their timed section.",
            "Elapsed summaries include connection close and decoding outside the SQL deadline. Failed reads remain failures irrespective of elapsed p95.",
        ],
        "experiments": {f"{mode}_{clock}": {"samples": []} for mode in ("serial", "parallel")
                        for clock in ("monotonic", "perf_counter")},
    }
    original_time = sqlite_reader.time
    local = threading.local()
    clock_name = "monotonic"

    def observed_clock():
        actual = time.perf_counter()
        value = getattr(time, clock_name)()
        record = getattr(local, "clock", None)
        if record is not None:
            if not record["calls"]:
                record["origin_actual"] = actual
                record["origin_clock"] = value
            record["calls"] += 1
            record["last_observation_actual_ms"] = (actual - record["origin_actual"]) * 1000
            if value >= record["origin_clock"] + .075 and record["first_expiry_actual_ms"] is None:
                record["first_expiry_actual_ms"] = record["last_observation_actual_ms"]
                record["first_expiry_clock_ms"] = (value - record["origin_clock"]) * 1000
        return value

    def save():
        (args.output / "results.json").write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")

    def read(source, barrier):
        record = {"calls": 0, "first_expiry_actual_ms": None, "first_expiry_clock_ms": None,
                  "last_observation_actual_ms": None}
        local.clock = record
        if barrier is not None:
            barrier.wait()
        start = time.perf_counter()
        row, result = {"source": source}, None
        try:
            result = (vector_index.read_vector_snapshot(path) if source == "activity"
                      else semantic_index.read_snapshot(path, source, archives[source]))
            row["status"] = "complete"
        except sqlite3.Error as error:
            row["status"] = "deadline" if "interrupt" in str(error).lower() else "sqlite_error"
            row["error"] = str(error)
        finally:
            row["elapsed_ms"] = (time.perf_counter() - start) * 1000
            local.clock = None
        row["clock"] = {key: value for key, value in record.items() if not key.startswith("origin_")}
        return row, result

    def verify(source, result):
        metadata, matrix = result[:2]
        if source == "activity":
            expected_ids = sorted(str(index) for index in range(counts[source]))
            assert metadata == expected_ids
            assert result[2] == dict.fromkeys(expected_ids, "revision")
            first = np.array(list(map(int, expected_ids)), dtype=np.float32)
        else:
            expected = tuple((str(index), "revision", float(index), float(index))
                             for index in reversed(range(counts[source])))
            assert metadata == expected
            first = np.arange(counts[source] - 1, -1, -1, dtype=np.float32)
        assert matrix.shape == (counts[source], dim)
        np.testing.assert_array_equal(matrix[:, 0], first)
        assert bool(np.all(matrix[:, 1:] == np.float32(.5)))

    try:
        sqlite_reader.time = SimpleNamespace(monotonic=observed_clock, perf_counter=observed_clock)
        with ThreadPoolExecutor(max_workers=3) as pool:
            for index in range(measured + warmups):
                modes = ("serial", "parallel") if index % 2 == 0 else ("parallel", "serial")
                clocks = ("monotonic", "perf_counter") if index % 2 == 0 else ("perf_counter", "monotonic")
                for mode in modes:
                    for position, clock_name in enumerate(clocks):
                        with semantic_index._cache_lock:
                            semantic_index._cache.clear()
                        gc.collect()
                        if mode == "parallel":
                            barrier = threading.Barrier(3)
                            futures = [pool.submit(read, source, barrier) for source in counts]
                            results = [future.result(timeout=10) for future in futures]
                            del futures
                        else:
                            results = [read(source, None) for source in counts]
                        rows = []
                        for (row, result), source in zip(results, counts):
                            if result is not None:
                                verify(source, result)
                                row["verified_rows"] = counts[source]
                            rows.append(row)
                        del results, result
                        output["experiments"][f"{mode}_{clock_name}"]["samples"].append({
                            "round": index, "warmup": index < warmups, "position_in_pair": position,
                            "sources": rows,
                        })
                        save()
    finally:
        sqlite_reader.time = original_time
    for name, experiment in output["experiments"].items():
        summary = {}
        for source in counts:
            rows = [row for sample in experiment["samples"] if not sample["warmup"]
                    for row in sample["sources"] if row["source"] == source]
            times = sorted(row["elapsed_ms"] for row in rows)
            expired = [row["clock"]["first_expiry_actual_ms"] for row in rows
                       if row["clock"]["first_expiry_actual_ms"] is not None]
            summary[source] = {
                "complete": sum(row["status"] == "complete" for row in rows),
                "deadline": sum(row["status"] == "deadline" for row in rows),
                "other_statuses": sorted({row["status"] for row in rows} - {"complete", "deadline"}),
                "p95_all_elapsed_ms": times[math.ceil(len(times) * .95) - 1],
                "first_expiry_actual_ms": {"count": len(expired), "min": min(expired) if expired else None,
                                           "median": statistics.median(expired) if expired else None,
                                           "max": max(expired) if expired else None},
            }
        experiment["summary"] = summary
        print(json.dumps({"experiment": name, "summary": summary}), flush=True)
    save()
    print(args.output / "results.json", flush=True)


if __name__ == "__main__":
    main()
