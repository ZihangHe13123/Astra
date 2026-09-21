"""Receipts prevent replay during one backend lifetime; crash returns unknown."""

from __future__ import annotations

import hashlib
import json
import re


class Submissions:
    def __init__(self):
        self.records: dict[str, tuple[str, dict]] = {}

    def begin(self, command: dict) -> dict | None:
        sid = command.get("submission_id")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
            return {"type": "message_rejected", "submission_id": "", "code": "invalid_submission", "retryable": False}
        digest = hashlib.sha256(json.dumps(command, sort_keys=True).encode()).hexdigest()
        if sid in self.records:
            previous, result = self.records[sid]
            if previous != digest:
                return {"type": "message_rejected", "submission_id": sid, "code": "submission_payload_conflict", "retryable": False}
            return dict(result)
        self.records[sid] = (digest, {"type": "submission_status", "submission_id": sid, "status": "pending"})
        return None

    def finish(self, sid: str, accepted: bool) -> dict:
        result: dict = {"type": "message_accepted" if accepted else "message_rejected", "submission_id": sid}
        if not accepted:
            result.update(code="not_started", retryable=True)
        self.records[sid] = (self.records[sid][0], result)
        return result

    def status(self, sid: str) -> dict:
        result = self.records.get(sid, ("", {}))[1]
        status = "accepted" if result.get("type") == "message_accepted" else "rejected" if result.get("type") == "message_rejected" else result.get("status", "unknown")
        return {"type": "submission_status", "submission_id": sid, "status": status}
