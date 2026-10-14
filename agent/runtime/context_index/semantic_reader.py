"""Semantic candidates revalidated against their canonical, read-only archives."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import sqlite3
import time

from . import embedder
from .lexical import LexicalQuery
from .models import RecommendationCandidate, RetrievalStage, SourceLocator, SourceResult
from .query import QueryPlan
from .query_embedding import current_embedding
from .record_source import _eligible_metadata, _epoch, _revision
from .semantic_index import read_snapshot
from .session_source import (
    SessionRecommendationSource, _IncompatibleSchema, _eligible_rows, _validate_schema,
    content_fingerprint, session_revision,
)
from .sqlite_reader import observe_stage, open_readonly, set_read_window
from .vector_index import default_vectors_db_path, vector_search
from .workspace import WorkspaceIdentity
from ..activity_store import _redact_text


def minimum_similarity() -> float:
    try:
        value = float(os.getenv("ASTRA_CONTEXT_INDEX_MEMORY_MIN_SIMILARITY", "0.75"))
        return min(1.0, max(0.3, value)) if value == value else 0.75
    except ValueError:
        return 0.75


class SemanticReader:
    def __init__(self, session_path: Path, vectors_path: Path | None = None) -> None:
        self.session_path = Path(session_path)
        self.vectors_path = Path(vectors_path) if vectors_path is not None else None

    def ready(self) -> bool:
        return embedder.ready_embedder() is not None and self.vector_path.is_file()

    @property
    def vector_path(self) -> Path:
        return self.vectors_path or default_vectors_db_path()

    def recommend(self, source: str, path: Path, plan: QueryPlan, workspace: WorkspaceIdentity,
                  session_id: str, active: frozenset[str]) -> SourceResult:
        stages: list[RetrievalStage] = []
        result = self._recommend(source, path, plan, workspace, session_id, active, stages)
        return replace(result, stages=tuple(stages))

    def _recommend(self, source, path, plan, workspace, session_id, active, stages) -> SourceResult:
        if not plan.should_recall or not any(char.isalpha() for char in plan.query):
            return SourceResult("disabled")
        if not self.ready():
            return SourceResult("cold")
        try:
            with observe_stage(stages, "snapshot"):
                rows, matrix = read_snapshot(self.vector_path, source, path)
            if not rows:
                return SourceResult("absent")
            query = current_embedding.get()
            if query is None:
                return SourceResult("cold")
            with observe_stage(stages, "encode"):
                encoded = query.get()
            if encoded is None:
                return SourceResult("cold")
            # Drop out-of-window vectors before top-k; later canonical checks also
            # reject changed/deleted evidence, and do not spend the result quota.
            indices = [i for i, row in enumerate(rows) if plan.contains(row[2]) and row[3] <= plan.cutoff]
            if not indices:
                return SourceResult("available")
            with observe_stage(stages, "search"):
                # The ordered filter selects every row on ordinary unbounded
                # recall. Reuse the immutable snapshot instead of copying it.
                subset = matrix if len(indices) == len(rows) else matrix[indices]
                metadata = {rows[i][0]: rows[i][1] for i in indices}
                hits = vector_search(encoded, subset, list(metadata), k=len(metadata))
            hits = [(key, score) for key, score in hits if score >= minimum_similarity()]
            lexical = LexicalQuery.from_text(plan.query)
            active = active | {content_fingerprint(plan.original)}
            reader = self._sessions if source == "session" else self._records
            with observe_stage(stages, "validate"):
                candidates = reader(path, hits, metadata, plan, workspace, session_id, active, lexical)
            return SourceResult("available", relevance=tuple(
                replace(item, channel_ranks=((f"{source}-vector", rank),))
                for rank, item in enumerate(candidates, 1)
            ))
        except FileNotFoundError:
            return SourceResult("absent")
        except sqlite3.OperationalError as exc:
            # Old activity-only vector DBs have no evidence table yet.
            if "no such table" in str(exc):
                return SourceResult("absent")
            return SourceResult("error", error_category="deadline" if "interrupt" in str(exc) else "database_error")
        except (sqlite3.DatabaseError, _IncompatibleSchema, ValueError, TypeError, ImportError):
            return SourceResult("error", error_category="database_error")

    @staticmethod
    def _sessions(path, hits, revisions, plan, workspace, session_id, active, lexical):
        reader = SessionRecommendationSource(path)
        candidates = []
        with open_readonly(path, deadline_ms=60) as db:
            set_read_window(db, plan.cutoff, plan.time_start, plan.time_end, message_id=plan.message_id)
            columns = _validate_schema(db)
            current = reader._resolve_current_session_id(db, session_id, has_source_session_key="source_session_key" in columns) or ""
            current_id = reader._current_message_id(db, current, content_fingerprint(plan.original))
            guards, _ = reader._pool_guards()
            until = time.monotonic() + 0.06
            for offset in range(0, len(hits), 64):
                if time.monotonic() >= until:
                    break
                batch = hits[offset:offset + 64]
                sql = reader._base_select("0", has_workspace_key="workspace_key" in columns).format(from_clause="messages m")
                rows = db.execute(sql + " WHERE m.id IN (" + ",".join("?" for _ in batch) + ") "
                                  "AND m.role IN ('user', 'assistant') " + guards,
                                  [key for key, _ in batch]).fetchall()
                canonical = {str(row["id"]): row for row in rows}
                for key, score in batch:
                    row = canonical.get(key)
                    if row is None:
                        continue
                    revision = session_revision(row["session_id"], row["id"], row["role"], row["content"], row["timestamp"])
                    if revision != revisions[key] or not lexical.matches_anchors(str(row["content"]), semantic=True):
                        continue
                    prepared = _eligible_rows([row], workspace, current, current_id, active, relevance=True)
                    for item in reader._candidates(prepared, workspace):
                        candidates.append(replace(item, native_query_rank=-score, revision=revision,
                                                  locator=replace(item.locator, revision=revision)))
                    if len(candidates) >= 12:
                        return candidates
        return candidates

    @staticmethod
    def _records(path, hits, revisions, plan, workspace, session_id, active, lexical):
        del session_id
        candidates = []
        with open_readonly(path, deadline_ms=60) as db:
            until = time.monotonic() + 0.06
            for offset in range(0, len(hits), 64):
                if time.monotonic() >= until:
                    break
                batch = hits[offset:offset + 64]
                rows = db.execute("SELECT * FROM memory_records WHERE id IN (" +
                                  ",".join("?" for _ in batch) + ") AND status='active'",
                                  [key for key, _ in batch]).fetchall()
                canonical = {str(row["id"]): row for row in rows}
                for key, score in batch:
                    row = canonical.get(key)
                    if row is None or _revision(row) != revisions[key] or not _eligible_metadata(row["metadata_json"], workspace.root or None):
                        continue
                    timestamp = _epoch(row["last_confirmed_at"])
                    if (not plan.contains(timestamp) or _epoch(row["created_at"]) > plan.cutoff
                            or _epoch(row["valid_from"]) > plan.cutoff
                            or (row["valid_until"] and _epoch(row["valid_until"]) <= plan.cutoff)):
                        continue
                    content = str(row["content"])
                    if content_fingerprint(content) in active or not lexical.matches_anchors(content, semantic=True):
                        continue
                    candidates.append(RecommendationCandidate(
                        source="memory", identity=key, description=" ".join(_redact_text(content).split())[:240],
                        timestamp=timestamp, workspace_tier=1 if workspace.label.casefold() in content.casefold() else 2,
                        native_query_rank=-score, topic_key=str(row["kind"]), trust_label="historical_context",
                        locator=SourceLocator("memory_record", key, revision=revisions[key]),
                        private_text=content[:6000], revision=revisions[key], available_at=_epoch(row["created_at"]),
                    ))
                    if len(candidates) >= 12:
                        return candidates
        return candidates
