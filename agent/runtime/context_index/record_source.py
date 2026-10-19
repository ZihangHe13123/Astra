"""Read Astra's own structured SQLite memories without provider/network calls."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ..activity_store import _redact_text
from ..learning_scope import learning_scope_matches

from .models import EvidenceResult, RecommendationCandidate, SourceLocator, SourceResult
from .lexical import LexicalQuery
from .query import QueryPlan
from .session_source import content_fingerprint
from .sqlite_reader import open_readonly
from .workspace import WorkspaceIdentity


def _epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _revision(row: sqlite3.Row) -> str:
    fields = ("content", "kind", "status", "created_at", "last_confirmed_at", "valid_from", "valid_until", "metadata_json")
    return hashlib.sha256(json.dumps([row[name] for name in fields], ensure_ascii=False).encode()).hexdigest()


def _eligible_metadata(raw: str, workspace: str | None = None, *, respect_scope: bool = True,
                       workspace_key: str = "") -> bool:
    try:
        metadata = json.loads(raw)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(metadata, dict)
        and metadata.get("pinned") is not True
        and metadata.get("conflict_state") != "pending"
        and (not respect_scope or (
            learning_scope_matches(metadata, workspace)
            # A saved memory scoped to a project belongs to that checkout only.
            and (metadata.get("scope") != "project"
                 or (bool(workspace_key) and metadata.get("workspace_key") == workspace_key))))
    )


class RecordSource:
    """Request-scoped records with canonical version checks for evidence opens."""

    def __init__(self, path: Path):
        self.path = path
        self._versions: dict[str, str] = {}
        self._workspace: str | None = None
        self._workspace_key = ""

    def recommend(self, plan: QueryPlan, workspace: WorkspaceIdentity, active: frozenset[str], *,
                  minimum: int | None = None) -> SourceResult:
        """``minimum`` lowers how many query terms a record must contain, for a search the model asked for."""
        self._workspace = workspace.root or None
        self._workspace_key = workspace.key
        lexical = LexicalQuery.from_text(plan.query)
        if not lexical.terms or not plan.should_recall:
            return SourceResult("available")
        try:
            with open_readonly(self.path) as db:
                db.create_function("memory_epoch", 1, _epoch, deterministic=True)
                db.create_function("memory_eligible", 1, lambda raw: _eligible_metadata(
                    raw, self._workspace, workspace_key=self._workspace_key), deterministic=True)
                mask, params = lexical.mask_sql("content")
                anchors, anchor_params = lexical.anchor_sql("content")
                rows = db.execute(
                    f"""WITH matched AS MATERIALIZED (
                        SELECT *, ({mask}) AS query_match_mask FROM memory_records WHERE status = 'active'
                        AND memory_epoch(created_at) <= ?
                        AND memory_epoch(last_confirmed_at) <= ?
                        AND memory_epoch(last_confirmed_at) >= ?
                        AND (? = 0 OR memory_epoch(last_confirmed_at) < ?)
                        AND memory_epoch(valid_from) <= ?
                        AND (valid_until = '' OR memory_epoch(valid_until) > ?)
                        AND memory_eligible(metadata_json)
                        AND ({anchors}) AND query_match_mask != 0)
                        SELECT *, ({lexical.coverage_sql('query_match_mask')}) AS query_coverage
                        FROM matched WHERE ({lexical.count_sql('query_match_mask')}) >= ?
                        ORDER BY query_coverage DESC, salience DESC, last_confirmed_at DESC, id ASC LIMIT 40""",
                    (*params, plan.cutoff, plan.cutoff, plan.time_start, plan.time_end, plan.time_end,
                     plan.cutoff, plan.cutoff, *anchor_params, lexical.minimum if minimum is None else minimum),
                ).fetchall()
            candidates = []
            for row in rows:
                content = str(row["content"])
                if content_fingerprint(content) in active:
                    continue
                timestamp = _epoch(row["last_confirmed_at"])
                if not plan.contains(timestamp):
                    continue
                key = str(row["id"])
                revision = _revision(row)
                self._versions[key] = revision
                candidates.append(
                    RecommendationCandidate(
                        source="memory",
                        identity=key,
                        description=" ".join(_redact_text(content).split())[:240],
                        timestamp=timestamp,
                        workspace_tier=1 if workspace.label.casefold() in content.casefold() else 2,
                        native_query_rank=-float(row["query_coverage"]),
                        topic_key=str(row["kind"]),
                        trust_label="historical_context",
                        locator=SourceLocator("memory_record", key),
                        private_text=content[:6000],
                        revision=revision,
                        available_at=_epoch(row["created_at"]),
                    )
                )
            candidates.sort(key=lambda item: (item.native_query_rank, -item.timestamp, item.identity))
            return SourceResult("available", relevance=tuple(candidates[:12]))
        except FileNotFoundError:
            return SourceResult("absent")
        except sqlite3.DatabaseError:
            return SourceResult("error", error_category="database_error")

    def open(self, locator: SourceLocator, window: int, plan: QueryPlan | None = None) -> EvidenceResult:
        empty = EvidenceResult("memory", "historical_context", "Astra memory unavailable", ())
        expected = locator.revision or self._versions.get(locator.primary)
        if locator.kind != "memory_record" or not expected:
            return empty
        try:
            with open_readonly(self.path) as db:
                row = db.execute(
                    "SELECT * FROM memory_records WHERE id = ? AND status = 'active'", (locator.primary,)
                ).fetchone()
            if (
                row is None
                or _revision(row) != expected
                or not _eligible_metadata(row["metadata_json"], self._workspace, workspace_key=self._workspace_key)
            ):
                return empty
            return EvidenceResult(
                "memory", "historical_context", "Astra " + str(row["kind"]), (_redact_text(str(row["content"]))[:6000],)
            )
        except (FileNotFoundError, sqlite3.DatabaseError):
            return empty
