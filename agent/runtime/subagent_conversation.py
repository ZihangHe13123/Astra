"""Exclusive, durable message-and-state checkpoints for subagent recovery.

Audit transcripts are intentionally not consumed here. A checkpoint carries both
its message suffix and full recovery metadata in one JSONL record, so replay can
never advance the recovery cursor without the messages that justified it.
"""

from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
from typing import Any

from .instance_lock import InstanceLock
from .session_store import SessionStore


class ConversationCorrupt(ValueError):
    """A canonical journal is not safe to use as recovery evidence."""


def _validate_json(value: Any) -> None:
    """Reject coercions such as integer dictionary keys and non-finite floats."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("checkpoint numbers must be finite")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("checkpoint object keys must be strings")
            _validate_json(item)
        return
    raise TypeError(f"checkpoint contains non-JSON value: {type(value).__name__}")


def _validate_messages(messages: Any) -> None:
    if not isinstance(messages, list):
        raise ValueError("checkpoint messages must be a list")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {
            "system", "developer", "user", "assistant", "tool", "function",
        }:
            raise ValueError("checkpoint message requires a recognized role")
        content = message.get("content")
        if content is not None and not isinstance(content, (str, list)):
            raise ValueError("checkpoint message content must be text, a list, or null")
        if "tool_calls" in message:
            calls = message["tool_calls"]
            if not isinstance(calls, list) or not all(isinstance(call, dict) for call in calls):
                raise ValueError("checkpoint tool_calls must be a list of objects")
        if "tool_call_id" in message and not isinstance(message["tool_call_id"], str):
            raise ValueError("checkpoint tool_call_id must be a string")
    _validate_json(messages)


def apply_checkpoint(event: Any, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate and apply one record, also used by the ordinary session reader."""
    try:
        if not isinstance(event, dict) or event.get("type") != "subagent_checkpoint":
            raise ValueError("expected subagent_checkpoint record")
        if type(event.get("version")) is not int or event["version"] != 1:
            raise ValueError("unsupported subagent checkpoint version")
        index = event.get("index")
        if type(index) is not int or not 0 <= index <= len(messages):
            raise ValueError("checkpoint index would leave a hole in history")
        suffix = event.get("messages")
        state = event.get("state")
        if not isinstance(suffix, list):
            raise ValueError("checkpoint messages must be a list")
        _validate_messages(suffix)
        if not isinstance(state, dict):
            raise ValueError("checkpoint state must be an object")
        _validate_json(state)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ConversationCorrupt(str(exc)) from exc
    return messages[:index] + suffix, state


class SubagentConversation:
    """A journal owned by one worker for its full active and idle lifetime.

    ``load`` and ``save`` require an acquired lease. The initial read is strict;
    a partial last line is remembered and removed only before a later append.
    Subsequent saves compare against cached committed messages rather than
    repeatedly loading the journal from disk.
    """

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self._lock = InstanceLock(Path(str(store.jsonl_path) + ".lock"))
        self._acquired = False
        self._loaded = False
        self._messages: list[dict[str, Any]] = []
        self._state: dict[str, Any] = {}
        self._torn_offset: int | None = None
        self._needs_newline = False

    def acquire(self) -> None:
        if self._acquired:
            return
        self._lock.acquire()
        self._acquired = True
        self._loaded = False

    def close(self) -> None:
        self._loaded = False
        self._messages = []
        self._state = {}
        self._acquired = False
        self._lock.release()

    def _require_lease(self) -> None:
        if not self._acquired:
            raise RuntimeError("subagent conversation requires an acquired lease")

    def _read(self) -> None:
        messages: list[dict[str, Any]] = []
        state: dict[str, Any] = {}
        torn_offset: int | None = None
        needs_newline = False
        try:
            handle = self.store.jsonl_path.open("rb")
        except FileNotFoundError:
            handle = None
        if handle is not None:
            with handle:
                offset = 0
                for line_number, line in enumerate(handle, 1):
                    complete = line.endswith(b"\n")
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        if not complete:
                            torn_offset = offset
                            break
                        raise ConversationCorrupt(f"invalid complete record at line {line_number}") from exc
                    try:
                        messages, state = apply_checkpoint(event, messages)
                    except ConversationCorrupt as exc:
                        raise ConversationCorrupt(f"line {line_number}: {exc}") from exc
                    needs_newline = not complete
                    offset += len(line)
        self._messages = messages
        self._state = state
        self._torn_offset = torn_offset
        self._needs_newline = needs_newline
        self._loaded = True

    def load(self) -> dict[str, Any]:
        self._require_lease()
        if not self._loaded:
            self._read()
        return {"messages": deepcopy(self._messages), "state": deepcopy(self._state)}

    @staticmethod
    def _sync_directory(path: Path) -> None:
        # Windows does not support opening directories with os.open/fsync.
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def save(self, messages: list[dict[str, Any]], state: dict[str, Any]) -> None:
        self._require_lease()
        if not self._loaded:
            self._read()
        # Snapshot before comparisons/serialization so callers cannot mutate the
        # cached baseline by retaining references to their own messages/state.
        saved_messages = deepcopy(messages)
        saved_state = deepcopy(state)
        _validate_messages(saved_messages)
        if not isinstance(saved_state, dict):
            raise ValueError("checkpoint state must be an object")
        _validate_json(saved_state)
        index = 0
        for old, new in zip(self._messages, saved_messages):
            if old != new:
                break
            index += 1
        event = {"type": "subagent_checkpoint", "version": 1, "index": index,
                 "messages": saved_messages[index:], "state": saved_state}
        encoded = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        path = self.store.jsonl_path
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        try:
            if self._torn_offset is not None:
                with path.open("r+b") as handle:
                    handle.truncate(self._torn_offset)
                    handle.flush()
                    os.fsync(handle.fileno())
            with path.open("ab") as handle:
                if self._needs_newline:
                    handle.write(b"\n")
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if new_file:
                self._sync_directory(path.parent)
                # The lease may have just created the artifacts directory.
                self._sync_directory(path.parent.parent)
        except OSError:
            # The write may have partly succeeded. Reinspect under the same
            # lease before any subsequent append rather than using stale state.
            self._loaded = False
            raise
        self._messages = saved_messages
        self._state = saved_state
        self._torn_offset = None
        self._needs_newline = False
