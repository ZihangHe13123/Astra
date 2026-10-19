"""Opt-in retention of tool-authored evidence; never interpret conversation text."""

from __future__ import annotations

import os
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

from .hooks import HookRegistry, HookReject
from .memory_provider import MemoryProvider

if TYPE_CHECKING:
    from .memory import MemoryStore


@dataclass(frozen=True)
class ToolEvidence:
    tool_name: str
    content: str
    call_id: str = ""


@dataclass(frozen=True)
class RetentionOutcome:
    session_id: str
    decision: str
    reason: str
    retained_ids: tuple[str, ...] = ()
    confirmed_ids: tuple[str, ...] = ()
    superseded_ids: tuple[str, ...] = ()

    def format(self) -> str:
        lines = [
            "Last automatic retention decision:",
            f"Decision: {self.decision}",
            f"Reason: {self.reason}",
            f"Retained: {', '.join('#' + item[:12] for item in self.retained_ids) if self.retained_ids else '(none)'}",
            f"Confirmed: {', '.join('#' + item[:12] for item in self.confirmed_ids) if self.confirmed_ids else '(none)'}",
            f"Superseded: {', '.join('#' + item[:12] for item in self.superseded_ids) if self.superseded_ids else '(none)'}",
        ]
        return "\n".join(lines)

class MemoryRetainer:
    """Retain only successful, attributable, explicitly opted-in tool evidence.

    Personal facts and corrections are authored by the main model through the
    memory tools. Legacy mode names remain loadable but cannot enable sentence
    extraction, fuzzy confirmation, or automatic replacement of old records.
    """

    def __init__(self, provider: MemoryProvider, store: "MemoryStore",
                 hooks: HookRegistry | None = None, *, mode: str | None = None):
        self.provider = provider
        self.store = store
        self.hooks = hooks
        configured = str(mode or os.getenv("MEMORY_RETENTION_MODE", "tool-evidence")).strip().lower()
        if configured not in {"tool-evidence", "conservative", "evolving"}:
            raise ValueError(f"Unknown memory retention mode: {configured}")
        self.mode = "tool-evidence"
        self.enabled = os.getenv("MEMORY_AUTO_RETAIN", "0").strip().lower() in {"1", "true", "yes", "on"}
        self._last_outcome: dict[str, RetentionOutcome] = {}

    def _already_retained(self, record: dict) -> bool:
        repo = self.store.record_store
        with repo._lock, repo._connection() as db:
            rows = db.execute(
                "SELECT metadata_json FROM memory_records WHERE kind='observation' AND content=? "
                "AND source_session_id=? AND source_message_id=?",
                (record["content"], record["source_session_id"], record["source_message_id"]),
            ).fetchall()
        source = record["metadata"]
        return any(all(json.loads(row["metadata_json"]).get(key) == source.get(key)
                       for key in ("tool_name", "tool_call_id")) for row in rows)

    async def retain_turn(self, user_text: str, *, session_id: str, message_id: str,
                          tool_evidence: Iterable[ToolEvidence] = ()) -> RetentionOutcome:
        # user_text is not an input to a semantic classifier. In particular, "remember", quotations and "change to"
        # must never authorize a background write or supersede operation.
        retained: list[str] = []
        outcome = RetentionOutcome(session_id, "skipped", "no opted-in tool evidence")
        if not self.enabled:
            outcome = RetentionOutcome(session_id, "skipped", "disabled by MEMORY_AUTO_RETAIN")
        else:
            try:
                for evidence in list(tool_evidence)[:2]:
                    content = str(evidence.content).strip()
                    if not content or len(content) > 1000 or not evidence.call_id or not session_id or not message_id:
                        continue
                    record = {
                        "kind": "observation", "content": content,
                        "source_session_id": session_id, "source_message_id": message_id,
                        "confidence": 0.8, "salience": 0.7,
                        "tags": ("tool-evidence", evidence.tool_name),
                        "valid_until": (datetime.now(timezone.utc) + timedelta(days=90)).isoformat(),
                        "metadata": {"retained_by": "opt-in-tool-evidence", "tool_name": evidence.tool_name,
                                     "tool_call_id": evidence.call_id, "maturity": "provisional", "ttl_days": 90},
                    }
                    if self.hooks is not None:
                        record = self.hooks.dispatch_memory_retain(dict(record))
                        if not isinstance(record, dict):
                            raise ValueError("memory_retain hook must return a record mapping")
                    # Exact source replay is idempotent, even after another
                    # observation arrived or the original was expired/forgotten.
                    if self._already_retained(record):
                        continue
                    saved = await self.provider.retain(**record)
                    retained.append(saved.record_id)
                if retained:
                    outcome = RetentionOutcome(session_id, "retained", "opted-in tool evidence recorded",
                                               retained_ids=tuple(retained))
            except HookReject as exc:
                outcome = RetentionOutcome(session_id, "rejected", f"memory_retain hook rejected evidence: {exc.reason}",
                                           retained_ids=tuple(retained))
            except (OSError, TypeError, ValueError) as exc:
                outcome = RetentionOutcome(session_id, "rejected", f"retention validation rejected evidence: {exc}",
                                           retained_ids=tuple(retained))
        self._last_outcome[session_id] = outcome
        return outcome

    def last_outcome(self, session_id: str) -> RetentionOutcome | None:
        return self._last_outcome.get(session_id)
