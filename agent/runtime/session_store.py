"""Append-only session storage with legacy JSON compatibility."""

import json
import mmap
import os
import re
import sys
import time
import uuid
from collections.abc import Iterable, Iterator
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

_LIFECYCLE_TYPES = frozenset({"session_started", "session_ended", "session_interrupted"})
_LIFECYCLE_MARKERS = (b"session_started", b"session_ended", b"session_interrupted")
_MESSAGE_PREFIXES = (b'{"type": "message"', b'{"type":"message"')
_REPLACE_PREFIXES = (b'{"type": "replace"', b'{"type":"replace"')
_STATE_PREFIXES = (b'{"type": "state"', b'{"type":"state"')


class SessionStore:
    # Use read-only mmap + reverse-search for journals at or above 256 KiB so
    # both append-only and multi-replace journals avoid redundant disk reads.
    _MMAP_MIN_BYTES = 256 * 1024

    @classmethod
    def for_subagent(cls, session_path: str | Path, key: str, *, branch_id: str | None = None) -> "SessionStore":
        """Return an isolated canonical journal for one worker identity."""
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", key):
            raise ValueError("subagent identity must be 1-128 safe ASCII filename characters")
        parent = cls(session_path, branch_id=branch_id)
        return cls(parent.legacy_path.parent / ".artifacts" / f"{parent.legacy_path.stem}.{key}.conv.json")

    def __init__(self, path: str | Path, branch_id: str | None = None):
        from .conversation_branches import ConversationBranches, branch_scope
        self.logical_path = Path(path)
        if self.logical_path.suffix != ".json":
            self.logical_path = self.logical_path.with_suffix(".json")
        self._branches = ConversationBranches(self.logical_path)
        # Resolve once: an in-flight save remains attached to its original head.
        self.branch_id, self.legacy_path = self._branches.resolve(branch_id)
        self.session_scope = branch_scope(self.logical_path, self.branch_id)
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
        self._cached_signature: tuple[
            tuple[int, int, int] | None,
            tuple[int, int, int] | None,
            tuple[int, int, int] | None,
        ] | None = None
        self._cached_message_count: int | None = None
        self._recovered_jsonl_signature: tuple[int, int, int] | None = None
        self._last_start_bytes: int | None = None
        if self.branch_id != "main":
            for related in self.related_paths(include_branches=False):
                self._branches._safe(related)

    @property
    def exists(self) -> bool:
        return self.legacy_path.exists() or self.jsonl_path.exists() or self.snapshot_path.exists()

    @property
    def appshot_media(self):
        from .appshot_media import AppshotMediaStore
        return AppshotMediaStore(self.logical_path)

    def related_paths(self, *, include_branches: bool = True) -> list[Path]:
        paths = [
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
        if include_branches:
            if self.branch_id != "main":
                paths.extend(SessionStore(self.logical_path, branch_id="main").related_paths(include_branches=False))
            artifacts = self.logical_path.parent / ".artifacts"
            if artifacts.exists():
                self._branches._safe(artifacts)
                pattern = re.compile(re.escape(self.logical_path.stem) + r"\.([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.conv\.")
                for path in artifacts.iterdir():
                    match = pattern.match(path.name)
                    if not match:
                        continue
                    self._branches._safe(path)
                    if path.is_file():
                        paths.append(path)
                        child = SessionStore(artifacts / f"{self.logical_path.stem}.{match[1]}.conv.json", branch_id="main")
                        paths.extend(child.related_paths(include_branches=False))
            paths.extend(self._branches.all_related_paths())
            for path in paths:
                self._branches._safe(path)
        return list(dict.fromkeys(paths))

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

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    @staticmethod
    def _same_file_grew_by(
        before: tuple[int, int, int] | None,
        after: tuple[int, int, int] | None,
        delta: int,
    ) -> bool:
        if after is None:
            return False
        if before is None:
            return after[1] == delta
        if before[0] and after[0] and before[0] != after[0]:
            return False
        return after[1] == before[1] + delta

    def _storage_signature(
        self,
    ) -> tuple[tuple[int, int, int] | None, tuple[int, int, int] | None, tuple[int, int, int] | None]:
        return (
            self._file_signature(self.jsonl_path),
            self._file_signature(self.legacy_path),
            self._file_signature(self.snapshot_path),
        )

    def _invalidate_message_count_cache(self) -> None:
        self._cached_message_count = None
        self._cached_signature = None

    def _read_json_checked(self, path: Path) -> tuple[dict[str, Any] | None, bool]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f), True
        except json.JSONDecodeError:
            return None, True
        except OSError:
            return None, False

    def load(self, *, readonly: bool = False) -> dict[str, Any]:
        custom_recover = type(self).recover_interrupted is not SessionStore.recover_interrupted
        if not readonly and custom_recover:
            self.recover_interrupted()
        sig_before = self._storage_signature()
        complete = True
        data = dict(DEFAULT_SESSION_DATA)
        data["messages"] = []

        if sig_before[2] is not None or self.snapshot_path.exists():
            loaded, read_ok = self._read_json_checked(self.snapshot_path)
            if not read_ok:
                complete = False
            elif loaded:
                data.update(loaded)
                data["messages"] = list(loaded.get("messages", []))
        elif sig_before[1] is not None or self.legacy_path.exists():
            loaded, read_ok = self._read_json_checked(self.legacy_path)
            if not read_ok:
                complete = False
            elif loaded:
                data.update(loaded)
                data["messages"] = list(loaded.get("messages", []))

        last_lifecycle: dict[str, Any] | None = None
        track_lifecycle = not readonly and not custom_recover
        jsonl_existed = sig_before[0] is not None or self.jsonl_path.exists()
        if jsonl_existed:
            last_lifecycle, replay_ok = self._replay_jsonl_with_lifecycle(
                data, track_lifecycle=track_lifecycle
            )
            if not replay_ok:
                complete = False

        sig_after_replay = self._storage_signature()
        stable_read = complete and sig_after_replay == sig_before
        if stable_read:
            self._cached_message_count = len(data["messages"])
            self._cached_signature = sig_after_replay
        else:
            self._invalidate_message_count_cache()

        if track_lifecycle:
            if jsonl_existed and complete:
                _recovered, live_start, checked_sig = self._recover_from_lifecycle_event_state(
                    last_lifecycle,
                    checked_jsonl_sig=sig_after_replay[0] if stable_read else None,
                )
                self._recovered_jsonl_signature = None if live_start else checked_sig
            else:
                self._recovered_jsonl_signature = None

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
        pre_start_sig = self._storage_signature()
        self.ensure_started()
        messages = list(data.get("messages", []))
        append_from = max(0, min(append_from, len(messages)))

        # If history shrank or was rewritten, append a replace record so replay is deterministic.
        post_start_sig = self._storage_signature()
        baseline_sig: tuple[
            tuple[int, int, int] | None,
            tuple[int, int, int] | None,
            tuple[int, int, int] | None,
        ] | None
        if (
            pre_start_sig == (None, None, None)
            and self._last_start_bytes is not None
            and self._same_file_grew_by(None, post_start_sig[0], self._last_start_bytes)
            and post_start_sig[1:] == (None, None)
        ):
            existing_count = 0
            baseline_sig = post_start_sig
        elif (
            self._cached_message_count is not None
            and self._cached_signature is not None
            and post_start_sig == self._cached_signature
        ):
            existing_count = self._cached_message_count
            baseline_sig = post_start_sig
        else:
            existing_count = len(self.load(readonly=True).get("messages", []))
            baseline_sig = self._cached_signature
        replace = existing_count > append_from

        self._write_header(self._state_without_messages(data))
        if replace:
            written = self._append_jsonl([json.dumps({"type": "replace", "messages": messages}, ensure_ascii=False)])
        else:
            written = self._append_jsonl(
                json.dumps({"type": "message", "index": idx, "message": msg}, ensure_ascii=False)
                for idx, msg in enumerate(messages[append_from:], start=append_from)
            )

        wrote_snapshot = snapshot or not self.snapshot_path.exists()
        if wrote_snapshot:
            self.save_snapshot(data)
            return

        if (
            baseline_sig is not None
            and (
                replace
                or (
                    existing_count == append_from
                    and all(isinstance(msg, dict) for msg in messages[append_from:])
                )
            )
        ):
            post_write_sig = self._storage_signature()
            jsonl_exact = self._same_file_grew_by(baseline_sig[0], post_write_sig[0], written)
            legacy_exact = post_write_sig[1] == baseline_sig[1]
            snap_exact = post_write_sig[2] == baseline_sig[2]
            if jsonl_exact and legacy_exact and snap_exact:
                self._cached_message_count = len(messages)
                self._cached_signature = post_write_sig
            else:
                self._invalidate_message_count_cache()
        else:
            self._invalidate_message_count_cache()

    def save_snapshot(self, data: dict[str, Any]) -> None:
        self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.snapshot_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.snapshot_path)
        self._invalidate_message_count_cache()

    def _write_header(self, header: dict[str, Any]) -> None:
        """Persist mutable session metadata outside the append-only message stream."""
        self.header_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.header_path.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(header, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self.header_path)

    def _append_lifecycle(
        self,
        event_type: str,
        *,
        expected_jsonl_sig: tuple[int, int, int] | None = None,
        **payload: Any,
    ) -> tuple[int, tuple[int, int, int] | None]:
        pre_sig = self._storage_signature()
        cache_valid = (
            self._cached_message_count is not None
            and self._cached_signature is not None
            and pre_sig == self._cached_signature
        )
        event = {"type": event_type, "recorded_at": time.time(), **payload}
        written = self._append_jsonl([json.dumps(event, ensure_ascii=False, default=str)])
        post_sig = self._storage_signature()
        if cache_valid:
            if self._same_file_grew_by(pre_sig[0], post_sig[0], written) and post_sig[1:] == pre_sig[1:]:
                self._cached_signature = post_sig
            else:
                self._invalidate_message_count_cache()
        verified_jsonl_sig = (
            post_sig[0]
            if (
                expected_jsonl_sig is not None
                and pre_sig[0] == expected_jsonl_sig
                and self._same_file_grew_by(expected_jsonl_sig, post_sig[0], written)
            )
            else None
        )
        return written, verified_jsonl_sig

    def _append_jsonl(self, records: Iterable[str]) -> int:
        """Keep every new record separate, preserving any torn tail as evidence."""
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        bytes_written = 0
        with open(self.jsonl_path, "a+b") as handle:
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.write(b"\n")
                    bytes_written += 1
            for record in records:
                encoded = record.encode("utf-8") + b"\n"
                handle.write(encoded)
                bytes_written += len(encoded)
        return bytes_written

    def _jsonl_events(self) -> Iterator[dict[str, Any]]:
        """Read complete records without changing a damaged journal."""
        try:
            with open(self.jsonl_path, "rb") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(event, dict):
                        yield event
        except OSError:
            return

    def _read_lifecycle_events_checked(self) -> tuple[list[dict[str, Any]], bool]:
        events: list[dict[str, Any]] = []
        try:
            with open(self.jsonl_path, "rb") as handle:
                for line in handle:
                    stripped = line.lstrip()
                    if (
                        not stripped
                        or stripped.startswith(_MESSAGE_PREFIXES)
                        or stripped.startswith(_REPLACE_PREFIXES)
                        or stripped.startswith(_STATE_PREFIXES)
                    ):
                        continue
                    if not any(marker in stripped for marker in _LIFECYCLE_MARKERS):
                        continue
                    try:
                        event = json.loads(stripped)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if isinstance(event, dict) and event.get("type") in _LIFECYCLE_TYPES:
                        events.append(event)
        except FileNotFoundError:
            return [], True
        except OSError:
            return [], False
        return events, True

    def lifecycle_events(self) -> list[dict[str, Any]]:
        events, _ok = self._read_lifecycle_events_checked()
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

    def _recover_from_lifecycle_event_state(
        self,
        last_event: dict[str, Any] | None,
        *,
        checked_jsonl_sig: tuple[int, int, int] | None,
    ) -> tuple[dict[str, Any] | None, bool, tuple[int, int, int] | None]:
        """Recover an unended run and report (recovered, live_start, verified_post_sig)."""
        if not last_event or last_event.get("type") != "session_started":
            return None, False, checked_jsonl_sig
        pid = int(last_event.get("pid") or 0)
        if self._process_alive(pid):
            return None, True, None
        recovered = {
            "run_id": str(last_event.get("run_id") or ""),
            "previous_pid": pid,
            "reason": "process_exit_without_session_end",
        }
        _written, verified_sig = self._append_lifecycle(
            "session_interrupted",
            expected_jsonl_sig=checked_jsonl_sig,
            **recovered,
        )
        return recovered, False, verified_sig

    def recover_interrupted(self) -> dict[str, Any] | None:
        sig_before = self._file_signature(self.jsonl_path)
        events, read_ok = self._read_lifecycle_events_checked()
        sig_after = self._file_signature(self.jsonl_path)
        stable_sig = sig_after if (read_ok and sig_after == sig_before) else None
        recovered, live_start, verified_sig = self._recover_from_lifecycle_event_state(
            events[-1] if events else None,
            checked_jsonl_sig=stable_sig,
        )
        self._recovered_jsonl_signature = None if live_start else verified_sig
        return recovered

    def ensure_started(self) -> str:
        if self._run_id or self._closed:
            return self._run_id
        custom_recover = type(self).recover_interrupted is not SessionStore.recover_interrupted
        if (
            custom_recover
            or self._recovered_jsonl_signature is None
            or self._file_signature(self.jsonl_path) != self._recovered_jsonl_signature
        ):
            self.recover_interrupted()
        self._run_id = uuid.uuid4().hex
        self._last_start_bytes, _verified = self._append_lifecycle(
            "session_started", run_id=self._run_id, pid=os.getpid()
        )
        self._recovered_jsonl_signature = None
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
            "standing_memory_projection": data.get("standing_memory_projection", {}),
            "guidance_snapshot": data.get("guidance_snapshot", {}),
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
            # A summary written while the conversation sat idle, not applied yet (AgentContext).
            "idle_summary": data.get("idle_summary"),
        }

    @staticmethod
    def _apply_replay_event(
        event: dict[str, Any],
        data: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        event_type = event.get("type")
        if event_type == "state":
            data.update(event.get("state", {}))
        elif event_type == "replace":
            messages = list(event.get("messages", []))
        elif event_type == "message":
            idx = event.get("index", len(messages))
            msg = event.get("message")
            if not isinstance(msg, dict):
                return messages
            if isinstance(idx, int) and idx < len(messages):
                return messages
            messages.append(msg)
        elif event_type == "subagent_checkpoint":
            from .subagent_conversation import apply_checkpoint

            messages, data["subagent_state"] = apply_checkpoint(event, messages)
        return messages

    def _replay_lines(
        self,
        line_iter: Iterable[bytes],
        data: dict[str, Any],
        messages: list[dict[str, Any]],
        *,
        track_lifecycle: bool,
        last_lifecycle: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Replay lines into *messages* in place so partial progress survives mid-stream OSError."""
        loads = json.loads
        append_msg = messages.append
        try:
            for line in line_iter:
                try:
                    event = loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                event_type = event.get("type")
                if event_type == "message":
                    msg = event.get("message")
                    if not isinstance(msg, dict):
                        continue
                    idx = event.get("index", len(messages))
                    if isinstance(idx, int) and idx < len(messages):
                        continue
                    append_msg(msg)
                elif event_type == "replace":
                    messages[:] = event.get("messages", [])
                elif event_type == "state":
                    data.update(event.get("state", {}))
                elif track_lifecycle and event_type in _LIFECYCLE_TYPES:
                    last_lifecycle = event
                elif event_type == "subagent_checkpoint":
                    updated = self._apply_replay_event(event, data, messages)
                    if updated is not messages:
                        messages[:] = updated
        except OSError:
            return last_lifecycle, False
        return last_lifecycle, True

    @staticmethod
    def _find_last_lifecycle_in_mmap(mm: mmap.mmap) -> dict[str, Any] | None:
        line_end = len(mm)
        loads = json.loads
        while line_end > 0:
            line_start = mm.rfind(b"\n", 0, line_end) + 1
            if line_start < line_end:
                head = mm[line_start : min(line_start + 32, line_end)].lstrip()
                if (
                    head
                    and not head.startswith(_MESSAGE_PREFIXES)
                    and not head.startswith(_REPLACE_PREFIXES)
                    and not head.startswith(_STATE_PREFIXES)
                ):
                    line = mm[line_start:line_end]
                    if any(marker in line for marker in _LIFECYCLE_MARKERS):
                        try:
                            event = loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
                        else:
                            if isinstance(event, dict) and event.get("type") in _LIFECYCLE_TYPES:
                                return event
            line_end = line_start - 1
        return None

    @staticmethod
    def _apply_states_before_in_mmap(
        mm: mmap.mmap,
        limit: int,
        data: dict[str, Any],
    ) -> None:
        pos = 0
        loads = json.loads
        while pos < limit:
            found = mm.find(b'"state"', pos, limit)
            if found == -1:
                return
            line_start = mm.rfind(b"\n", 0, found) + 1
            line_end = mm.find(b"\n", found, limit)
            if line_end == -1:
                line_end = limit
            head = mm[line_start : min(line_start + 32, line_end)].lstrip()
            if head.startswith(_MESSAGE_PREFIXES) or head.startswith(_REPLACE_PREFIXES):
                pos = line_end + 1
                continue
            try:
                event = loads(mm[line_start:line_end])
            except (json.JSONDecodeError, UnicodeDecodeError):
                pos = line_end + 1
                continue
            if isinstance(event, dict) and event.get("type") == "state":
                data.update(event.get("state", {}))
            pos = line_end + 1

    def _replay_jsonl_with_lifecycle(
        self,
        data: dict[str, Any],
        *,
        track_lifecycle: bool = False,
    ) -> tuple[dict[str, Any] | None, bool]:
        messages = list(data.get("messages", []))
        last_lifecycle: dict[str, Any] | None = None

        try:
            with open(self.jsonl_path, "rb") as handle:
                file_size = os.fstat(handle.fileno()).st_size
                if file_size == 0:
                    data["messages"] = messages
                    return None, True

                if file_size >= self._MMAP_MIN_BYTES:
                    try:
                        mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
                    except (OSError, ValueError):
                        mm = None
                    if mm is not None:
                        try:
                            loads = json.loads
                            chosen_replace_start: int | None = None
                            chosen_replace_end = 0
                            if (
                                mm.rfind(b'"replace"') != -1
                                and mm.find(b"subagent_checkpoint") == -1
                            ):
                                search_end = len(mm)
                                while search_end > 0:
                                    pos = mm.rfind(b'"replace"', 0, search_end)
                                    if pos == -1:
                                        break
                                    line_start = mm.rfind(b"\n", 0, pos) + 1
                                    line_end = mm.find(b"\n", pos)
                                    if line_end == -1:
                                        line_end = len(mm)
                                    head = mm[line_start : min(line_start + 32, line_end)].lstrip()
                                    if head.startswith(_MESSAGE_PREFIXES) or head.startswith(_STATE_PREFIXES):
                                        search_end = line_start
                                        continue
                                    try:
                                        event = loads(mm[line_start:line_end])
                                    except (json.JSONDecodeError, UnicodeDecodeError):
                                        search_end = line_start
                                        continue
                                    if isinstance(event, dict) and event.get("type") == "replace":
                                        chosen_replace_start = line_start
                                        chosen_replace_end = (
                                            line_end + 1 if line_end < len(mm) else len(mm)
                                        )
                                        messages[:] = event.get("messages", [])
                                        break
                                    search_end = line_start

                            if chosen_replace_start is not None:
                                if chosen_replace_start > 0:
                                    self._apply_states_before_in_mmap(mm, chosen_replace_start, data)
                                if track_lifecycle:
                                    last_lifecycle = self._find_last_lifecycle_in_mmap(mm)
                                mm.seek(chosen_replace_end)
                                _, replay_ok = self._replay_lines(
                                    iter(mm.readline, b""),
                                    data,
                                    messages,
                                    track_lifecycle=False,
                                )
                            else:
                                mm.seek(0)
                                last_lifecycle, replay_ok = self._replay_lines(
                                    iter(mm.readline, b""),
                                    data,
                                    messages,
                                    track_lifecycle=track_lifecycle,
                                )
                        finally:
                            mm.close()
                        data["messages"] = messages
                        return last_lifecycle, replay_ok

                last_lifecycle, replay_ok = self._replay_lines(
                    handle,
                    data,
                    messages,
                    track_lifecycle=track_lifecycle,
                )
        except OSError:
            data["messages"] = messages
            return last_lifecycle, False

        data["messages"] = messages
        return last_lifecycle, replay_ok

    def _replay_jsonl(self, data: dict[str, Any]) -> None:
        self._replay_jsonl_with_lifecycle(data, track_lifecycle=False)
