"""M1 tests for the vector store + brute-force search (numpy ground truth)."""
import math
import struct
import tracemalloc

import numpy as np
import pytest

from agent.runtime.context_index import vector_index as mod


def test_default_db_path_resolution(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(mod.ENV_DB_PATH, str(tmp_path / "custom-vectors.db"))
    assert mod.default_vectors_db_path() == tmp_path / "custom-vectors.db"
    monkeypatch.delenv(mod.ENV_DB_PATH, raising=False)
    monkeypatch.setenv("ASTRA_EMBEDDING_BACKEND", "mlx")
    default = mod.default_vectors_db_path()
    assert default.name == "context-vectors.db"
    assert default.parent.name == ".astra"  # repo-state convention, like sessions.db


def test_store_roundtrip_upsert_and_hash_increment(tmp_path) -> None:
    store = mod.VectorStore(tmp_path / "v.db")
    store.upsert("s1", "hash-a", [1.0, 0.0, 0.0])
    store.upsert("s2", "hash-b", [0.0, 1.0, 0.0])
    assert store.hashes() == {"s1": "hash-a", "s2": "hash-b"}

    store.upsert("s1", "hash-a2", [0.0, 0.0, 1.0])  # 内容变更→覆盖
    assert store.hashes()["s1"] == "hash-a2"
    ids, matrix = store.load_matrix()
    assert ids == ["s1", "s2"]  # 稳定序（id 升序），与插入序无关
    assert matrix.shape == (2, 3)
    assert matrix[0].tolist() == pytest.approx([0.0, 0.0, 1.0])


def test_load_matrix_on_empty_db_yields_empty() -> None:
    store = mod.VectorStore(":memory:")
    ids, matrix = store.load_matrix()
    assert ids == [] and matrix.size == 0


def test_truncate_mrl_renormalizes() -> None:
    vec = [3.0, 4.0, 99.0, 99.0]
    cut = mod.truncate_mrl(vec, 2)
    assert math.isclose(math.sqrt(sum(x * x for x in cut)), 1.0)
    assert math.isclose(cut[0], 0.6) and math.isclose(cut[1], 0.8)


def test_vector_search_ranks_by_cosine_descending() -> None:
    ids = ["far", "mid", "near"]
    matrix = np.array([[0.0, 1.0], [0.7, 0.7], [1.0, 0.0]], dtype=np.float32)
    hits = mod.vector_search([1.0, 0.0], matrix, ids, k=2)
    assert [hit[0] for hit in hits] == ["near", "mid"]
    assert hits[0][1] == pytest.approx(1.0)
    assert hits[1][1] == pytest.approx(0.7 / math.sqrt(0.98))


@pytest.mark.parametrize("dtype", [np.float32, np.float64, np.int32])
@pytest.mark.parametrize("strided", [False, True])
def test_vector_search_preserves_raw_cosines_and_readonly_input(dtype, strided):
    values = [[3, 4, 0], [-5, 2, 0], [0, 0, 0], [1000, -500, 20], [-2, 0, 1], [1, 2, 3]]
    matrix = np.asarray(values, dtype=dtype)
    if strided:
        matrix = np.repeat(matrix, 2, axis=1)[:, ::2]
    matrix.flags.writeable = False
    before = matrix.tobytes()
    query = [2.0, -1.0, 0.5]
    ids = [str(index) for index in range(len(values))]
    expected = []
    for identity, row in zip(ids, values):
        score = math.fsum(a * b for a, b in zip(row, query)) / ((math.hypot(*row) or 1) * math.hypot(*query))
        if score > 0:
            expected.append((identity, score))
    expected.sort(key=lambda item: -item[1])

    actual = mod.vector_search(query, matrix, ids, k=len(ids))
    assert [identity for identity, _score in actual] == [identity for identity, _score in expected]
    assert [score for _identity, score in actual] == pytest.approx([score for _identity, score in expected])
    assert mod.vector_search([0, 0, 0], matrix, ids, k=len(ids)) == []
    assert matrix.tobytes() == before and not matrix.flags.writeable


def test_vector_search_keeps_empty_and_invalid_dimension_behavior():
    assert mod.vector_search([], np.empty((0, 3), dtype=np.float32), [], k=3) == []
    matrix = np.ones((2, 3), dtype=np.float32)
    assert mod.vector_search([], matrix, [], k=3) == []
    assert mod.vector_search([], matrix, ["a", "b"], k=0) == []
    with pytest.raises(TypeError):
        mod.vector_search([1, 0], [[1, 0]], ["a"], k=1)
    with pytest.raises(ValueError):
        mod.vector_search([1, 0], matrix, ["a", "b"], k=2)
    with pytest.raises(ValueError):
        mod.vector_search([1, 0, 0], matrix[0], ["a"], k=1)


def test_vector_search_does_not_allocate_another_float32_matrix():
    matrix = np.ones((1024, 1024), dtype=np.float32)
    matrix.flags.writeable = False
    query = [1.0] * matrix.shape[1]
    ids = [str(index) for index in range(len(matrix))]
    tracemalloc.start()
    try:
        hits = mod.vector_search(query, matrix, ids, k=3)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(hits) == 3
    assert all(score == pytest.approx(1.0) for _identity, score in hits)
    assert peak < matrix.nbytes, (peak, matrix.nbytes)


def test_snapshot_preserves_float32_values_ids_and_revisions(tmp_path):
    path = tmp_path / "vectors.db"
    store = mod.VectorStore(path)
    values = [[1 / 3, -0.0, 1e-20], [-2.0, 3.25, 0.0]]
    store.upsert("b", "rev-b", values[1])
    store.upsert("a", "rev-a", values[0])
    store.close()

    ids, matrix, revisions = mod.read_vector_snapshot(path)
    expected = np.asarray(values, dtype="<f4")
    assert ids == ["a", "b"]
    assert revisions == {"a": "rev-a", "b": "rev-b"}
    assert matrix.tobytes() == expected.tobytes()
    assert matrix.flags.c_contiguous and not matrix.flags.writeable
    assert mod.vector_search([1, 0, 0], matrix, ids, k=2) == mod.vector_search([1, 0, 0], expected, ids, k=2)


@pytest.mark.parametrize("blobs", [
    [b""], [b"x"], [struct.pack("<f", 1), struct.pack("<2f", 1, 2)],
    [struct.pack("<f", float("nan"))], [struct.pack("<f", float("inf"))],
])
def test_invalid_binary_vectors_fail_closed(blobs):
    with pytest.raises(ValueError):
        mod.matrix_from_blobs(blobs)


def test_snapshot_allocation_stays_proportional_to_float32_payload(tmp_path):
    """A production-sized activity snapshot must not create millions of Python floats."""
    path = tmp_path / "vectors.db"
    count, dim = 1200, 2560
    blob = struct.pack(f"<{dim}f", *([0.5] * dim))
    store = mod.VectorStore(path)
    store._connection.executemany(
        "INSERT INTO context_index_vectors(summary_id, content_hash, dim, vec) VALUES (?, ?, ?, ?)",
        ((str(index), "revision", dim, blob) for index in range(count)),
    )
    store._connection.commit()
    store.close()
    payload_bytes = count * len(blob)

    tracemalloc.start()
    try:
        ids, matrix, revisions = mod.read_vector_snapshot(path)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(ids) == len(revisions) == count
    assert matrix.shape == (count, dim) and matrix.nbytes == payload_bytes
    assert peak < 4 * payload_bytes, (peak, payload_bytes)
