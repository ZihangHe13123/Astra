"""Task-bound questions; optional timed requests can outlive their tool call."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools.user_questions import UserQuestionCancelled, UserQuestionUnavailable, normalize_answers

MAX_REASON_CHARS = 500
_STALE_REASON = "Question request is no longer pending."
logger = logging.getLogger(__name__)


def _bounded_reason(value: object, *, fallback: str) -> str:
    text = "".join(char if char == "\t" or ord(char) >= 32 else " " for char in str(value or ""))
    return " ".join(text.split())[:MAX_REASON_CHARS].strip() or fallback


def validate_wait(mode: str, optional: bool, timeout_seconds: float) -> None:
    if mode not in {"blocking", "timed"}:
        raise ValueError("mode must be blocking or timed")
    if not isinstance(optional, bool) or (mode == "timed" and not optional):
        raise ValueError("timed mode requires optional=true; required input must stay blocking")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300):
        raise ValueError("timeout_seconds must be greater than 0 and at most 300")


@dataclass
class _PendingQuestion:
    request_id: str
    questions: list[dict[str, Any]]
    future: asyncio.Future[dict[str, Any]]
    identity: dict[str, str] = field(default_factory=dict)
    mode: str = "blocking"
    state: str = "waiting"
    editing_until: float = 0
    reason: str = ""


class UserQuestionBroker:
    """One outstanding question batch per backend, with session-bound late replies.

    Question contents and answers are never written to the lifecycle journal. On
    process restart, outstanding records explicitly expire instead of replaying an
    answer or granting authority. The ordinary session transcript holds tool output.
    """

    def __init__(self, emit: Callable[[dict[str, Any]], None], available: Callable[[], bool],
                 *, identity: Callable[[], dict[str, str]] | None = None,
                 journal_dir: Path | None = None) -> None:
        self._emit = emit
        self._available = available
        self._identity = identity or (lambda: {})
        self._pending: _PendingQuestion | None = None
        self._answers: list[dict[str, Any]] = []
        self._journal_dir = journal_dir
        self._recovered: set[str] = set()

    @property
    def pending_count(self) -> int:
        return int(self._pending is not None)

    async def ask(self, questions: list[dict[str, Any]], *, mode: str = "blocking",
                  optional: bool = False, timeout_seconds: float = 30,
                  call_id: str = "") -> dict[str, Any]:
        validate_wait(mode, optional, timeout_seconds)
        if not self._available():
            raise UserQuestionUnavailable("Human interaction is unavailable on this channel.")
        if self._pending is not None:
            raise UserQuestionUnavailable("Another question request is already pending. Continue independent work or wait for its answer.")
        identity = dict(self._identity())
        if call_id:
            identity["call_id"] = call_id
        pending = _PendingQuestion(uuid.uuid4().hex, questions,
                                   asyncio.get_running_loop().create_future(), identity, mode)
        self._pending = pending
        emitted = False
        detached = False
        try:
            self._record(pending.request_id, {**identity, "state": "waiting"})
            self._emit({"type": "user_question_request", "request_id": pending.request_id,
                        "questions": questions, "mode": mode, "state": "waiting", **identity})
            emitted = True
            if mode == "timed":
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                while not pending.future.done():
                    remaining = max(deadline, pending.editing_until) - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        pending.state = "pending"
                        self._record(pending.request_id, {**identity, "state": "pending"})
                        self._emit({"type": "user_question_pending", "request_id": pending.request_id,
                                    "state": "pending", **identity})
                        detached = True
                        return {"state": "pending", "request_id": pending.request_id, **identity,
                                "message": "No answer received. This is not approval. Continue only independent work; "
                                "the answer will arrive with the original question and IDs. "
                                "This question expires on backend restart, session change, or a new task."}
                    await asyncio.wait({pending.future}, timeout=min(remaining, 0.25))
            answer = await pending.future
            pending.state = "answered"
            return answer
        finally:
            if not detached:
                if self._pending is pending:
                    self._pending = None
                if not pending.future.done():
                    pending.future.cancel()
                # Retrieve cancellation exceptions even if the owning task was
                # cancelled at the same time as the broker was closed.
                elif not pending.future.cancelled():
                    pending.future.exception()
                if emitted:
                    self._finish(pending, pending.state if pending.state in {"answered", "expired"} else "cancelled")

    def resolve(self, request_id: str, answers: object) -> tuple[bool, str, bool]:
        pending = self._matching_pending(request_id)
        if pending is None:
            return False, _STALE_REASON, False
        if any(pending.identity.get(key, "") != self._identity().get(key, "") for key in ("session_id", "session_scope")):
            self.expire_other_sessions(self._identity().get("session_id", ""), self._identity().get("session_scope", ""))
            return False, _STALE_REASON, False
        try:
            normalized = normalize_answers(pending.questions, answers)
        except (TypeError, ValueError) as exc:
            return False, _bounded_reason(exc, fallback="Invalid question response."), True
        if pending.state == "pending":
            try:
                self._record(request_id, {**pending.identity, "state": "answer_queued"})
            except OSError:
                return False, "Could not save the question response. Please try again.", True
            self._answers.append({"request_id": request_id, **pending.identity,
                                  "questions": pending.questions, **normalized})
            self._pending = None
            pending.future.cancel()
            self._emit({"type": "user_question_resolved", "request_id": request_id, "state": "answered"})
        else:
            pending.future.set_result(normalized)
        return True, "", False

    def set_editing(self, request_id: str, editing: bool) -> bool:
        pending = self._matching_pending(request_id)
        if pending is None or not isinstance(editing, bool):
            return False
        # A lost blur/disconnect cannot hold the model forever. UIs renew while
        # focused; a pending question never becomes blocking again.
        pending.editing_until = asyncio.get_running_loop().time() + 60 if editing else 0
        return True

    def next_answer(self, *, session_id: str, task_id: str, busy: bool, session_scope: str = "") -> dict[str, Any] | None:
        return next((answer for answer in self._answers
                     if answer.get("session_id", "") == session_id and answer.get("session_scope", "") == session_scope
                     and (not busy or answer.get("task_id", "") == task_id)), None)

    def acknowledge_answer(self, request_id: str) -> None:
        for answer in self._answers:
            if answer["request_id"] == request_id:
                self._record_terminal(request_id, {key: answer[key] for key in ("session_id", "session_scope", "task_id", "call_id") if key in answer}
                             | {"state": "answered"})
        self._answers = [answer for answer in self._answers if answer["request_id"] != request_id]

    def cancel(self, request_id: str, reason: str, *, expired: bool = False) -> tuple[bool, str]:
        pending = self._matching_pending(request_id)
        if pending is None:
            return False, _STALE_REASON
        pending.reason = _bounded_reason(reason, fallback="Question request was cancelled.")
        state = "expired" if expired else "cancelled"
        if pending.state == "pending":
            self._pending = None
            pending.future.cancel()
            self._finish(pending, state)
        else:
            pending.state = state
            pending.future.set_exception(UserQuestionCancelled(pending.reason))
        return True, ""

    def expire_other_sessions(self, session_id: str, session_scope: str = "") -> None:
        pending = self._pending
        if pending and (pending.identity.get("session_id", "") != session_id or pending.identity.get("session_scope", "") != session_scope):
            self.cancel(pending.request_id, "Question expired because its backend changed session.", expired=True)
        for answer in list(self._answers):
            if answer.get("session_id", "") != session_id or answer.get("session_scope", "") != session_scope:
                self._record_terminal(answer["request_id"], {**{key: answer[key] for key in ("session_id", "session_scope", "task_id", "call_id") if key in answer},
                                                    "state": "expired", "reason": "Queued answer expired on session change."})
                self._emit({"type": "user_question_resolved", "request_id": answer["request_id"], "state": "expired",
                            "reason": "Queued answer expired on session change."})
                self._answers.remove(answer)

    @property
    def queued_request_ids(self) -> list[str]:
        return [answer["request_id"] for answer in self._answers]

    def supersede(self, reason: str) -> None:
        """A new user task cannot inherit a previous task's unanswered choices."""
        if self._pending is not None:
            self.cancel(self._pending.request_id, reason, expired=True)
        for answer in self._answers:
            identity = {key: answer[key] for key in ("session_id", "session_scope", "task_id", "call_id") if key in answer}
            self._record_terminal(answer["request_id"], {**identity, "state": "expired", "reason": reason})
            self._emit({"type": "user_question_resolved", "request_id": answer["request_id"],
                        "state": "expired", "reason": reason})
        self._answers.clear()

    def close(self, reason: str) -> None:
        if self._pending is not None:
            self.cancel(self._pending.request_id, reason, expired=self._pending.mode == "timed")

    def _finish(self, pending: _PendingQuestion, state: str) -> None:
        self._record_terminal(pending.request_id, {**pending.identity, "state": state, "reason": pending.reason})
        event = {"type": "user_question_resolved", "request_id": pending.request_id, "state": state}
        if state == "expired":
            event["reason"] = pending.reason
        self._emit(event)

    def _record_terminal(self, request_id: str, record: dict[str, Any]) -> None:
        try:
            self._record(request_id, record)
        except OSError:
            # A stale journal entry will expire on restart. It must never keep
            # an in-memory question active or prevent saving the owning task.
            logger.warning("Could not update question lifecycle state for %s", request_id)

    def _record(self, request_id: str, record: dict[str, Any]) -> None:
        if self._journal_dir is None:
            return
        self._journal_dir.mkdir(parents=True, exist_ok=True)
        target = self._journal_dir / f"{request_id}.json"
        if record.get("state") in {"answered", "cancelled"}:
            target.unlink(missing_ok=True)
            return
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"request_id": request_id, "pid": os.getpid(), **record}), encoding="utf-8")
        temporary.replace(target)

    def recover_expired(self, session_id: str, session_scope: str = "") -> None:
        """Show durable expiry notices for this session after a backend restart."""
        if self._journal_dir is None or not self._journal_dir.exists():
            return
        for path in sorted(self._journal_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                request_id = record["request_id"]
                if not isinstance(request_id, str) or path.name != f"{request_id}.json" or not request_id.isalnum():
                    continue
                if (record.get("session_id") != session_id or record.get("session_scope", "") != session_scope or request_id in self._recovered
                        or record.get("state") not in {"waiting", "pending", "answer_queued", "expired"}):
                    continue
                if record["state"] != "expired":
                    try:
                        os.kill(int(record["pid"]), 0)
                    except ProcessLookupError:
                        pass
                    else:
                        continue
                    record.update(state="expired", reason="Question expired after backend restart. Ask again if still needed.")
                    self._record(request_id, record)
                self._recovered.add(request_id)
                self._emit({"type": "user_question_resolved", "request_id": request_id, "state": "expired",
                            "reason": record.get("reason", "Question expired after backend restart.")})
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _matching_pending(self, request_id: str) -> _PendingQuestion | None:
        pending = self._pending
        if pending is None or pending.request_id != request_id or pending.future.done():
            return None
        return pending
