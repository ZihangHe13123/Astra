"""Measure warm vector retrieval and isolated allocation/BLAS alternatives.

This diagnostic reuses the production-sized test fixture and real reader. It
does not change production code, deadlines, embedding models, or acceptance.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
import ctypes
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
VARIANTS = ("baseline", "no_copy", "norm_einsum", "dot_einsum", "no_copy_norm", "all", "production")
REPEATS = 30


def save(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def distribution(values: list[float]) -> dict[str, float]:
    return {"p50_ms": statistics.median(values),
            "p95_ms": sorted(values)[math.ceil(len(values) * .95) - 1], "max_ms": max(values)}


def numpy_runtime(np: Any) -> dict[str, object]:
    captured = io.StringIO()
    with redirect_stdout(captured):
        np.show_config()
        np.show_runtime()
    info: dict[str, object] = {"numpy": np.__version__, "config_and_runtime": captured.getvalue(),
                              "thread_env": {key: os.environ.get(key) for key in (
                                  "OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                                  "VECLIB_MAXIMUM_THREADS", "BLIS_NUM_THREADS")},
                              "openblas_thread_queries": []}
    # Query only the NumPy wheel's own BLAS library. Do not install a profiler
    # or change its global thread settings while measuring the production path.
    libraries = Path(np.__file__).resolve().parent.parent / "numpy.libs"
    queries: list[dict[str, object]] = []
    if libraries.is_dir():
        for path in libraries.iterdir():
            if "openblas" not in path.name.lower():
                continue
            try:
                library = ctypes.CDLL(str(path))
                for symbol in ("scipy_openblas_get_num_threads64_", "openblas_get_num_threads64_",
                               "scipy_openblas_get_num_threads", "openblas_get_num_threads"):
                    getter = getattr(library, symbol, None)
                    if getter is not None:
                        getter.argtypes = []
                        getter.restype = ctypes.c_int
                        queries.append({"library": path.name, "symbol": symbol, "threads": getter()})
                        break
                else:
                    queries.append({"library": path.name, "threads": None, "reason": "getter unavailable"})
            except (OSError, AttributeError) as exc:
                queries.append({"library": path.name, "threads": None, "reason": type(exc).__name__})
    info["openblas_thread_queries"] = queries
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    import numpy as np
    import pytest
    from agent.runtime.context_index import embedder, semantic_index, semantic_reader, vector_index
    from agent.runtime.context_index.query import plan_query
    from agent.runtime.context_index.query_embedding import QueryEmbedding, current_embedding

    spec = importlib.util.spec_from_file_location("vector_performance_fixture", ROOT / "tests/test_context_index_performance.py")
    assert spec is not None and spec.loader is not None
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    summary: dict[str, Any] = {"platform": platform.platform(), "python": sys.version,
                               "cpu_count": os.cpu_count(), "repeats_per_variant": REPEATS,
                               "variant_order": list(VARIANTS), "runtime": numpy_runtime(np),
                               "baseline_semantics": "Historical full-selection copy + linalg row norm + BLAS dot; independent of current production",
                               "production_semantics": "Current unmodified vector_search and reader selection; separate from the six historical alternatives",
                               "selection_measurement": "Historical copies occur inside the search hook after metadata selection; production uses its real selection",
                               "scope": "synthetic fixed vectors; real canonical archive and SemanticReader",
                               "acceptance_ms": 75, "production_deadlines_unchanged": True,
                               "observations": [], "microbenchmarks": {}, "variants": {}}
    save(output / "summary.json", summary)

    class Encoder:
        def encode(self, texts):
            return [[1.0, *([0.0] * 4095)]] * len(texts)

    def candidate(query_vec, matrix, ids, *, k, norm_einsum=False, dot_einsum=False):
        query = np.asarray(query_vec, dtype=np.float32)
        query_norm = float(np.linalg.norm(query)) or 1.0
        norms = (np.sqrt(np.einsum("ij,ij->i", matrix, matrix)) if norm_einsum
                 else np.linalg.norm(matrix, axis=1))
        norms = np.where(norms == 0, 1.0, norms)
        dots = np.einsum("ij,j->i", matrix, query) if dot_einsum else matrix @ query
        scores = dots / (norms * query_norm)
        order = np.argsort(-scores)[:k]
        return [(ids[int(index)], float(scores[int(index)])) for index in order if scores[int(index)] > 0.0]

    # Also compare non-normalized, zero and negative rows: the fixed one-hot
    # performance fixture alone cannot establish general cosine equivalence.
    rng = np.random.default_rng(8312026)
    raw = rng.normal(size=(97, 4096)).astype(np.float32)
    raw *= np.linspace(.01, 100, len(raw), dtype=np.float32)[:, None]
    raw[0] = 0
    raw_query = rng.normal(size=4096).astype(np.float32)
    raw_ids = [str(index) for index in range(len(raw))]
    expected = vector_index.vector_search(raw_query.tolist(), raw, raw_ids, k=len(raw_ids))
    for norm, dot in ((True, False), (False, True), (True, True)):
        actual = candidate(raw_query, raw, raw_ids, k=len(raw_ids), norm_einsum=norm, dot_einsum=dot)
        assert [key for key, _ in actual] == [key for key, _ in expected]
        np.testing.assert_allclose([score for _, score in actual], [score for _, score in expected], rtol=1e-5, atol=1e-7)
    summary["raw_cosine_comparison"] = "same ranking; scores agree at rtol=1e-5/atol=1e-7"

    with tempfile.TemporaryDirectory(prefix="astra-vector-probe-") as temporary, pytest.MonkeyPatch.context() as patch:
        home = Path(temporary)
        patch.setenv("ASTRA_HOME", str(home))
        session_db = home / "sessions.db"
        connection = fixture._create_session_fixture(session_db)
        try:
            encoder = Encoder()
            patch.setattr(embedder, "ready_embedder", lambda: encoder)
            vectors = home / "vectors.db"
            report = semantic_index.rebuild_source(session_db, vectors, "session", backend=encoder)
            assert report["encoded"] == 5000
            original_open = semantic_index.open_readonly
            with pytest.MonkeyPatch.context() as warmup:
                warmup.setattr(semantic_index, "open_readonly", lambda path, **_kwargs: original_open(path, deadline_ms=10_000))
                rows, matrix = semantic_index.read_snapshot(vectors, "session", session_db)
            assert matrix.shape == (5000, 4096) and not matrix.flags.writeable
            summary["matrix"] = {"shape": list(matrix.shape), "dtype": str(matrix.dtype),
                                 "bytes": matrix.nbytes, "c_contiguous": bool(matrix.flags.c_contiguous),
                                 "writeable": bool(matrix.flags.writeable)}
            query = np.asarray(encoder.encode([""])[0], dtype=np.float32)
            indices = list(range(len(rows)))
            components = {
                "full_advanced_index_copy": lambda: matrix[indices],
                "row_norm_linalg": lambda: np.linalg.norm(matrix, axis=1),
                "row_norm_einsum": lambda: np.sqrt(np.einsum("ij,ij->i", matrix, matrix)),
                "dot_blas": lambda: matrix @ query,
                "dot_einsum": lambda: np.einsum("ij,j->i", matrix, query),
            }
            measured: dict[str, list[float]] = {name: [] for name in components}
            for round_index in range(REPEATS):
                names = list(components)
                names = names[round_index % len(names):] + names[:round_index % len(names)]
                for name in names:
                    start = time.perf_counter_ns()
                    value = components[name]()
                    measured[name].append((time.perf_counter_ns() - start) / 1_000_000)
                    del value
            summary["microbenchmarks"] = {name: {**distribution(values), "samples_ms": values}
                                          for name, values in measured.items()}
            save(output / "summary.json", summary)
            reader = semantic_reader.SemanticReader(session_db, vectors)
            plan = plan_query("换一种说法寻找相关历史", fixture._NOW)
            assert all(plan.contains(row[2]) and row[3] <= plan.cutoff for row in rows)
            original_stage = semantic_reader.observe_stage
            original_search = semantic_reader.vector_search
            current: dict[str, Any] = {}
            reference: list[tuple[str, str, float, str]] | None = None

            @contextmanager
            def stage(stages, name):
                start = time.perf_counter_ns()
                try:
                    with original_stage(stages, name):
                        yield
                finally:
                    current["stages_ms"][name] = (time.perf_counter_ns() - start) / 1_000_000

            def search(query_vec, selected_matrix, ids, *, k):
                name = current["variant"]
                assert ids == [row[0] for row in rows]
                # This fixture selects every row. Require the production
                # no-copy fix so a hidden earlier copy cannot skew comparisons.
                assert np.shares_memory(selected_matrix, matrix)
                if name == "production":
                    current["selection_ms"] = 0.0  # Included in the real search stage, not separately instrumented.
                    current["selection_copied"] = False
                    return original_search(query_vec, selected_matrix, ids, k=k)
                started = time.perf_counter_ns()
                copy = name not in {"no_copy", "no_copy_norm", "all"}
                selected = matrix[indices] if copy else matrix
                current["selection_ms"] = (time.perf_counter_ns() - started) / 1_000_000
                current["selection_copied"] = copy
                return candidate(query_vec, selected, ids, k=k,
                                 norm_einsum=name in {"norm_einsum", "no_copy_norm", "all"},
                                 dot_einsum=name in {"dot_einsum", "all"})

            patch.setattr(semantic_reader, "observe_stage", stage)
            patch.setattr(semantic_reader, "vector_search", search)
            for round_index in range(REPEATS):
                rotation = round_index % len(VARIANTS)
                for variant in (*VARIANTS[rotation:], *VARIANTS[:rotation]):
                    current = {"round": round_index + 1, "variant": variant, "stages_ms": {}}
                    token = current_embedding.set(QueryEmbedding(plan.query))
                    try:
                        start = time.perf_counter_ns()
                        result = reader.recommend("session", session_db, plan, fixture._WORKSPACE, "current-session", frozenset())
                        current["elapsed_ms"] = (time.perf_counter_ns() - start) / 1_000_000
                    finally:
                        current_embedding.reset(token)
                    identities = [(item.identity, item.revision, item.native_query_rank, item.private_text)
                                  for item in result.relevance]
                    if reference is None and variant == "baseline" and identities and not result.error_category:
                        reference = identities
                    current.update(nonempty=bool(identities), error_category=result.error_category,
                                   same_results=reference is not None and identities == reference)
                    summary["observations"].append(current)
                save(output / "summary.json", summary)
            for variant in VARIANTS:
                observations = [item for item in summary["observations"] if item["variant"] == variant]
                stats: dict[str, Any] = distribution([item["elapsed_ms"] for item in observations])
                stats.update({"nonempty": sum(item["nonempty"] for item in observations),
                              "same_results": all(item["same_results"] for item in observations),
                              "errors": [item["error_category"] for item in observations if item["error_category"]],
                              "within_original_75ms_gate": stats["p95_ms"] <= 75,
                              "stages": {name: distribution([item["stages_ms"][name] for item in observations
                                                              if name in item["stages_ms"]])
                                         for name in ("snapshot", "encode", "search", "validate")
                                         if any(name in item["stages_ms"] for item in observations)}})
                summary["variants"][variant] = stats
            summary["comparison_valid"] = all(value["same_results"] and not value["errors"]
                                              for value in summary["variants"].values())
            save(output / "summary.json", summary)
        finally:
            connection.close()
    print(json.dumps({"microbenchmarks": {key: distribution(value["samples_ms"])
                                          for key, value in summary["microbenchmarks"].items()},
                      "variants": summary["variants"], "comparison_valid": summary["comparison_valid"]}, indent=2))
    return 0 if summary["comparison_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
