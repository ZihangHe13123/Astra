"""Append-only session storage with legacy JSON compatibility."""

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any


DEFAULT_SESSION_DATA = {
    "system_prompt": "",
    "persona_id": "",
    "persona_definition_version": 0,
    "persona_state_revision": 0,
    "persona_state": {},
    "messages": [],
    "show_reasoning": True,
    "total_prompt_tokens": 0,
    "total_completion_tokens": 0,
    "total_cache_hit_tokens": 0,
    "total_cache_miss_tokens": 0,
    "last_prompt_tokens": 0,
}


class SessionStore:
    def __init__(self, path: str | Path):
        self.legacy_path = Path(path)
        if self.legacy_path.suffix != ".json":
            self.legacy_path = self.legacy_path.with_suffix(".json")
        self.jsonl_path = self.legacy_path.with_suffix(".jsonl")
        self.snapshot_path = self.legacy_path.with_name(f"{self.legacy_path.stem}.snapshot.json")
        self.header_path = self.legacy_path.with_name(f"{self.legacy_path.stem}.header.json")
        self.diagnostic_path = (
            self.legacy_path.parent
            / ".artifacts"
            / f"{self.legacy_path.stem}.tool-failures.jsonl"
        )
        self.context_index_feedback_path = (
            self.legacy_path.parent
            / ".artifacts"
            / f"{self.legacy_path.stem}.context-index-feedback.jsonl"
        )
        self.hindsight_sync_path = (
            self.legacy_path.parent
            / ".artifacts"
            / f"{self.legacy_path.stem}.hindsight-sync.json"
        )
        self.subagent_path = (
            self.legacy_path.parent
            / ".artifacts"
            / f"{self.legacy_path.stem}.subagents.jsonl"
        )
        self.raw_tool_calls_path = (
            self.legacy_path.parent
            / ".artifacts"
            / f"{self.legacy_path.stem}.raw-tool-calls.jsonl"
        )
        self.approval_audit_path = (
            self.legacy_path.parent
            / ".artifacts"
            / f"{self.legacy_path.stem}.approvals.jsonl"
        )
        self._run_id = ""
        self._closed = False

    @property
    def exists(self) -> bool:
        return self.legacy_path.exists() or self.jsonl_path.exists() or self.snapshot_path.exists()

    @property
    def appshot_media(self):
        from .appshot_media import AppshotMediaStore
        return AppshotMediaStore(self.legacy_path)

    def related_paths(self) -> list[Path]:
        return [
            self.legacy_path,
            self.jsonl_path,
            self.snapshot_path,
            self.header_path,
            self.diagnostic_path,
            self.context_index_feedback_path,
            self.hindsight_sync_path,
            self.subagent_path,
            self.raw_tool_calls_path,
            self.approval_audit_path,
        ]

    def append_diagnostic(self, event: dict[str, Any]) -> Path:
        """Persist non-canonical provider/tool diagnostics for audit."""
        self.diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.diagnostic_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return self.diagnostic_path

    def append_context_index_feedback(self, event: dict[str, Any]) -> Path:
        """Append one model-invisible Context Index feedback event."""
        payload = dict(event)
        payload["recorded_at"] = time.time()
        self.context_index_feedback_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.context_index_feedback_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return self.context_index_feedback_path

    def append_subagent_event(self, event: dict[str, Any]) -> Path:
        """Append one durable, non-canonical subagent transcript event."""
        payload = dict(event)
        payload.setdefault("recorded_at", time.time())
        self.subagent_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.subagent_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return self.subagent_path

    def append_approval_audit(self, event: dict[str, Any]) -> Path:
        """Append one model-invisible approval lifecycle record."""
        payload = dict(event)
        payload.setdefault("recorded_at", time.time())
        self.approval_audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.approval_audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return self.approval_audit_path

    def append_raw_tool_call(self, event: dict[str, Any]) -> Path:
        """Append a model-invisible call with caller-sanitized arguments.

        The legacy raw-tool-calls filename is retained. Records declare their
        argument persistence policy; request-local values are not raw wire data.
        """
        payload = dict(event)
        payload.setdefault("recorded_at", time.time())
        self.raw_tool_calls_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.raw_tool_calls_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return self.raw_tool_calls_path

    def load_subagent_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        try:
            with open(self.subagent_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        events.append(event)
        except OSError:
            pass
        return events

    def load_hindsight_sync_state(self) -> dict[str, Any]:
        """Load the durable per-session Hindsight delivery cursor."""
        return self._read_json(self.hindsight_sync_path) or {}

    def save_hindsight_sync_state(self, state: dict[str, Any]) -> Path:
        """Atomically persist the per-session Hindsight delivery cursor."""
        self.hindsight_sync_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.hindsight_sync_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.hindsight_sync_path)
        return self.hindsight_sync_path

    def load(self, *, readonly: bool = False) -> dict[str, Any]:
        if not readonly:
            self.recover_interrupted()
        data = dict(DEFAULT_SESSION_DATA)
        data["messages"] = []

        if self.snapshot_path.exists():
            loaded = self._read_json(self.snapshot_path)
            if loaded:
                data.update(loaded)
                data["messages"] = list(loaded.get("messages", []))
        elif self.legacy_path.exists():
            loaded = self._read_json(self.legacy_path)
            if loaded:
                data.update(loaded)
                data["messages"] = list(loaded.get("messages", []))

        if self.jsonl_path.exists():
            self._replay_jsonl(data)

        header = self._read_json(self.header_path)
        if header:
            data.update(header)

        if "work_prompt" in data:
            # Old snapshots can outlive a migrated header. Retire the original
            # projection once, without resetting a new header on every load.
            if not header or "work_prompt" in header:
                data["system_prompt_projection"] = {}
            data.pop("work_prompt", None)

        return data

    def save(self, data: dict[str, Any], append_from: int = 0, snapshot: bool = False) -> None:
        self.legacy_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_started()
        messages = list(data.get("messages", []))
        append_from = max(0, min(append_from, len(messages)))

        # If history shrank or was rewritten, append a replace record so replay is deterministic.
        existing_count = len(self.load().get("messages", [])) if self.exists else 0
        replace = existing_count > append_from

        self._write_header(self._state_without_messages(data))
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            if replace:
                f.write(json.dumps({"type": "replace", "messages": messages}, ensure_ascii=False) + "\n")
            else:
                for idx, msg in enumerate(messages[append_from:], start=append_from):
                    f.write(json.dumps({"type": "message", "index": idx, "message": msg}, ensure_ascii=False) + "\n")

        if snapshot or not self.snapshot_path.exists():
            self.save_snapshot(data)

    def save_snapshot(self, data: dict[str, Any]) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshot_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.snapshot_path)

    def _write_header(self, header: dict[str, Any]) -> None:
        """Persist mutable session metadata outside the append-only message stream."""
        self.header_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.header_path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(header, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self.header_path)

    def _append_lifecycle(self, event_type: str, **payload: Any) -> None:
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        event = {"type": event_type, "recorded_at": time.time(), **payload}
        with open(self.jsonl_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def lifecycle_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        try:
            with open(self.jsonl_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("type") in {
                        "session_started", "session_ended", "session_interrupted",
                    }:
                        events.append(event)
        except OSError:
            pass
        return events

    @staticmethod
    def _process_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                # Access denied means the process exists but cannot be queried
                # with the current token. Prefer a missed recovery event over a
                # false crash marker for a live protected process.
                return ctypes.get_last_error() == 5
            kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def recover_interrupted(self) -> dict[str, Any] | None:
        events = self.lifecycle_events()
        if not events or events[-1].get("type") != "session_started":
            return None
        started = events[-1]
        pid = int(started.get("pid") or 0)
        if self._process_alive(pid):
            return None
        recovered = {
            "run_id": str(started.get("run_id") or ""),
            "previous_pid": pid,
            "reason": "process_exit_without_session_end",
        }
        self._append_lifecycle("session_interrupted", **recovered)
        return recovered

    def ensure_started(self) -> str:
        if self._run_id or self._closed:
            return self._run_id
        self.recover_interrupted()
        self._run_id = uuid.uuid4().hex
        self._append_lifecycle("session_started", run_id=self._run_id, pid=os.getpid())
        return self._run_id

    def begin(self) -> None:
        """Allow a new lifecycle explicitly; late saves alone cannot reopen one."""
        self._closed = False

    def record_end(self, reason: str) -> None:
        self._closed = True
        if not self._run_id:
            return
        self._append_lifecycle(
            "session_ended", run_id=self._run_id, pid=os.getpid(), reason=str(reason),
        )
        self._run_id = ""

    def markdown(self, name: str) -> str:
        """Render the complete existing export without mutating session storage."""
        data = self.load(readonly=True)
        lines = [f"# Session: {name}", ""]
        for msg in data.get("messages", []):
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if not content and msg.get("tool_calls"):
                content = "[tool calls]"
            lines.extend([f"## {role}", "", str(content), ""])
        subagent_events = self.load_subagent_events()
        if subagent_events:
            lines.extend(["# Subagent transcripts", ""])
            for event in subagent_events:
                event_type = str(event.get("type") or "event")
                process_id = str(event.get("process_id") or "unknown")
                lines.extend([f"## {event_type} · {process_id}", ""])
                for key in (
                    "worker_type", "goal", "turn", "status", "content",
                    "tool_calls", "tool_name", "arguments", "output", "result", "error",
                ):
                    value = event.get(key)
                    if value not in (None, "", [], {}):
                        lines.extend([f"**{key}:**", "", str(value), ""])
        return "\n".join(lines).rstrip() + "\n"

    def export_markdown(self, name: str, out: Path) -> Path:
        markdown = self.markdown(name)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        return out

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _state_without_messages(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "system_prompt": data.get("system_prompt", ""),
            "system_prompt_migration": data.get("system_prompt_migration", {}),
            "system_prompt_projection": data.get("system_prompt_projection", {}),
            "runtime_context_projection": data.get("runtime_context_projection", {}),
            "persona_id": data.get("persona_id", ""),
            "persona_definition_version": data.get("persona_definition_version", 0),
            "persona_state_revision": data.get("persona_state_revision", 0),
            "persona_state": data.get("persona_state", {}),
            "show_reasoning": data.get("show_reasoning", True),
            "total_prompt_tokens": data.get("total_prompt_tokens", 0),
            "total_completion_tokens": data.get("total_completion_tokens", 0),
            "total_cache_hit_tokens": data.get("total_cache_hit_tokens", 0),
            "total_cache_miss_tokens": data.get("total_cache_miss_tokens", 0),
            "last_prompt_tokens": data.get("last_prompt_tokens", 0),
        }

    def _replay_jsonl(self, data: dict[str, Any]) -> None:
        messages = list(data.get("messages", []))
        try:
            with open(self.jsonl_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type")
                    if event_type == "state":
                        data.update(event.get("state", {}))
                    elif event_type == "replace":
                        messages = list(event.get("messages", []))
                    elif event_type == "message":
                        idx = event.get("index", len(messages))
                        msg = event.get("message")
                        if not isinstance(msg, dict):
                            continue
                        if isinstance(idx, int) and idx < len(messages):
                            continue
                        messages.append(msg)
        except OSError:
            return
        data["messages"] = messages
