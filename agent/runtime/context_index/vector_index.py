"""M1 vector storage + brute-force cosine search for the Context Index.

Design: docs/context-index-platforms.md.
The store lives in Astra-owned state (``.astra/context-vectors.db``), never inside
the externally-synced read-only activity DB, and is fully rebuildable from summaries.
numpy is imported lazily so the missing ``[embedding]`` extra can never break the
base package import chain.
"""
from __future__ import annotations

from agent.runtime.paths import state_path

import math
import hashlib
import os
import sqlite3
import struct
from pathlib import Path
from typing import Sequence
from urllib.parse import quote

ENV_DB_PATH = "ASTRA_CONTEXT_INDEX_VECTORS_DB"
_REPO_ROOT = Path(__file__).resolve().parents[3]


def default_vectors_db_path() -> Path:
    configured = os.getenv(ENV_DB_PATH, "").strip()
    if configured:
        return Path(configured)
    from .embedder import backend_name, embedding_identity

    suffix = "" if backend_name() == "mlx" else "-" + hashlib.sha256(
        embedding_identity().encode()
    ).hexdigest()[:12]
    return state_path(f"context-vectors{suffix}.db", root=_REPO_ROOT)


class VectorStore:
    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(str(path))
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS context_index_vectors (
                   summary_id   TEXT PRIMARY KEY,
                   content_hash TEXT NOT NULL,
                   dim          INTEGER NOT NULL,
                   vec          BLOB NOT NULL,
                   encoded_at   TEXT NOT NULL DEFAULT ''
               )"""
        )
        self._connection.commit()
        self._connection.execute("CREATE TABLE IF NOT EXISTS vector_revision (revision INTEGER NOT NULL)")
        if self._connection.execute("SELECT 1 FROM vector_revision LIMIT 1").fetchone() is None:
            self._connection.execute("INSERT INTO vector_revision VALUES (0)")
        self._connection.commit()
        from .embedder import embedding_identity

        self._connection.execute("CREATE TABLE IF NOT EXISTS embedding_metadata (identity TEXT NOT NULL)")
        row = self._connection.execute("SELECT identity FROM embedding_metadata LIMIT 1").fetchone()
        if row is not None and row[0] != embedding_identity():
            self._connection.close()
            raise ValueError("Embedding model changed; build a separate vector index")
        if row is None:
            if self._connection.execute("SELECT 1 FROM context_index_vectors LIMIT 1").fetchone():
                if not embedding_identity().startswith("mlx:"):
                    self._connection.close()
                    raise ValueError("Legacy vectors belong to MLX; build a separate vector index")
            self._connection.execute("INSERT INTO embedding_metadata VALUES (?)", (embedding_identity(),))
            self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def _bump_revision(self) -> None:
        """Run in the writer's transaction: a same-size commit can leave every file stat equal."""
        self._connection.execute("UPDATE vector_revision SET revision = revision + 1")

    def upsert(self, summary_id: str, content_hash: str, vec: Sequence[float]) -> None:
        blob = struct.pack(f"<{len(vec)}f", *vec)
        self._connection.execute(
            """INSERT INTO context_index_vectors(summary_id, content_hash, dim, vec)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(summary_id) DO UPDATE SET
                   content_hash = excluded.content_hash,
                   dim = excluded.dim,
                   vec = excluded.vec""",
            (summary_id, content_hash, len(vec), blob),
        )
        self._bump_revision()
        self._connection.commit()

    def hashes(self) -> dict[str, str]:
        return dict(
            self._connection.execute(
                "SELECT summary_id, content_hash FROM context_index_vectors"
            )
        )

    def load_matrix(self) -> tuple[list[str], "object"]:
        rows = self._connection.execute(
            "SELECT summary_id, vec FROM context_index_vectors ORDER BY summary_id"
        ).fetchall()
        matrix = matrix_from_blobs([blob for _sid, blob in rows])
        return [row[0] for row in rows], matrix


def matrix_from_blobs(blobs: Sequence[bytes]):
    """Decode immutable float32 storage without allocating Python float objects.

    Expanding millions of floats holds the GIL long enough to starve concurrent
    archive readers and exhaust their deadlines. One binary buffer also bounds
    the transient memory to the stored payload instead of a Python object graph.
    """
    import numpy as np

    if not blobs:
        return np.zeros((0, 0), dtype=np.float32)
    dimensions = {len(blob) for blob in blobs}
    if len(dimensions) != 1 or any(size == 0 or size % 4 for size in dimensions):
        raise ValueError("Invalid vector dimensions")
    matrix = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), -1)
    if not np.all(np.isfinite(matrix)):
        raise ValueError("Nonfinite vector snapshot")
    return matrix


def read_vector_revision(path: Path) -> int | None:
    """A transaction-visible revision, without retaining handles that prevent replacement on Windows.

    Legacy indexes have no revision; readers conservatively refresh until a writer upgrades them.
    """
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.05)
    try:
        present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='vector_revision'").fetchone()
        row = connection.execute("SELECT revision FROM vector_revision LIMIT 1").fetchone() if present else None
        return int(row[0]) if row else None
    finally:
        connection.close()


def read_vector_snapshot(path: Path) -> tuple[list[str], object, dict[str, str]]:
    """Read vectors and their content revisions together, without opening a writer."""
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=0.05)
    try:
        from .embedder import embedding_identity

        metadata = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='embedding_metadata'"
        ).fetchone()
        identity = connection.execute("SELECT identity FROM embedding_metadata LIMIT 1").fetchone() if metadata else None
        if (identity and identity[0] != embedding_identity()) or (
            identity is None and not embedding_identity().startswith("mlx:")
        ):
            raise ValueError("Vector index belongs to a different embedding model")
        rows = connection.execute(
            "SELECT summary_id, content_hash, vec FROM context_index_vectors ORDER BY summary_id"
        ).fetchall()
    finally:
        connection.close()
    matrix = matrix_from_blobs([blob for _, _, blob in rows])
    return [sid for sid, _, _ in rows], matrix, {sid: revision for sid, revision, _ in rows}


def truncate_mrl(vec: Sequence[float], dim: int) -> list[float]:
    """Matryoshka slice + L2 renormalize (Qwen3-Embedding supports prefix dims)."""
    head = [float(x) for x in vec[:dim]]
    norm = math.sqrt(sum(value * value for value in head))
    return [value / norm for value in head] if norm else head


def vector_search(
    query_vec: Sequence[float],
    matrix: "object",
    ids: Sequence[str],
    *,
    k: int,
) -> list[tuple[str, float]]:
    """Cosine top-k, descending; non-positive similarities are dropped."""
    import numpy as np

    if not isinstance(matrix, np.ndarray):
        raise TypeError("vector search requires a NumPy matrix")
    if matrix.size == 0 or not ids or k <= 0:
        return []
    query = np.asarray(query_vec, dtype=np.float32)
    query_norm = float(np.linalg.norm(query)) or 1.0
    # Accumulate float32 storage in float64: a wide row with mixed scales can
    # otherwise cross the similarity cutoff through reduction error. This
    # still avoids a full matrix of squares or a full float64 conversion.
    norms = (np.sqrt(np.einsum("ij,ij->i", matrix, matrix, dtype=np.float64))
             if matrix.ndim == 2 and matrix.dtype == np.float32
             else np.linalg.norm(matrix, axis=1))
    norms = np.where(norms == 0, 1.0, norms)
    scores = (matrix @ query) / (norms * query_norm)
    order = np.argsort(-scores)[:k]
    return [
        (ids[int(index)], float(scores[int(index)]))
        for index in order
        if scores[int(index)] > 0.0
    ]
