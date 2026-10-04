"""Conversation versions: immutable reply checkpoints and isolated writable heads.

The optional manifest is the only publication/selection authority. Readers never
create it, and a SessionStore resolves its physical head only at construction.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .message_source import message_source_ref

_GUARD = threading.RLock()
_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_MANIFEST = 8 * 1024 * 1024
_MAX_BRANCHES = 512
_STATUSES = {"completed", "generating", "failed", "cancelled", "interrupted"}
_WRITER_INSTANCE = uuid.uuid4().hex


class BranchConflict(ValueError):
    """A stale selection or an unverifiable source must not change a branch."""


class BranchPublicationError(OSError):
    """The manifest could not be reread after a publication error."""
    publication_unknown = True

    def __init__(self, candidate_branch_id: str | None):
        super().__init__("Conversation publication outcome could not be verified")
        self.candidate_branch_id = candidate_branch_id


def checked_branch_id(value: Any) -> str:
    if value != "main" and (not isinstance(value, str) or not _ID.fullmatch(value)):
        raise ValueError("Invalid conversation branch identity")
    return value


def _checked_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid conversation version identity")
    return value


def _logical(path: str | Path) -> Path:
    value = Path(path)
    return value if value.suffix == ".json" else value.with_suffix(".json")


def branch_scope(path: str | Path, branch_id: str) -> str:
    checked_branch_id(branch_id)
    logical = _logical(path)
    if branch_id == "main":
        return logical.stem
    identity = hashlib.sha256(str(logical.resolve()).encode()).hexdigest()[:16]
    return f"branch_{identity}_{branch_id}"


def _text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _user(message: dict) -> bool:
    # Share the compressor's legacy-prefix/provenance policy. A recap or a
    # background worker envelope must never become a regeneration request.
    from .context_compressor import _is_synthetic_user_turn
    return message.get("role") == "user" and not _is_synthetic_user_turn(message)


def _messages_digest(messages: list[dict]) -> str:
    digest = hashlib.sha256()
    for message in messages:
        digest.update(bytes.fromhex(message_source_ref(message, 0)["digest"]))
    return digest.hexdigest()


def _dated(message: dict) -> bool:
    value = message.get("timestamp")
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _complete_tools(messages: list[dict]) -> bool:
    pending: set[str] = set()
    for message in messages:
        calls = message.get("tool_calls")
        if calls:
            if pending or not isinstance(calls, list):
                return False
            for call in calls:
                key = call.get("id") if isinstance(call, dict) else None
                if not isinstance(key, str) or not key or key in pending:
                    return False
                pending.add(key)
        elif message.get("role") == "tool":
            key = message.get("tool_call_id")
            if key not in pending:
                return False
            pending.remove(key)
        elif pending:
            return False
    return not pending


def _targets(messages: list[dict]) -> list[tuple[int, int]]:
    result = []
    # Peer mail and wakeups start their own exchanges. In-turn observations
    # (worker results, images, question answers) still belong to the human turn.
    def boundary(message: dict) -> bool:
        if message.get("role") != "user":
            return False
        if _user(message):
            return True
        provenance = message.get("provenance")
        if provenance in {"delegate", "notification", "tool_image", "user_question_answer"}:
            return False
        if not provenance and _text(message).lstrip().startswith(("[SYSTEM-DELIVERED SUBAGENT RESULT", "[通知 ")):
            return False
        return True

    anchors = [index for index, message in enumerate(messages) if boundary(message)]
    for ordinal, start in enumerate(anchors):
        if not _user(messages[start]):
            continue
        end = anchors[ordinal + 1] if ordinal + 1 < len(anchors) else len(messages)
        replies = [index for index in range(start + 1, end)
                   if messages[index].get("role") == "assistant"
                   and (messages[index].get("_meta") or {}).get("type") != "reasoning_context"]
        if replies:
            reply = replies[-1]
            if not messages[reply].get("tool_calls") and _text(messages[reply]).strip() and _complete_tools(messages[:reply]):
                result.append((start, reply))
    return result


class ConversationBranches:
    def __init__(self, logical_path: str | Path):
        self.logical_path = _logical(logical_path)
        self.root = self.logical_path.parent / ".branches" / self.logical_path.stem
        self.manifest_path = self.root / "manifest.json"

    def _safe(self, path: Path) -> None:
        """Reject links in every component below the established session root."""
        relative = path.relative_to(self.logical_path.parent)
        current = self.logical_path.parent
        for part in relative.parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise OSError("Conversation branch path is not a regular owned path")

    def _head_path(self, branch_id: str) -> Path:
        checked_branch_id(branch_id)
        path = self.logical_path if branch_id == "main" else self.root / "heads" / f"{branch_id}.json"
        if branch_id != "main":
            self._safe(path)
        return path

    def _checkpoint_path(self, version_id: str) -> Path:
        path = self.root / "checkpoints" / f"{_checked_id(version_id)}.json"
        self._safe(path)
        return path

    def _read(self) -> dict:
        self._safe(self.manifest_path)
        try:
            with self.manifest_path.open("rb") as handle:
                raw = handle.read(_MAX_MANIFEST + 1)
        except FileNotFoundError:
            return {"schema": 1, "revision": 0, "active_branch": "main",
                    "branches": {"main": {"id": "main", "choices": [], "status": "completed"}}, "groups": {}}
        if len(raw) > _MAX_MANIFEST:
            raise OSError("Conversation branch manifest is too large")
        try:
            value = json.loads(raw)
            self._validate(value)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise OSError("Conversation branch manifest is invalid") from error
        return value

    @staticmethod
    def _validate(value: dict) -> None:
        if not isinstance(value, dict) or value.get("schema") != 1 or type(value.get("revision")) is not int or value["revision"] < 0:
            raise ValueError("Invalid branch manifest")
        branches, groups = value["branches"], value["groups"]
        if not isinstance(branches, dict) or not isinstance(groups, dict) or not 1 <= len(branches) <= _MAX_BRANCHES or "main" not in branches:
            raise ValueError("Invalid branch registry")
        if checked_branch_id(value["active_branch"]) not in branches:
            raise ValueError("Unknown active branch")
        if branches[value["active_branch"]]["status"] != "completed":
            raise ValueError("Active branch is incomplete")
        for branch_id, branch in branches.items():
            checked_branch_id(branch_id)
            if branch["id"] != branch_id or branch["status"] not in _STATUSES or not isinstance(branch["choices"], list):
                raise ValueError("Invalid branch record")
            if branch_id != "main" and branch.get("parent") not in branches:
                raise ValueError("Unknown parent branch")
            seen = set()
            for choice in branch["choices"]:
                group_id, version_id = _checked_id(choice["group_id"]), _checked_id(choice["version_id"])
                if group_id in seen or group_id not in groups or version_id not in groups[group_id]["versions"]:
                    raise ValueError("Invalid branch choice")
                seen.add(group_id)
        for group_id, group in groups.items():
            _checked_id(group_id)
            if group["id"] != group_id or not isinstance(group["versions"], dict):
                raise ValueError("Invalid reply group")
            for version_id, version in group["versions"].items():
                _checked_id(version_id)
                if version["id"] != version_id or version["status"] not in _STATUSES:
                    raise ValueError("Invalid reply version")
                if version["branch_id"] not in branches or version["last_head"] not in branches:
                    raise ValueError("Unknown version branch")
                selection = {"group_id": group_id, "version_id": version_id}
                if selection not in branches[version["last_head"]]["choices"]:
                    raise ValueError("Version head does not contain its selection")
                if version["status"] == "completed" and (not isinstance(version.get("reply_ref"), dict)
                        or not isinstance(version.get("prefix_digest"), str)):
                    raise ValueError("Completed version has no canonical anchor")

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name != "nt":
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _atomic_json(self, path: Path, value: dict) -> None:
        self._safe(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._safe(path)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            self._safe(path)
            os.replace(temporary, path)
            self._sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def _publish(self, manifest: dict, *, candidate_branch_id: str | None = None) -> None:
        manifest["revision"] += 1
        self._validate(manifest)
        if len(json.dumps(manifest, ensure_ascii=False).encode()) > _MAX_MANIFEST:
            raise ValueError("Conversation version limit reached")
        try:
            self._atomic_json(self.manifest_path, manifest)
        except OSError as error:
            # replace may already have committed even when the following
            # directory fsync fails. The manifest remains authoritative.
            try:
                published = self._read()
            except OSError:
                raise BranchPublicationError(candidate_branch_id) from error
            if published != manifest:
                raise

    def _freeze(self, version_id: str, value: dict) -> None:
        """A failed publication may leave a checkpoint, but cannot rewrite it."""
        path = self._checkpoint_path(version_id)
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                previous = json.load(handle)
            if previous != value:
                raise BranchConflict("Reply checkpoint already exists with different content")
            return
        self._atomic_json(path, value)

    @contextmanager
    def _writer(self):
        from agent.ui.session_ownership import claim_session
        with _GUARD:
            lease = claim_session(self.logical_path)
            try:
                yield
            finally:
                del lease

    def resolve(self, branch_id: str | None = None) -> tuple[str, Path]:
        manifest = self._read()
        selected = manifest["active_branch"] if branch_id is None else checked_branch_id(branch_id)
        if selected not in manifest["branches"]:
            raise ValueError("Unknown conversation branch")
        return selected, self._head_path(selected)

    def _head(self, branch_id: str) -> dict:
        from .session_store import SessionStore
        store = SessionStore(self.logical_path, branch_id=branch_id)
        if not store.exists:
            raise FileNotFoundError("Conversation branch does not exist")
        return store.load(readonly=True)

    def checkpoint(self, version_id: str) -> dict:
        manifest = self._read()
        if not any(version_id in group["versions"] for group in manifest["groups"].values()):
            raise ValueError("Unknown reply checkpoint")
        with self._checkpoint_path(version_id).open(encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
            raise OSError("Reply checkpoint is invalid")
        return value

    @staticmethod
    def _check_current(manifest: dict, branch_id: str, expected_revision: int) -> None:
        checked_branch_id(branch_id)
        if type(expected_revision) is not int or expected_revision != manifest["revision"] or branch_id != manifest["active_branch"]:
            raise BranchConflict("Conversation version changed; refresh before continuing")

    @staticmethod
    def _refresh_heads(manifest: dict, branch_id: str) -> None:
        for choice in manifest["branches"][branch_id]["choices"]:
            manifest["groups"][choice["group_id"]]["versions"][choice["version_id"]]["last_head"] = branch_id

    def state(self, branch_id: str | None = None, *, messages: list[dict] | None = None) -> dict:
        """Read metadata; pass messages=[] for an index-only warm query.

        Nonempty messages must be the complete canonical head in source order,
        not a display projection or page. No reader performs lifecycle writes.
        """
        manifest = self._read()
        selected = manifest["active_branch"] if branch_id is None else checked_branch_id(branch_id)
        if selected not in manifest["branches"]:
            raise ValueError("Unknown conversation branch")
        if messages is None:
            try:
                messages = self._head(selected)["messages"]
            except FileNotFoundError:
                if selected != "main":
                    raise
                messages = []
        if not isinstance(messages, list) or any(not isinstance(message, dict) for message in messages):
            raise OSError("Conversation messages are invalid")
        choices = copy.deepcopy(manifest["branches"][selected]["choices"])
        targets = []
        by_prefix = {}
        by_anchor = {}
        prefix_hash = hashlib.sha256()
        prefixes = []
        for message in messages:
            prefix_hash.update(bytes.fromhex(message_source_ref(message, 0)["digest"]))
            prefixes.append(prefix_hash.hexdigest())
        for user, reply in _targets(messages):
            reference = message_source_ref(messages[reply], reply)
            target = {"source_ref": reference}
            by_prefix[prefixes[reply]] = target
            if _dated(messages[user]) and _dated(messages[reply]):
                identity = (message_source_ref(messages[user], user)["digest"], reference["digest"])
                by_anchor.setdefault(identity, []).append(target)
            targets.append(target)
        groups = []
        for choice in choices:
            group = manifest["groups"][choice["group_id"]]
            version = group["versions"][choice["version_id"]]
            item = {"id": group["id"], "selected_version": version["id"],
                    "user_text": group["user_text"], "response_text": version.get("response_text", ""),
                    "versions": [{"id": value["id"], "branch_id": value["branch_id"],
                                  "status": value["status"], "number": number}
                                 for number, value in enumerate(group["versions"].values(), 1)]}
            target = by_prefix.get(version.get("prefix_digest"))
            if target is None and version.get("dated_anchor"):
                matches = by_anchor.get((group.get("user_digest"), version["reply_ref"]["digest"]), [])
                if len(matches) == 1:
                    target = matches[0]
            if target is not None:
                item["source_ref"] = target["source_ref"]
                target.update(group_id=group["id"], version_id=version["id"])
            groups.append(item)
        return {"revision": manifest["revision"], "active_branch": manifest["active_branch"],
                "branch_id": selected, "choices": choices, "groups": groups, "targets": targets}

    def prepare_retry(self, source_ref: dict, *, branch_id: str, expected_revision: int) -> dict:
        with self._writer():
            manifest = self._read()
            self._check_current(manifest, branch_id, expected_revision)
            if any(branch["status"] == "generating" for branch in manifest["branches"].values()):
                raise BranchConflict("Another response version is still generating")
            if len(manifest["branches"]) >= _MAX_BRANCHES:
                raise ValueError("Conversation version limit reached")
            if (not isinstance(source_ref, dict) or set(source_ref) != {"index", "digest"}
                    or type(source_ref["index"]) is not int or source_ref["index"] < 0
                    or not isinstance(source_ref["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", source_ref["digest"])):
                raise ValueError("Invalid reply source reference")
            data = self._head(branch_id)
            messages = data["messages"]
            reply = source_ref["index"]
            if reply >= len(messages) or message_source_ref(messages[reply], reply) != source_ref:
                raise BranchConflict("Reply changed or was compacted; refresh before regenerating")
            anchor = next((user for user, final in _targets(messages) if final == reply), None)
            if anchor is None:
                raise BranchConflict("Only a complete final reply can be regenerated")
            choices = manifest["branches"][branch_id]["choices"]
            prefix_digest = _messages_digest(messages[:reply + 1])
            user_digest = message_source_ref(messages[anchor], anchor)["digest"]
            dated_anchor = _dated(messages[anchor]) and _dated(messages[reply])
            same_anchor_count = sum(
                message_source_ref(messages[user], user)["digest"] == user_digest
                and message_source_ref(messages[final], final)["digest"] == source_ref["digest"]
                for user, final in _targets(messages)
            )
            position = None
            for index, choice in enumerate(choices):
                version = manifest["groups"][choice["group_id"]]["versions"][choice["version_id"]]
                group = manifest["groups"][choice["group_id"]]
                if version.get("prefix_digest") == prefix_digest or (
                        dated_anchor and version.get("dated_anchor") and same_anchor_count == 1
                        and group.get("user_digest") == user_digest
                        and version["reply_ref"]["digest"] == source_ref["digest"]):
                    position = index
                    break
            if position is None:
                group_id, original_id = uuid.uuid4().hex, uuid.uuid4().hex
                original = copy.deepcopy(data)
                original["messages"] = original["messages"][:reply + 1]
                original.update(runtime_context_projection={}, system_prompt_projection={}, last_prompt_tokens=0)
                self._freeze(original_id, original)
                original_choice = {"group_id": group_id, "version_id": original_id}
                manifest["groups"][group_id] = {"id": group_id, "user_text": _text(messages[anchor])[:1000],
                    "user_digest": user_digest,
                    "versions": {original_id: {"id": original_id, "branch_id": branch_id, "last_head": branch_id,
                                                "status": "completed", "reply_ref": source_ref,
                                                "prefix_digest": prefix_digest,
                                                "dated_anchor": dated_anchor,
                                                "response_text": _text(messages[reply])[:4000]}}}
                # An outer group can be created after an inner group. Register
                # its original choice in every proven descendant, including
                # inactive siblings, so returning through a nested version
                # never drops the newly introduced outer selector.
                checkpoint_cache = {}
                for other_id, other in manifest["branches"].items():
                    evidence = [self._head(other_id)["messages"]]
                    for choice in other["choices"]:
                        selected_id = choice["version_id"]
                        selected_version = manifest["groups"][choice["group_id"]]["versions"][selected_id]
                        if selected_version["status"] != "completed":
                            continue
                        if selected_id not in checkpoint_cache:
                            checkpoint_cache[selected_id] = self.checkpoint(selected_id)["messages"]
                        evidence.append(checkpoint_cache[selected_id])
                    if not any(len(items) > reply and _messages_digest(items[:reply + 1]) == prefix_digest for items in evidence):
                        continue
                    insertion = len(other["choices"])
                    for index, choice in enumerate(other["choices"]):
                        checkpoint_messages = checkpoint_cache.get(choice["version_id"], [])
                        if len(checkpoint_messages) > reply and _messages_digest(checkpoint_messages[:reply + 1]) == prefix_digest:
                            insertion = index
                            break
                    other["choices"].insert(insertion, copy.deepcopy(original_choice))
                position = next(index for index, choice in enumerate(choices) if choice["group_id"] == group_id)
            else:
                group_id = choices[position]["group_id"]
            candidate_id, version_id = uuid.uuid4().hex, uuid.uuid4().hex
            seed = copy.deepcopy(data)
            seed["messages"] = seed["messages"][:reply]
            seed.update(runtime_context_projection={}, system_prompt_projection={}, last_prompt_tokens=0)
            self._atomic_json(self._head_path(candidate_id), seed)
            candidate_choices = copy.deepcopy(choices[:position]) + [{"group_id": group_id, "version_id": version_id}]
            manifest["branches"][candidate_id] = {"id": candidate_id, "parent": branch_id, "choices": candidate_choices,
                                                  "status": "generating", "pid": os.getpid(), "owner": _WRITER_INSTANCE,
                                                  "created_at": time.time(),
                                                  "group_id": group_id, "version_id": version_id, "seed_count": len(seed["messages"]),
                                                  "seed_digest": _messages_digest(seed["messages"])}
            manifest["groups"][group_id]["versions"][version_id] = {"id": version_id, "branch_id": candidate_id,
                "last_head": candidate_id, "status": "generating", "response_text": ""}
            self._refresh_heads(manifest, branch_id)
            self._publish(manifest, candidate_branch_id=candidate_id)
            return {"candidate_branch_id": candidate_id, "group_id": group_id, "version_id": version_id,
                    "seed": seed, "source_ref": source_ref, "revision": manifest["revision"]}

    def finish_candidate(self, candidate_branch_id: str, *, status: str = "completed",
                         expected_revision: int | None = None, error: str = "") -> dict:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("Invalid response version outcome")
        with self._writer():
            manifest = self._read()
            if expected_revision is not None and manifest["revision"] != expected_revision:
                raise BranchConflict("Conversation version changed before completion")
            candidate = manifest["branches"].get(checked_branch_id(candidate_branch_id))
            if not candidate or candidate["status"] != "generating":
                raise BranchConflict("Response version is no longer generating")
            version = manifest["groups"][candidate["group_id"]]["versions"][candidate["version_id"]]
            if status == "completed":
                if manifest["active_branch"] != candidate["parent"]:
                    raise BranchConflict("Conversation selection changed during generation")
                data = self._head(candidate_branch_id)
                messages = data["messages"]
                seed_count = candidate["seed_count"]
                targets = _targets(messages)
                if (len(messages) != seed_count + 1 or not targets or targets[-1][1] != seed_count
                        or _messages_digest(messages[:seed_count]) != candidate["seed_digest"]):
                    raise BranchConflict("Generated reply has not been saved completely")
                reply = targets[-1][1]
                self._freeze(version["id"], data)
                version.update(reply_ref=message_source_ref(data["messages"][reply], reply),
                               prefix_digest=_messages_digest(messages),
                               dated_anchor=_dated(messages[targets[-1][0]]) and _dated(messages[reply]),
                               response_text=_text(data["messages"][reply])[:4000])
                self._refresh_heads(manifest, manifest["active_branch"])
                manifest["active_branch"] = candidate_branch_id
                self._refresh_heads(manifest, candidate_branch_id)
            candidate["status"] = version["status"] = status
            if error:
                version["error"] = str(error)[:1000]
            self._publish(manifest, candidate_branch_id=candidate_branch_id)
            return self.state()

    def resolve_version(self, group_id: str, version_id: str, *, branch_id: str, expected_revision: int) -> str:
        manifest = self._read()
        self._check_current(manifest, branch_id, expected_revision)
        group = manifest["groups"].get(_checked_id(group_id))
        version = group["versions"].get(_checked_id(version_id)) if group else None
        if not version or version["status"] != "completed":
            raise BranchConflict("This response version is not complete")
        if group_id not in {choice["group_id"] for choice in manifest["branches"][branch_id]["choices"]}:
            raise BranchConflict("Reply group is outside the selected conversation path")
        target = version["last_head"]
        self._head(target)  # prove the referenced head still exists, without writes
        return target

    def select_version(self, group_id: str, version_id: str, *, branch_id: str, expected_revision: int) -> dict:
        with self._writer():
            target = self.resolve_version(group_id, version_id, branch_id=branch_id, expected_revision=expected_revision)
            manifest = self._read()
            if any(branch["status"] == "generating" for branch in manifest["branches"].values()):
                raise BranchConflict("Finish the generating response before switching versions")
            self._refresh_heads(manifest, branch_id)
            manifest["active_branch"] = target
            self._refresh_heads(manifest, target)
            self._publish(manifest)
            return {"branch_id": target, "revision": manifest["revision"], "state": self.state()}

    def recover_interrupted(self) -> bool:
        from .session_store import SessionStore
        with self._writer():
            manifest = self._read()
            changed = False
            for branch in manifest["branches"].values():
                if branch["status"] == "generating" and (branch.get("owner") != _WRITER_INSTANCE
                        or not SessionStore._process_alive(int(branch.get("pid", 0)))):
                    branch["status"] = "interrupted"
                    manifest["groups"][branch["group_id"]]["versions"][branch["version_id"]]["status"] = "interrupted"
                    changed = True
            if changed:
                self._publish(manifest)
            return changed

    def all_related_paths(self) -> list[Path]:
        self._safe(self.root)
        if not self.root.exists():
            return []
        paths = []
        for path in self.root.rglob("*"):
            self._safe(path)
            if path.is_file():
                paths.append(path)
        return sorted(paths, key=lambda path: (path == self.manifest_path, str(path)))
