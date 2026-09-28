"""Seed the archived proposal table the way the retired candidate queue wrote it."""

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from agent.runtime.learning import LearningStore


def stage_legacy_proposal(store: LearningStore, session_id: str, kind: str, payload: dict[str, Any],
                          reason: str, *, status: str = "pending") -> dict[str, Any]:
    proposal_id = f"lr_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    with store._connection() as db:
        db.execute(
            "INSERT INTO learning_proposals VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)",
            (proposal_id, session_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True),
             reason, status, now, now),
        )
    proposal = store.get(proposal_id)
    assert proposal is not None
    return proposal
