"""Measure small SQL readers beside real current/historical vector decoders."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import gc
import hashlib
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def current_acceptance(output, *, required):
    """Keep expected historical failures, but gate the real current SQL reader."""
    groups = {}
    unexpected = [
        {key: row[key] for key in ("source", "variant", "round", "warmup", "status")}
        for row in output["samples"] if row["status"] not in {"complete", "deadline"}
    ]
    for source in output["counts"]:
        for variant in ("alone", "current"):
            rows = [row for row in output["samples"]
                    if row["source"] == source and row["variant"] == variant and not row["warmup"]]
            values = sorted(row["victim_elapsed_ms"] if row["status"] == "complete" else math.inf
                            for row in rows)
            p95 = values[math.ceil(len(values) * .95) - 1] if values else math.inf
            groups[source + "_" + variant] = {
                "measured_samples": len(rows), "expected_samples": output["rounds"],
                "failed_reads": sum(row["status"] != "complete" for row in rows),
                "p95_is_infinite": not math.isfinite(p95),
                "p95_victim_ms": p95 if math.isfinite(p95) else None,
                "passed": len(rows) == output["rounds"] and p95 <= 75,
            }
    return {
        "required": required, "assessed": True, "limit_ms": 75,
        "rule": "Each alone/current source has20 measured samples; failed reads count as infinity and p95 must be <=75ms. Any unexpected SQLite status fails. Historical deadline controls never affect this gate.",
        "groups": groups, "unexpected_statuses": unexpected,
        "passed": all(group["passed"] for group in groups.values()) and not unexpected,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-current", action="store_true",
                        help="save all observations, then fail unless alone/current small SQL p95 stays within75ms")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("use a fresh output path")
    args.output.mkdir(parents=True)
    folder = Path(tempfile.mkdtemp(prefix="astra-decode-contention-"))
    os.environ["ASTRA_HOME"] = str(folder / "home")
    os.environ["ASTRA_EMBEDDING_BACKEND"] = "mlx"
    import numpy as np
    from agent.runtime.context_index import semantic_index, sqlite_reader, vector_index

    counts, dim, rounds, warmups = {"activity": 1200, "session": 5000}, 2560, 20, 2
    path, victim_path = folder / "vectors.db", folder / "small.db"
    archive = folder / "session.db"
    tail = struct.pack(f"<{dim - 1}f", *([.5] * (dim - 1)))
    store = semantic_index.EvidenceVectorStore(path)
    store._connection.executemany(
        "INSERT INTO context_index_vectors(summary_id, content_hash, dim, vec) VALUES (?, ?, ?, ?)",
        ((str(i), "revision", dim, struct.pack("<f", i) + tail) for i in range(counts["activity"])),
    )
    store._connection.executemany(
        "INSERT INTO context_memory_vectors VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (("session", semantic_index.archive_key(archive), str(i), "revision", semantic_index._LAYOUT,
          float(i), float(i), struct.pack("<f", i) + tail) for i in range(counts["session"])),
    )
    store._connection.commit()
    store.close()
    with sqlite3.connect(victim_path) as db:
        db.execute("CREATE TABLE numbers (value INTEGER NOT NULL)")
        db.executemany("INSERT INTO numbers VALUES (?)", ((i,) for i in range(4096)))
    query = "SELECT SUM(value * value) FROM numbers"
    expected_sum = sum(i * i for i in range(4096))
    progress_calls = []
    with sqlite_reader.open_readonly(victim_path, deadline_ms=75) as db:
        db.set_progress_handler(lambda: progress_calls.append(1) or 0, 1000)
        assert db.execute(query).fetchone()[0] == expected_sum
    assert progress_calls, "the victim query must reach the real progress interval"

    # Read the real blobs once outside the experiment. Large SQL materialization
    # must not confound this decoder-only test of another source's SQL budget.
    preload = {}
    with sqlite_reader.open_readonly(path, deadline_ms=2000) as db:
        preload["activity"] = db.execute(
            "SELECT summary_id, vec FROM context_index_vectors ORDER BY summary_id"
        ).fetchall()
    with sqlite_reader.open_readonly(path, deadline_ms=2000) as db:
        preload["session"] = db.execute(
            "SELECT item_id, vec FROM context_memory_vectors WHERE source=? AND archive=? "
            "AND layout=? ORDER BY timestamp DESC, item_id LIMIT ?",
            ("session", semantic_index.archive_key(archive), semantic_index._LAYOUT, counts["session"]),
        ).fetchall()
    payloads = {source: [row[1] for row in rows] for source, rows in preload.items()}
    expected_first = {source: np.asarray([int(row[0]) for row in rows], dtype=np.float32)
                      for source, rows in preload.items()}
    del preload
    variants = ("alone", "current", "old_whole", "old_critical")
    output = {
        "platform": platform.platform(), "python": sys.version, "numpy": np.__version__,
        "sqlite_version": sqlite3.sqlite_version, "fixture": str(folder),
        "counts": counts, "dim": dim, "rounds": rounds, "warmup_rounds": warmups,
        "payload_bytes": {source: count * dim * 4 for source, count in counts.items()},
        "victim": {"rows": 4096, "database_bytes": victim_path.stat().st_size, "query": query,
                   "preflight_progress_1000_calls": len(progress_calls), "deadline_ms": 75},
        "source_sha256": {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                          for name, module in (("sqlite_reader", sqlite_reader), ("vector_index", vector_index))},
        "clock_info": {name: vars(time.get_clock_info(name)) for name in ("monotonic", "perf_counter")},
        "interpretation": [
            "Synthetic data only. Activity1200 and session5000 use the existing cold benchmark sizes, not an increased load selected to force failure.",
            "Large BLOB SQL reads happen once before timing. This separates decoder contention from large snapshot SQL throughput; it does not replace the original cold benchmark.",
            "The victim uses the unmodified production open_readonly75ms reader, including its real clock and1000-op progress handler. No sleeps, clock mocks or switch-interval changes.",
            "current calls production matrix_from_blobs. old_whole is the historical struct.unpack-list then np.array(float32) decoder. old_critical runs that same np.array step after preparing its real tuples before the gate.",
            "The barrier releases conversion with the victim deadline already active. Queue time before release and thread-resume delay after release are reported separately.",
            "Gated results show a reachable scheduling effect, not its frequency under natural application scheduling. A negative control without actual deadline failures remains inconclusive for deadline protection.",
            "Each source has20 measured rounds and2 warmups with rotating variant order. Every decoded item and vector value is checked after both timed sections finish.",
        ],
        "samples": [], "summary": {},
        "acceptance": {"required": args.require_current, "assessed": False},
    }

    def save():
        (args.output / "results.json").write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")

    def run(source, variant, pool):
        trace = {}
        gate = threading.Barrier(2, action=lambda: trace.update(gate_released=time.perf_counter()))
        ready = threading.Event()

        def decode():
            started = time.perf_counter()
            blobs = payloads[source]
            expanded = None
            if variant == "old_critical":
                expanded = [struct.unpack(f"<{len(blob) // 4}f", blob) for blob in blobs]
            trace["pre_gate_ms"] = (time.perf_counter() - started) * 1000
            ready.set()
            gate.wait(timeout=5)
            started = time.perf_counter()
            if variant == "current":
                matrix = vector_index.matrix_from_blobs(blobs)
            else:
                if expanded is None:
                    expanded = [struct.unpack(f"<{len(blob) // 4}f", blob) for blob in blobs]
                matrix = np.array(expanded, dtype=np.float32)
                del expanded
            trace["decoder_after_gate_ms"] = (time.perf_counter() - started) * 1000
            return matrix

        future = None
        row = {"source": source, "variant": variant}
        if variant != "alone":
            future = pool.submit(decode)
            assert ready.wait(5), "decoder did not reach gate"
        started = time.perf_counter()
        try:
            with sqlite_reader.open_readonly(victim_path, deadline_ms=75) as db:
                active = time.perf_counter()
                row["connection_setup_ms"] = (active - started) * 1000
                if variant != "alone":
                    gate.wait(timeout=5)
                    resumed = time.perf_counter()
                    row["pre_release_wait_ms"] = (trace["gate_released"] - active) * 1000
                    row["post_release_resume_delay_ms"] = (resumed - trace["gate_released"]) * 1000
                else:
                    row["pre_release_wait_ms"] = row["post_release_resume_delay_ms"] = 0.0
                assert db.execute(query).fetchone()[0] == expected_sum
                row["status"] = "complete"
        except sqlite3.OperationalError as error:
            row["status"] = "deadline" if "interrupt" in str(error).lower() else "sqlite_error"
            row["error"] = str(error)
        finally:
            row["victim_elapsed_ms"] = (time.perf_counter() - started) * 1000
        if future is not None:
            matrix = future.result(timeout=5)
            assert matrix.shape == (counts[source], dim)
            np.testing.assert_array_equal(matrix[:, 0], expected_first[source])
            assert bool(np.all(matrix[:, 1:] == np.float32(.5)))
            row["verified_rows"] = counts[source]
        row.update({key: value for key, value in trace.items() if key != "gate_released"})
        return row

    with ThreadPoolExecutor(max_workers=1) as pool:
        for source in counts:
            for index in range(rounds + warmups):
                offset = index % len(variants)
                for variant in variants[offset:] + variants[:offset]:
                    gc.collect()
                    row = run(source, variant, pool)
                    row.update(round=index, warmup=index < warmups)
                    output["samples"].append(row)
                    save()
            for variant in variants:
                rows = [row for row in output["samples"]
                        if row["source"] == source and row["variant"] == variant and not row["warmup"]]
                times = sorted(row["victim_elapsed_ms"] for row in rows)
                summary = {
                    "complete": sum(row["status"] == "complete" for row in rows),
                    "deadline": sum(row["status"] == "deadline" for row in rows),
                    "other_statuses": sorted({row["status"] for row in rows} - {"complete", "deadline"}),
                    "p95_victim_ms": times[math.ceil(len(times) * .95) - 1],
                    "max_pre_release_wait_ms": max(row["pre_release_wait_ms"] for row in rows),
                    "max_post_release_resume_delay_ms": max(row["post_release_resume_delay_ms"] for row in rows),
                    "median_decoder_ms": statistics.median([row.get("decoder_after_gate_ms", 0) for row in rows]),
                }
                output["summary"][source + "_" + variant] = summary
                print(json.dumps({"source": source, "variant": variant, **summary}), flush=True)
            save()
    output["acceptance"] = current_acceptance(output, required=args.require_current)
    save()
    print(json.dumps({"acceptance": output["acceptance"]}), flush=True)
    print(args.output / "results.json", flush=True)
    if args.require_current and not output["acceptance"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
