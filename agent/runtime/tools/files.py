"""Host filesystem tools governed by an explicit per-root access policy.

Code execution remains sandboxed by ``tools.code``.  File reads and writes run
on the host so explicitly mounted Windows and WSL paths remain accessible.
"""

from __future__ import annotations

import hashlib
import difflib
import json
import logging
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..tool_execution import PartialResult
from ..tool_failure import ToolFailure
from ..file_checkpoints import FileCheckpointStore
from ..turn_change_store import current_turn_change_store
from .file_patch import (
    PatchError,
    PatchFormatError,
    apply_patch as apply_file_patch,
    parse_patch,
    patch_summary,
)
from .registry import ToolRegistry, ToolDef, approval_justification_schema


logger = logging.getLogger(__name__)


DEFAULT_SEARCH_SKIP_DIRS = frozenset({
    ".agent_system",
    ".astra",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".sessions",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
})


def _is_search_noise(part: str) -> bool:
    return part in DEFAULT_SEARCH_SKIP_DIRS or part.startswith(".sandbox_")


def _strip_regex_meta(needle: str) -> str:
    """Remove regex metacharacters so a pattern can be matched approximately."""
    return re.sub(r"[.*+?^${}()|[\]\\]", "", needle).strip()


def _glob_segments_match(parts: tuple[str, ...], segments: tuple[str, ...]) -> bool:
    import fnmatch as _fnmatch

    if not segments:
        return not parts
    if segments[0] == "**":
        return any(
            _glob_segments_match(parts[index:], segments[1:])
            for index in range(len(parts) + 1)
        )
    return (
        bool(parts)
        and _fnmatch.fnmatch(parts[0], segments[0])
        and _glob_segments_match(parts[1:], segments[1:])
    )


def _search_glob_matches(relative_parts: tuple[str, ...], glob_filter: str) -> bool:
    """Apply a content-search glob to one file below the search directory.

    A glob without ``/`` filters by file name. A glob with ``/`` is matched
    against the path relative to the search directory, where ``**`` spans any
    number of directories.
    """
    import fnmatch as _fnmatch

    pattern = glob_filter.replace("\\", "/")
    if "/" not in pattern:
        return _fnmatch.fnmatch(relative_parts[-1], pattern)
    segments = tuple(part for part in pattern.split("/") if part not in {"", "."})
    return _glob_segments_match(relative_parts, segments)


def _content_near_miss_suggestions(
    search: Path,
    needle: str,
    *,
    include_ignored: bool = False,
    glob_filter: str = "*",
    limit: int = 3,
    max_entries: int = 2000,
    max_bytes_per_file: int = 32 * 1024,
) -> list[str]:
    """Probe file contents for close token matches after a failed search.

    Uses 4-gram overlap on the first ``max_bytes_per_file`` of each candidate
    (case-insensitive). A file is suggested when at least half of the needle's
    grams appear in its content, which catches typos like "compresion" against
    content containing "compression" without false-positiving on unrelated
    words. Bounded the same way as :func:`_near_miss_suggestions`.
    """
    clean = _strip_regex_meta(needle)
    if len(clean) < 4:
        return []
    grams = {clean[i:i + 4] for i in range(len(clean) - 3)}
    threshold = max(2, (len(grams) + 1) // 2)
    suggestions: list[str] = []
    scanned = 0
    for candidate in search.rglob("*"):
        scanned += 1
        if scanned > max_entries:
            break
        if candidate.is_dir():
            continue
        relative = candidate.relative_to(search)
        if not include_ignored and any(
            _is_search_noise(part) for part in relative.parts
        ):
            continue
        if not _search_glob_matches(relative.parts, glob_filter):
            continue
        try:
            st = candidate.stat()
        except OSError:
            continue
        if st.st_size > 1024 * 1024:
            continue
        try:
            with candidate.open("rb") as handle:
                raw = handle.read(max_bytes_per_file)
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        content_lower = raw.decode("utf-8", errors="replace").lower()
        hits = sum(1 for gram in grams if gram in content_lower)
        if hits >= threshold:
            suggestions.append(str(relative))
            if len(suggestions) >= limit:
                break
    return suggestions


def _near_miss_suggestions(
    search: Path,
    needle: str,
    *,
    include_ignored: bool = False,
    glob_filter: str = "*",
    dirname: str = "",
    limit: int = 3,
    max_entries: int = 2000,
) -> list[str]:
    """Probe close filename matches after a failed search.

    Mirrors the search loops' noise pruning and stays bounded so a zero-result
    search costs little. Returns up to ``limit`` paths relative to ``search``,
    trying the full relative path before the file name alone.
    """
    clean = _strip_regex_meta(needle)
    dirname = "/".join(
        p for p in dirname.replace("\\", "/").split("/")
        if p and p != "**"
    )
    if not clean:
        return []
    relative_paths: list[str] = []
    basenames: list[str] = []
    scanned = 0
    for candidate in search.rglob("*"):
        scanned += 1
        if scanned > max_entries:
            break
        if candidate.is_dir():
            continue
        relative = candidate.relative_to(search)
        if not include_ignored and any(
            _is_search_noise(part) for part in relative.parts
        ):
            continue
        if dirname and not str(relative).replace("\\", "/").startswith(dirname + "/"):
            continue
        if not _search_glob_matches(relative.parts, glob_filter):
            continue
        relative_paths.append(str(relative))
        basenames.append(candidate.name)
    if not relative_paths:
        return []
    suggestions: list[str] = []
    for pool in (relative_paths, basenames):
        for match in difflib.get_close_matches(clean, pool, n=limit, cutoff=0.6):
            # A file-name match is reported by its path so it can be opened.
            paths = [match] if pool is relative_paths else [
                path for path, name in zip(relative_paths, basenames) if name == match
            ]
            for path in paths:
                if path not in suggestions:
                    suggestions.append(path)
        if len(suggestions) >= limit:
            break
    return suggestions[:limit]


@dataclass(frozen=True)
class FilesystemRoot:
    path: Path
    mode: str = "ro"
    requires_approval: bool = False


@dataclass(frozen=True)
class FilesystemGrant:
    key: str
    previous: FilesystemRoot | None = None


@dataclass
class FileWriteTransaction:
    write_id: str
    requested_path: str
    target: Path
    temp_path: Path
    overwrite: bool
    expected_size: int | None
    manifest_path: Path
    created_at: float
    next_sequence: int = 0
    chars: int = 0
    bytes_written: int = 0
    running_sha256: str = hashlib.sha256(b"").hexdigest()


class FilesystemPolicy:
    """Resolve paths against workspace and explicitly configured host roots."""

    def __init__(self, workspace: Path, roots: list[FilesystemRoot] | None = None):
        self.workspace = workspace.resolve()
        # Configured host roots are visibility hints, not standing permission.
        # Grants are represented as roots with ``requires_approval=False`` and
        # are revoked after once/session scope.
        configured = [
            FilesystemRoot(item.path, item.mode, requires_approval=True)
            for item in (roots or [])
        ]
        configured.append(FilesystemRoot(self.workspace, "rw"))
        deduplicated: dict[str, FilesystemRoot] = {}
        for item in configured:
            resolved = item.path.expanduser().resolve()
            mode = item.mode.strip().lower()
            if mode not in {"ro", "rw"}:
                raise ValueError(f"Invalid filesystem root mode '{item.mode}' for {item.path}")
            key = os.path.normcase(str(resolved))
            previous = deduplicated.get(key)
            if previous is None or mode == "rw":
                deduplicated[key] = FilesystemRoot(
                    resolved,
                    mode,
                    requires_approval=item.requires_approval,
                )
        self.roots = sorted(deduplicated.values(), key=lambda item: len(str(item.path)), reverse=True)

    @classmethod
    def load(cls, workdir: str, config_path: str | None = None) -> "FilesystemPolicy":
        workspace = Path(workdir).expanduser().resolve()
        path = Path(
            config_path
            or os.getenv("AGENT_FILESYSTEM_CONFIG", "")
            or workspace / ".astra" / "filesystem.json"
        ).expanduser()
        if not path.is_absolute():
            path = workspace / path
        roots: list[FilesystemRoot] = []
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            raw_roots = payload.get("roots", []) if isinstance(payload, dict) else []
            if not isinstance(raw_roots, list):
                raise ValueError(f"Filesystem config roots must be a list: {path}")
            for item in raw_roots:
                if not isinstance(item, dict) or not str(item.get("path", "")).strip():
                    raise ValueError(f"Invalid filesystem root in {path}: {item!r}")
                raw_path = os.path.expandvars(str(item["path"]))
                root_path = Path(raw_path).expanduser()
                if not root_path.is_absolute():
                    root_path = workspace / root_path
                roots.append(FilesystemRoot(root_path, str(item.get("mode", "ro"))))
        return cls(workspace, roots)

    @staticmethod
    def _contains(root: Path, candidate: Path) -> bool:
        try:
            common = os.path.normcase(os.path.commonpath([str(root), str(candidate)]))
            return common == os.path.normcase(str(root))
        except ValueError:  # Different Windows drives, or incompatible path forms.
            return False

    def _matching_root(self, candidate: Path) -> FilesystemRoot | None:
        for item in self.roots:
            if self._contains(item.path, candidate):
                return item
        return None

    def _candidate(self, path: str) -> Path:
        cleaned = _clean_path(path)
        candidate = Path(cleaned).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve()

    def resolve(self, path: str, *, write: bool = False) -> Path:
        resolved = self._candidate(path)
        item = self._matching_root(resolved)
        if item is not None:
            if write and item.mode != "rw":
                raise PermissionError(f"Filesystem root is read-only: {item.path}")
            return resolved
        allowed = ", ".join(f"{item.path} ({item.mode})" for item in self.roots)
        raise ValueError(f"Path is outside allowed filesystem roots: {path}. Allowed: {allowed}")

    def permission_request(self, path: str, *, write: bool, operation: str) -> dict | None:
        """Return a frontend-safe request for every workspace-external path."""
        target = self._candidate(path)
        matching_root = self._matching_root(target)
        outside_workspace = not self._contains(self.workspace, target)
        detail = ""
        try:
            self.resolve(path, write=write)
        except (PermissionError, ValueError) as exc:
            detail = str(exc)
        needs_approval = (
            matching_root is None
            or matching_root.requires_approval
            or (write and matching_root.mode != "rw")
        )
        if not needs_approval:
            return None
        access = "write" if write else "read"
        scope_kind = "directory" if target.exists() and target.is_dir() else "file"
        resource = "目录" if scope_kind == "directory" else "文件"
        action = "修改" if write else "读取"
        if not detail:
            detail = (
                "该路径已配置为可见，但在当前工作区之外；"
                "仍需明确批准。"
            )
        return {
            "kind": "filesystem",
            "scope": f"filesystem:{access}:{os.path.normcase(str(target))}",
            "scope_kind": scope_kind,
            "target": str(target),
            "targets": [str(target)],
            "workspace": str(self.workspace),
            "outside_workspace": outside_workspace,
            "operation": operation,
            "access": access,
            "approval_title": f"{action}工作区外的宿主机{resource}",
            "approval_summary": f"{operation}: {target}",
            "approval_effect": (
                f"Agent 将{action}该宿主机{resource}；"
                "不授予 shell 权限。"
            ),
            "approval_boundary": (
                "该目录及其子目录"
                if scope_kind == "directory"
                else "仅该文件"
            ),
            "approval_question": (
                f"是否允许我{action}工作区外的宿主机{resource}？"
                if outside_workspace
                else f"是否允许我{action}该宿主机{resource}？"
            ),
            "reason": (
                f"{operation} 请求访问工作区外的宿主机路径；"
                "需要审批"
                if outside_workspace
                else f"{operation} 请求访问超出当前文件系统策略的路径"
            ),
            "detail": detail,
        }

    def grant(self, path: str, mode: str) -> FilesystemGrant:
        """Grant an exact path, returning enough state for one-shot rollback."""
        resolved = self._candidate(path)
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"ro", "rw"}:
            raise ValueError(f"Invalid filesystem grant mode: {mode}")
        key = os.path.normcase(str(resolved))
        previous = next(
            (item for item in self.roots if os.path.normcase(str(item.path)) == key),
            None,
        )
        self.roots = [
            item for item in self.roots
            if os.path.normcase(str(item.path)) != key
        ]
        self.roots.append(FilesystemRoot(resolved, normalized_mode, requires_approval=False))
        self.roots.sort(key=lambda item: len(str(item.path)), reverse=True)
        return FilesystemGrant(key=key, previous=previous)

    def revoke(self, grant: FilesystemGrant) -> None:
        self.roots = [
            item for item in self.roots
            if os.path.normcase(str(item.path)) != grant.key
        ]
        if grant.previous is not None:
            self.roots.append(grant.previous)
        self.roots.sort(key=lambda item: len(str(item.path)), reverse=True)


def _clean_path(path: str) -> str:
    """Normalize Docker-style workspace paths emitted by models."""
    normalized = path.strip()
    if normalized.startswith("/workspace/"):
        return normalized[len("/workspace/"):]
    if normalized == "/workspace":
        return "."
    if os.name == "nt":
        match = re.match(r"^/mnt/([a-zA-Z])(?:/(.*))?$", normalized)
        if match:
            drive = match.group(1).upper()
            remainder = (match.group(2) or "").replace("/", "\\")
            return f"{drive}:\\{remainder}" if remainder else f"{drive}:\\"
    return normalized


def _note_turn_capture(pending, checkpoint_id: str) -> None:
    """Feed captured before-bytes into the active turn-change store (best effort).

    The store is only visible while a turn store scope is bound; turn-change
    bookkeeping must never propagate failures into the file tools, so every
    path here degrades to a logged no-op.
    """
    try:
        store = current_turn_change_store()
        if store is None or pending is None:
            return
        for captured in pending.files:
            # 传文件策略根解析出的绝对路径（稳定身份），并用策略相对名做展示名；
            # store 的 after 读取因此始终指向被编辑的那一份文件（review R4）。
            if captured.existed:
                store.note_capture(
                    captured.path,
                    captured.before,
                    checkpoint_id=checkpoint_id,
                    display=captured.relative,
                )
            else:
                store.note_absent(
                    captured.path,
                    checkpoint_id=checkpoint_id,
                    display=captured.relative,
                )
    except Exception:
        logger.exception("Failed to note turn-change capture")


def register_file_tools(
    registry: ToolRegistry,
    workdir: str = ".",
    sandbox=None,
    *,
    policy: FilesystemPolicy | None = None,
    config_path: str | None = None,
):
    """Register host file tools.

    ``sandbox`` is retained for API compatibility but intentionally unused:
    execution isolation belongs to Python/shell tools, while this module uses
    the explicit filesystem policy.
    """
    access = policy or FilesystemPolicy.load(workdir, config_path)
    # M2 review P2: expose the resolved policy so runtime path bookkeeping can
    # unify tool-reported paths with the capture-chain identity.
    registry.filesystem_policy = access
    checkpoints = FileCheckpointStore(access.workspace)

    def _capture_checkpoint(paths, *, operation: str, task_id: str = ""):
        """Keep checkpoint bookkeeping best-effort so it never blocks a write."""
        try:
            pending = checkpoints.capture(paths, operation=operation, task_id=task_id)
        except Exception:
            logger.exception("Failed to capture file checkpoint for %s", operation)
            return None
        if pending is not None and pending.skipped_paths:
            logger.warning(
                "File checkpoint skipped approved paths outside workspace for %s: %s",
                operation,
                ", ".join(pending.skipped_paths[:8]),
            )
        _note_turn_capture(pending, "")
        return pending

    def _finalize_checkpoint(pending) -> str:
        try:
            checkpoint_id = checkpoints.finalize(pending)
        except Exception:
            logger.exception("Failed to finalize file checkpoint")
            return ""
        if checkpoint_id:
            _note_turn_capture(pending, checkpoint_id)
        return checkpoint_id
    transactions: dict[str, FileWriteTransaction] = {}
    transaction_dir = access.workspace / ".astra" / "file-transactions"

    def positive_env(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            return default
        return value if value > 0 else default

    chunk_char_limit = positive_env("TOOL_FILE_CHUNK_CHARS", 6_000)

    def read_file_limits() -> tuple[int, int, int]:
        """Return read_file's page byte cap, line byte cap and default line limit."""
        try:
            return (
                max(1_024, int(os.getenv("READ_FILE_MAX_PAGE_BYTES", str(1024 * 1024)))),
                max(1_024, int(os.getenv("READ_FILE_MAX_LINE_BYTES", str(256 * 1024)))),
                max(1, int(os.getenv("READ_FILE_DEFAULT_LINES", "400"))),
            )
        except ValueError:
            return 1024 * 1024, 256 * 1024, 400

    # Counting every line of a larger file on each partial page would make
    # paging slow; include_metadata already reads the whole file for its hash.
    read_file_count_lines_max_bytes = 64 * 1024 * 1024

    def _tool_path(target: Path) -> str:
        """Render a path the file tools accept back: workspace-relative inside it."""
        try:
            return str(target.relative_to(access.workspace))
        except ValueError:
            return str(target)

    def audit_transaction(event: str, transaction: FileWriteTransaction, **details) -> None:
        transaction_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": time.time(),
            "event": event,
            "write_id": transaction.write_id,
            "path": str(transaction.target),
            "next_sequence": transaction.next_sequence,
            "chars": transaction.chars,
            "bytes": transaction.bytes_written,
            "running_sha256": transaction.running_sha256,
            **details,
        }
        with open(transaction_dir / "audit.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def persist_transaction(transaction: FileWriteTransaction) -> None:
        payload = {
            "write_id": transaction.write_id,
            "requested_path": transaction.requested_path,
            "target": str(transaction.target),
            "temp_path": str(transaction.temp_path),
            "overwrite": transaction.overwrite,
            "expected_size": transaction.expected_size,
            "created_at": transaction.created_at,
            "next_sequence": transaction.next_sequence,
            "chars": transaction.chars,
            "bytes_written": transaction.bytes_written,
            "running_sha256": transaction.running_sha256,
        }
        temporary = transaction.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, transaction.manifest_path)

    def discard_transaction(transaction: FileWriteTransaction, reason: str) -> None:
        transaction.temp_path.unlink(missing_ok=True)
        transaction.manifest_path.unlink(missing_ok=True)
        audit_transaction("discarded", transaction, reason=reason)
        transactions.pop(transaction.write_id, None)

    def cleanup_abandoned_transactions() -> None:
        if not transaction_dir.exists():
            return
        for manifest_path in transaction_dir.glob("*.manifest.json"):
            write_id = manifest_path.name.removesuffix(".manifest.json")
            temp_path = transaction_dir / f"{write_id}.part"
            transaction = FileWriteTransaction(
                write_id=write_id,
                requested_path="",
                target=Path("<unknown>"),
                temp_path=temp_path,
                overwrite=False,
                expected_size=None,
                manifest_path=manifest_path,
                created_at=0,
            )
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                transaction.requested_path = str(payload.get("requested_path") or "")
                transaction.target = Path(str(payload.get("target") or "<unknown>"))
                transaction.next_sequence = int(payload.get("next_sequence") or 0)
                transaction.chars = int(payload.get("chars") or 0)
                transaction.bytes_written = int(payload.get("bytes_written") or 0)
                transaction.running_sha256 = str(
                    payload.get("running_sha256") or hashlib.sha256(b"").hexdigest()
                )
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            discard_transaction(transaction, "process_restart")
        for orphan in transaction_dir.glob("*.part"):
            orphan.unlink(missing_ok=True)
        for orphan in transaction_dir.glob("*.tmp"):
            orphan.unlink(missing_ok=True)

    cleanup_abandoned_transactions()
    session_grants: list[FilesystemGrant] = []

    def _preview_lines(lines: list[str], limit: int = 18, max_chars: int = 1600) -> str:
        preview = "\n".join(lines[:limit])
        if len(lines) > limit:
            preview += f"\n… ({len(lines) - limit} more lines)"
        if len(preview) > max_chars:
            preview = preview[: max_chars - 1] + "…"
        return preview

    def _file_change_preview(operation: str, args: dict, target: str) -> tuple[dict, str]:
        if operation == "Write file":
            content = str(args.get("content") or "")
            lines = content.splitlines()
            diff = list(difflib.unified_diff(
                [],
                lines,
                fromfile="/dev/null",
                tofile=target,
                lineterm="",
                n=2,
            ))
            return {
                "kind": "create_or_overwrite",
                "additions": len(lines),
                "deletions": 0,
            }, _preview_lines(diff)
        if operation == "Edit file":
            old = str(args.get("old") or "")
            new = str(args.get("new") or "")
            old_lines = old.splitlines()
            new_lines = new.splitlines()
            diff = list(difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"{target} (matched text)",
                tofile=f"{target} (replacement)",
                lineterm="",
                n=2,
            ))
            matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
            additions = deletions = 0
            for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
                if tag in {"replace", "delete"}:
                    deletions += old_end - old_start
                if tag in {"replace", "insert"}:
                    additions += new_end - new_start
            return {
                "kind": "edit",
                "additions": additions,
                "deletions": deletions,
            }, _preview_lines(diff)
        return {}, ""

    def permission_check(operation: str, *, write: bool, default_path: str = "."):
        def check(args: dict) -> dict | None:
            request = access.permission_request(
                str(args.get("path", default_path)),
                write=write,
                operation=operation,
            )
            if request is not None and write:
                summary, preview = _file_change_preview(
                    operation,
                    args,
                    str(request["target"]),
                )
                if summary:
                    request["change_summary"] = summary
                if preview:
                    request["preview"] = preview
            return request
        return check

    def permission_grant(args: dict, request: dict, decision: str):
        mode = "rw" if request.get("access") == "write" else "ro"
        grant = access.grant(str(request["target"]), mode)
        if decision == "once":
            return lambda: access.revoke(grant)
        session_grants.append(grant)
        return None

    def patch_permission_check(args: dict) -> dict | None:
        try:
            operations = parse_patch(str(args.get("patch") or ""))
        except PatchError:
            return None
        requests = []
        targets = []
        for operation in operations:
            target = access._candidate(operation.path)
            targets.append(str(target))
            request = access.permission_request(
                operation.path,
                write=True,
                operation=f"Apply patch ({operation.kind})",
            )
            if request is not None:
                requests.append(request)
        destructive = any(operation.kind == "delete" for operation in operations)
        if not requests and not destructive:
            return None
        summary = patch_summary(operations)
        outside_workspace = any(
            not access._contains(access.workspace, Path(target).resolve())
            for target in targets
        )
        return {
            "kind": "filesystem_patch",
            "scope_kind": "batch",
            "scopes": [
                f"filesystem:write:{os.path.normcase(str(Path(target).resolve()))}"
                for target in targets
            ],
            "target": ", ".join(targets),
            "targets": targets,
            "workspace": str(access.workspace),
            "outside_workspace": outside_workspace,
            "operation": "Apply multi-file patch",
            "access": "write",
            "approval_title": (
                "Modify host files outside workspace"
                if outside_workspace
                else "Apply multi-file patch"
            ),
            "approval_summary": (
                f"Apply a patch to {summary['files']} path(s)"
            ),
            "approval_effect": "The patch may create, modify, or delete files.",
            "approval_boundary": "This batch of paths only",
            "reason": (
                "补丁将删除文件，需要确认"
                if destructive and not requests
                else "补丁请求写入工作区外的宿主机路径，需要审批"
            ),
            "detail": json.dumps(summary, ensure_ascii=False),
            "change_summary": {
                "kind": "patch",
                "files": summary["files"],
                "additions": summary["additions"],
                "deletions": summary["deletions"],
            },
            "preview": _preview_lines(str(args.get("patch") or "").splitlines()),
            "arguments": {
                "files": str(summary["files"]),
                "operations": ", ".join(summary["operations"]),
                "changes": f"+{summary['additions']} / -{summary['deletions']}",
            },
        }

    def patch_permission_grant(args: dict, request: dict, decision: str):
        grants = [
            access.grant(str(target), "rw")
            for target in request.get("targets", [])
        ]
        if decision != "once":
            session_grants.extend(grants)
            return None

        def cleanup() -> None:
            for grant in reversed(grants):
                access.revoke(grant)

        return cleanup

    def cleanup_session_grants(session_id: str, reason: str) -> None:
        del session_id, reason
        for grant in reversed(session_grants):
            access.revoke(grant)
        session_grants.clear()

    registry.hooks.on_session_end(cleanup_session_grants)

    async def _read_file(
        path: str,
        offset: int = 0,
        limit: int | None = None,
        include_metadata: bool = False,
        byte_offset: int | None = None,
    ) -> str | ToolFailure:
        full = access.resolve(path)
        if not full.exists():
            return ToolFailure(
                code="file_not_found",
                message=f"File not found: {full}",
                retryable=True,
                recovery_hint="Check the path or search the workspace before retrying.",
                details={"path": str(full)},
            )
        try:
            file_stat = full.stat()
        except OSError as exc:
            return ToolFailure(
                code="file_read_failed",
                message=f"Could not inspect file: {full}",
                retryable=True,
                recovery_hint="Check file permissions and whether another process has locked the file.",
                details={"path": str(full), "error": str(exc)},
            )
        if not stat.S_ISREG(file_stat.st_mode):
            return ToolFailure(
                code="not_a_file",
                message=f"Not a regular file: {full}",
                retryable=True,
                recovery_hint="Provide a regular file path rather than a directory, pipe, socket, or device.",
                details={"path": str(full)},
            )
        max_page_bytes, max_line_bytes, default_lines = read_file_limits()
        if byte_offset is not None and offset:
            return ToolFailure(
                code="invalid_arguments",
                message="byte_offset and a non-zero line offset cannot be combined",
                retryable=True,
                recovery_hint="Use line offset for normal paging or byte_offset for a truncated long line.",
            )
        resolved_limit = limit if limit is not None else default_lines
        selected_raw = bytearray()
        start_byte = max(0, int(byte_offset or 0))
        next_byte_offset: int | None = None
        line_truncated = False
        reached_eof = False
        requested_line_offset = 0 if byte_offset is not None else min(offset, 2**31 - 1)
        selected_lines = 0
        scanned_lines = 0
        try:
            with full.open("rb") as handle:
                probe = handle.read(min(8192, file_stat.st_size))
                if b"\x00" in probe:
                    raise UnicodeDecodeError("utf-8", probe, probe.index(b"\x00"), probe.index(b"\x00") + 1, "NUL byte")
                # final=False accepts an otherwise-valid UTF-8 sequence cut by
                # the probe boundary, while still rejecting malformed bytes.
                import codecs
                codecs.getincrementaldecoder("utf-8")("strict").decode(probe, final=False)
                handle.seek(start_byte if byte_offset is not None else 0)

                if byte_offset is not None:
                    payload = handle.read(max_page_bytes + 4)
                    candidate = payload[:max_page_bytes]
                    while candidate:
                        try:
                            candidate.decode("utf-8")
                            break
                        except UnicodeDecodeError as exc:
                            if exc.reason != "unexpected end of data" or len(candidate) - exc.start > 4:
                                raise
                            candidate = candidate[:exc.start]
                    # limit counts lines here too: stop after that many line ends.
                    page_end = 0
                    for _ in range(max(0, resolved_limit)):
                        newline_at = candidate.find(b"\n", page_end)
                        if newline_at < 0:
                            page_end = len(candidate)
                            break
                        page_end = newline_at + 1
                    candidate = candidate[:page_end]
                    selected_raw.extend(candidate)
                    reached_eof = start_byte + len(candidate) >= file_stat.st_size
                    if not reached_eof:
                        next_byte_offset = start_byte + len(candidate)
                        line_truncated = bool(candidate) and not candidate.endswith(b"\n")
                else:
                    # Skip requested logical lines with bounded memory. Very
                    # long lines are drained in chunks rather than materialized.
                    while scanned_lines < requested_line_offset:
                        chunk = handle.readline(max_line_bytes + 1)
                        if not chunk:
                            reached_eof = True
                            break
                        if len(chunk) > max_line_bytes and not chunk.endswith(b"\n"):
                            while chunk and not chunk.endswith(b"\n"):
                                chunk = handle.readline(max_line_bytes + 1)
                        scanned_lines += 1

                    while not reached_eof and selected_lines < resolved_limit:
                        line_start = handle.tell()
                        chunk = handle.readline(max_line_bytes + 1)
                        if not chunk:
                            reached_eof = True
                            break
                        oversized_line = len(chunk) > max_line_bytes and not chunk.endswith(b"\n")
                        keep = max_line_bytes if oversized_line else len(chunk)
                        remaining = max_page_bytes - len(selected_raw)
                        if keep > remaining:
                            if selected_lines:
                                # The page is full: leave this line whole for the next page.
                                handle.seek(line_start)
                                break
                            keep = remaining
                        prefix = chunk[:keep]
                        if keep < len(chunk):
                            # The line is cut here; keep only whole characters.
                            while prefix:
                                try:
                                    prefix.decode("utf-8")
                                    break
                                except UnicodeDecodeError as exc:
                                    if exc.reason != "unexpected end of data":
                                        raise
                                    prefix = prefix[:exc.start]
                            line_truncated = True
                            next_byte_offset = line_start + len(prefix)
                            while chunk and not chunk.endswith(b"\n"):
                                chunk = handle.readline(max_line_bytes + 1)
                        selected_raw.extend(prefix)
                        selected_lines += 1
                        if line_truncated or len(selected_raw) >= max_page_bytes:
                            break
                    if handle.tell() >= file_stat.st_size:
                        reached_eof = True

            content = bytes(selected_raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            return ToolFailure(
                code="file_not_utf8",
                message=f"File is not valid UTF-8 text: {full}",
                retryable=False,
                recovery_hint="Use a binary-aware tool or convert the file to UTF-8 before reading it as text.",
                details={"path": str(full), "error": str(exc)},
            )
        except OSError as exc:
            return ToolFailure(
                code="file_read_failed",
                message=f"Could not read file: {full}",
                retryable=True,
                recovery_hint="Check file permissions and whether another process has locked the file.",
                details={"path": str(full), "error": str(exc)},
            )

        actual_line_offset = scanned_lines if byte_offset is None else 0
        # truncated means more of the file remains after this page.
        truncated = not reached_eof or line_truncated
        if not truncated and not include_metadata and not actual_line_offset and not start_byte:
            return content
        digest = ""
        total_lines = actual_line_offset + selected_lines if reached_eof and byte_offset is None else None
        if include_metadata or (
            total_lines is None and file_stat.st_size <= read_file_count_lines_max_bytes
        ):
            hasher = hashlib.sha256()
            newline_count = 0
            final_byte = b""
            try:
                with full.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        if include_metadata:
                            hasher.update(chunk)
                        newline_count += chunk.count(b"\n")
                        final_byte = chunk[-1:]
            except OSError as exc:
                if include_metadata:
                    return ToolFailure(
                        code="file_read_failed",
                        message=f"Could not hash file: {full}",
                        retryable=True,
                        details={"path": str(full), "error": str(exc)},
                    )
            else:
                if include_metadata:
                    digest = hasher.hexdigest()
                total_lines = newline_count + (1 if final_byte not in {b"", b"\n"} else 0)
        metadata = {
            "path": str(full),
            "bytes": file_stat.st_size,
            "mtime_ns": file_stat.st_mtime_ns,
            "line_offset": actual_line_offset if byte_offset is None else None,
            "line_start": actual_line_offset + 1 if byte_offset is None and content else None,
            "line_end": actual_line_offset + selected_lines if byte_offset is None else None,
            "total_lines": total_lines,
            "next_offset": (
                actual_line_offset + selected_lines
                if byte_offset is None and not reached_eof
                else None
            ),
            "byte_offset": start_byte if byte_offset is not None else None,
            "next_byte_offset": next_byte_offset,
            "line_truncated": line_truncated,
            "truncated": truncated,
        }
        if digest:
            metadata["sha256"] = digest
        text = (
            f"[File metadata: {json.dumps(metadata, ensure_ascii=False)}]\n"
            f"{content}"
        )
        return PartialResult(text) if truncated else text

    async def _stat_file(path: str, preview_lines: int = 3) -> str | ToolFailure:
        full = access.resolve(path)
        if not full.exists():
            return ToolFailure(
                code="file_not_found",
                message=f"File not found: {full}",
                retryable=True,
                recovery_hint="Check the path or search the workspace before retrying.",
                details={"path": str(full)},
            )
        try:
            file_stat = full.stat()
        except OSError as exc:
            return ToolFailure(
                code="file_read_failed",
                message=f"Could not inspect file: {full}",
                retryable=True,
                details={"path": str(full), "error": str(exc)},
            )
        if not stat.S_ISREG(file_stat.st_mode):
            return ToolFailure(
                code="not_a_file",
                message=f"Not a regular file: {full}",
                retryable=True,
                recovery_hint="Provide a regular file path rather than a directory, pipe, socket, or device.",
                details={"path": str(full)},
            )
        preview_bytes = 64 * 1024
        hasher = hashlib.sha256()
        first_raw = bytearray()
        last_raw = bytearray()
        newline_count = 0
        final_byte = b""
        utf8_valid = True
        try:
            import codecs
            decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
            with full.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
                    newline_count += chunk.count(b"\n")
                    final_byte = chunk[-1:]
                    if len(first_raw) < preview_bytes:
                        first_raw.extend(chunk[:preview_bytes - len(first_raw)])
                    last_raw.extend(chunk)
                    if len(last_raw) > preview_bytes:
                        del last_raw[:-preview_bytes]
                    if b"\x00" in chunk:
                        utf8_valid = False
                    if utf8_valid:
                        try:
                            decoder.decode(chunk, final=False)
                        except UnicodeDecodeError:
                            utf8_valid = False
                if utf8_valid:
                    try:
                        decoder.decode(b"", final=True)
                    except UnicodeDecodeError:
                        utf8_valid = False
        except OSError as exc:
            return ToolFailure(
                code="file_read_failed",
                message=f"Could not read file metadata: {full}",
                retryable=True,
                details={"path": str(full), "error": str(exc)},
            )
        def preview_text(payload: bytes, *, tail: bool = False) -> list[str]:
            if not utf8_valid:
                return []
            if tail:
                # A tail window may begin within one UTF-8 code point.
                for skip in range(4):
                    try:
                        return payload[skip:].decode("utf-8-sig").splitlines()
                    except UnicodeDecodeError:
                        continue
                return []
            try:
                return payload.decode("utf-8-sig", errors="strict").splitlines()
            except UnicodeDecodeError:
                # The preview boundary alone may split a valid code point.
                return payload.decode("utf-8-sig", errors="ignore").splitlines()

        first_lines = preview_text(bytes(first_raw))
        last_lines = preview_text(bytes(last_raw), tail=True)
        count = max(0, min(preview_lines, 20))
        total_lines = newline_count + (1 if file_stat.st_size and final_byte != b"\n" else 0)
        return json.dumps({
            "path": str(full),
            "bytes": file_stat.st_size,
            "sha256": hasher.hexdigest(),
            "mtime_ns": file_stat.st_mtime_ns,
            "total_lines": total_lines if utf8_valid else None,
            "encoding": "utf-8-sig" if bytes(first_raw).startswith(b"\xef\xbb\xbf") else (
                "utf-8" if utf8_valid else "binary-or-non-utf8"
            ),
            "first_lines": first_lines[:count],
            "last_lines": last_lines[-count:] if count else [],
        }, ensure_ascii=False)

    async def _write_file(
        path: str,
        content: str,
        overwrite: bool = False,
        expected_sha256: str = "",
        _task_id: str = "",
    ) -> str | ToolFailure:
        full = access.resolve(path, write=True)
        existed = full.exists()
        if existed and not overwrite:
            return ToolFailure(
                code="existing_file_requires_edit",
                message=f"write_file will not overwrite existing file: {full}",
                retryable=True,
                recovery_hint=(
                    "Read the relevant region, then use edit_file for one exact replacement "
                    "or apply_patch for multi-hunk changes."
                ),
                details={"path": str(full)},
            )
        if existed:
            normalized_expected = expected_sha256.strip().lower()
            if not normalized_expected:
                return ToolFailure(
                    code="file_changed",
                    message=f"Explicit overwrite requires expected_sha256 for existing file: {full}",
                    retryable=True,
                    recovery_hint=(
                        "Read the file with include_metadata=true and retry only if a whole-file "
                        "replacement is genuinely required; otherwise use edit_file or apply_patch."
                    ),
                    details={"path": str(full), "reason": "missing_expected_sha256"},
                )
            current_sha256 = hashlib.sha256(full.read_bytes()).hexdigest()
            if normalized_expected != current_sha256:
                return ToolFailure(
                    code="file_changed",
                    message=(
                        f"File version changed before overwrite: expected {normalized_expected}, "
                        f"found {current_sha256}"
                    ),
                    retryable=True,
                    recovery_hint="Read the current file and regenerate the edit against its latest version.",
                    details={
                        "path": str(full),
                        "expected_sha256": normalized_expected,
                        "actual_sha256": current_sha256,
                    },
                )
        full.parent.mkdir(parents=True, exist_ok=True)
        checkpoint = _capture_checkpoint(
            [full], operation="write_file", task_id=_task_id
        )
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=full.parent,
                prefix=f".{full.name}.agent-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, full)
            temporary = None
            checkpoint_id = _finalize_checkpoint(checkpoint)
            byte_count = len(content.encode("utf-8"))
            return json.dumps({
                "path": str(full),
                "operation": "overwrite" if existed else "create",
                "bytes": byte_count,
                "chars": len(content),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "status": "written",
                "checkpoint_id": checkpoint_id,
            }, ensure_ascii=False)
        except Exception as exc:
            return f"Error writing file: {exc}"
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _verify_write_file(args: dict, result: dict) -> tuple[bool, str]:
        output = str(result.get("output") or "")
        if output.startswith("Error"):
            return False, output
        try:
            full = access.resolve(str(args.get("path") or ""), write=True)
            expected = str(args.get("content") or "")
            if not full.is_file():
                return False, f"written file does not exist: {full}"
            # Compare bytes: reading as text would translate "\r\n" and "\r".
            if full.read_bytes() != expected.encode("utf-8"):
                return False, f"written file content does not match: {full}"
        except (OSError, ValueError) as exc:
            return False, f"could not verify written file: {exc}"
        return True, f"verified written file: {full}"

    def _verify_no_error(args: dict, result: dict) -> tuple[bool, str]:
        output = str(result.get("output") or "")
        if output.startswith("Error:"):
            return False, output
        return True, "file operation completed"

    async def _search_files(
        pattern: str,
        path: str = ".",
        include_ignored: bool = False,
    ) -> str | ToolFailure:
        search = access.resolve(path)
        if not search.exists():
            return ToolFailure("file_not_found", f"Path not found: {search}", False,
                               "List the parent directory and use an existing path; do not repeat this path unchanged.", details={"path": str(search)})
        if not search.is_dir():
            return ToolFailure("not_a_directory", f"Not a directory: {search}", False,
                               "Use a directory path for search_files; use read_file for a single file.", details={"path": str(search)})
        matches = []
        for match in search.rglob(pattern):
            relative = match.relative_to(search)
            if (
                not include_ignored
                and any(_is_search_noise(part) for part in relative.parts)
            ):
                continue
            matches.append(match)
        matches.sort()
        if not matches:
            pattern_parts = pattern.replace("\\", "/").rsplit("/", 1)
            dirname = pattern_parts[0] if len(pattern_parts) > 1 else ""
            needle = pattern_parts[-1]
            suggestions = _near_miss_suggestions(
                search, needle, include_ignored=include_ignored, dirname=dirname
            )
            if suggestions:
                return (
                    f"No files matching '{pattern}' found in {search}\n"
                    "Did you mean one of these files?\n"
                    + "\n".join(f"- {_tool_path(search / name)}" for name in suggestions)
                )
            return f"No files matching '{pattern}' found in {search}"
        lines = [_tool_path(match) for match in matches[:100]]
        if len(matches) > 100:
            lines.append(
                f"... and {len(matches) - 100} more not listed; "
                "narrow the pattern or path to see them"
            )
            return PartialResult("\n".join(lines))
        return "\n".join(lines)

    async def _search_text(
        pattern: str,
        path: str = ".",
        glob: str = "*",
        max_results: int = 50,
        include_ignored: bool = False,
    ) -> str | ToolFailure:
        """Grep file contents with a regex pattern under an allowed directory.

        Skips the same noise directories as search_files by default. The *glob*
        parameter restricts by filename (e.g. ``"*.py"``) or, when it contains
        ``/``, by path relative to the search directory. *max_results* caps the
        total match count. Files larger than 1 MiB and binary files (null byte
        in first 8 KiB) are silently skipped.

        Set *include_ignored* to True to also search dependency, cache, and
        build directories, including .sandbox_* files and directories.
        """
        import re as _re

        _MAX_FILE_BYTES = 1_048_576   # 1 MiB
        _MAX_PATTERN_LEN = 500

        if len(pattern) > _MAX_PATTERN_LEN:
            return ToolFailure(
                "invalid_arguments",
                f"pattern too long ({len(pattern)} > {_MAX_PATTERN_LEN})",
                True,
                f"Use a regex of at most {_MAX_PATTERN_LEN} characters.",
            )

        search = access.resolve(path)
        if not search.exists():
            return ToolFailure("file_not_found", f"Path not found: {search}", False,
                               "List the parent directory and use an existing path; do not repeat this path unchanged.", details={"path": str(search)})
        if not search.is_dir():
            return ToolFailure("not_a_directory", f"Not a directory: {search}", False,
                               "Use a directory path for search_text; to search one file, pass its directory as path and its name as glob.", details={"path": str(search)})
        try:
            compiled = _re.compile(pattern)
        except _re.error as exc:
            return ToolFailure(
                "invalid_arguments",
                f"Invalid regex pattern: {exc}",
                True,
                "Correct the regex; escape characters such as ( [ . * with a backslash to match them literally.",
            )

        max_results = max(1, max_results)
        results: list[str] = []
        capped = False
        candidate_files = 0
        glob_files = 0

        for dirpath_str, dirnames, filenames in os.walk(str(search)):
            # Prune noise directories in-place so we don't descend into them.
            if not include_ignored:
                dirnames[:] = [
                    d for d in dirnames
                    if not _is_search_noise(d)
                ]
            relative_dir = Path(dirpath_str).relative_to(search).parts

            for fname in filenames:
                if not include_ignored and _is_search_noise(fname):
                    continue
                candidate_files += 1
                if not _search_glob_matches((*relative_dir, fname), glob):
                    continue
                glob_files += 1
                fp = Path(dirpath_str) / fname
                # Check size before reading
                try:
                    st = fp.stat()
                except OSError:
                    continue
                if st.st_size > _MAX_FILE_BYTES:
                    continue
                # Read once, then reject obvious binary content before decoding.
                try:
                    raw = fp.read_bytes()
                except OSError:
                    continue
                if b"\x00" in raw[:8192]:
                    continue
                text = raw.decode("utf-8", errors="replace")
                shown = _tool_path(fp)
                for lineno, line in enumerate(text.splitlines(), 1):
                    if compiled.search(line):
                        if len(results) >= max_results:
                            # One more match exists, so the list really is cut short.
                            capped = True
                            break
                        results.append(f"{shown}:{lineno}: {line.rstrip()}")
                if capped:
                    break
            if capped:
                break

        if not results:
            if candidate_files and not glob_files:
                return (
                    f"No files in {search} match glob '{glob}', so no file contents were searched. "
                    "A glob without '/' is matched against file names; a glob with '/' is matched "
                    "against the path relative to the search directory, where '**/' spans directories."
                )
            content_suggestions = _content_near_miss_suggestions(
                search, pattern, include_ignored=include_ignored, glob_filter=glob
            )
            suggestions = _near_miss_suggestions(
                search, pattern, include_ignored=include_ignored, glob_filter=glob
            )
            content_only = [
                name for name in content_suggestions if name not in suggestions
            ]
            if suggestions or content_only:
                lines = [f"No lines matching '{pattern}' found in {search}"]
                if suggestions:
                    lines.append("Did you mean one of these files?")
                    lines.extend(f"- {_tool_path(search / name)}" for name in suggestions)
                if content_only:
                    lines.append("Content near-miss in:")
                    lines.extend(f"- {_tool_path(search / name)}" for name in content_only)
                return "\n".join(lines)
            return f"No lines matching '{pattern}' found in {search}"
        if capped:
            return PartialResult(
                f"{len(results)} match(es) (capped at {max_results}); more exist, so raise "
                "max_results or narrow the pattern, path or glob:\n" + "\n".join(results)
            )
        return f"{len(results)} match(es):\n" + "\n".join(results)

    async def _edit_file(
        path: str,
        old: str,
        new: str,
        expected_sha256: str = "",
        _task_id: str = "",
    ) -> str | ToolFailure:
        full = access.resolve(path, write=True)
        if not full.exists():
            return ToolFailure("file_not_found", f"File not found: {full}", False,
                               "List the parent directory and read the intended file before editing it.", details={"path": str(full)})
        if not old:
            return ToolFailure("invalid_arguments", "old must not be empty", False,
                               "Read the file and supply an exact non-empty old string; use write_file to create a file.")
        raw = full.read_bytes()
        before_sha256 = hashlib.sha256(raw).hexdigest()
        normalized_expected = expected_sha256.strip().lower()
        if normalized_expected and normalized_expected != before_sha256:
            return ToolFailure(
                code="file_changed",
                message=(
                    f"File version changed at {full}; expected SHA-256 {normalized_expected}, "
                    f"found {before_sha256}"
                ),
                retryable=True,
                recovery_hint="Read the current file and regenerate the edit against the latest content.",
                details={
                    "path": str(full),
                    "expected_sha256": normalized_expected,
                    "actual_sha256": before_sha256,
                },
            )
        has_bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            content = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            return f"Error: file is not valid UTF-8: {exc}"

        match_old = old
        replacement = new
        count = content.count(match_old)
        if count == 0 and "\r\n" in content and "\r\n" not in old:
            match_old = old.replace("\n", "\r\n")
            replacement = new.replace("\n", "\r\n")
            count = content.count(match_old)
        elif count == 0 and "\r\n" not in content and "\r\n" in old:
            match_old = old.replace("\r\n", "\n")
            replacement = new.replace("\r\n", "\n")
            count = content.count(match_old)
        fuzzy_span: tuple[int, int] | None = None
        if count == 0:
            # Fuzzy fallback: find closest whitespace-normalized match
            lines = content.splitlines(keepends=True)
            old_lines = [line.rstrip() for line in old.split("\n")]
            # A line break at the end of old belongs to its last line; it is
            # not one more (blank) line to match.
            through_line_end = len(old_lines) > 1 and old.endswith("\n")
            if through_line_end:
                old_lines.pop()
            best_line = -1
            best_score = 0
            perfect_lines: list[int] = []
            for i in range(len(lines)):
                remaining = lines[i:]
                matched = 0
                for j, ol in enumerate(old_lines):
                    if j >= len(remaining):
                        break
                    if remaining[j].rstrip() == ol:
                        matched += 1
                    else:
                        break
                if matched > best_score:
                    best_score = matched
                    best_line = i
                if matched == len(old_lines) and any(old_lines):
                    perfect_lines.append(i)
            if len(perfect_lines) > 1:
                return ToolFailure(
                    code="edit_match_ambiguous",
                    message=(
                        f"Edit text matched {len(perfect_lines)} places in {full} "
                        "when trailing whitespace is ignored"
                    ),
                    retryable=True,
                    recovery_hint="Include more surrounding context so old matches exactly once.",
                    details={"path": str(full), "matches": len(perfect_lines)},
                )
            # If fuzzy matcher found a perfect match (all lines equal
            # after rstrip), use the actual file text for replacement
            # instead of failing.  This handles trailing-whitespace drift.
            if perfect_lines:
                first_line = perfect_lines[0]
                matched_lines = "".join(lines[first_line:first_line + len(old_lines)])
                start = sum(len(line) for line in lines[:first_line])
                end = start + len(matched_lines)
                if not through_line_end:
                    # old stops before the line break, so the last line keeps
                    # its line ending and any trailing whitespace.
                    last_line = lines[first_line + len(old_lines) - 1]
                    end -= len(last_line) - len(last_line.rstrip())
                # Preserve the file's line-ending style in replacement.
                replacement = new.replace("\r\n", "\n")
                if "\r\n" in matched_lines or ("\n" not in matched_lines and "\r\n" in content):
                    replacement = replacement.replace("\n", "\r\n")
                # Report and replace the text that is really in the file.
                match_old = content[start:end]
                fuzzy_span = (start, end)
            else:
                context = ""
                if best_line >= 0 and best_score > 0:
                    start = max(0, best_line - 2)
                    end = min(len(lines), best_line + len(old_lines) + 2)
                    snippet = "".join(lines[start:end])
                    context = (
                        f"\nClosest match at line {best_line + 1} "
                        f"({best_score}/{len(old_lines)} lines match):\n"
                        f"```\n{snippet}```"
                    )
                return ToolFailure(
                    code="edit_match_not_found",
                    message=f"Exact edit text was not found in {full}{context}",
                    retryable=True,
                    recovery_hint="Read the target region and retry with exact current text.",
                    details={"path": str(full), "matches": 0, "closest_line": best_line + 1 if best_line >= 0 else None},
                )
        if count > 1:
            return ToolFailure(
                code="edit_match_ambiguous",
                message=f"Exact edit text matched {count} places in {full}",
                retryable=True,
                recovery_hint="Include more surrounding context so old matches exactly once.",
                details={"path": str(full), "matches": count},
            )
        if fuzzy_span is not None:
            updated = content[:fuzzy_span[0]] + replacement + content[fuzzy_span[1]:]
        else:
            updated = content.replace(match_old, replacement, 1)
        encoded = updated.encode("utf-8")
        if has_bom:
            encoded = b"\xef\xbb\xbf" + encoded
        temporary: Path | None = None
        checkpoint = _capture_checkpoint(
            [full], operation="edit_file", task_id=_task_id
        )
        checkpoint_id = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=full.parent,
                prefix=f".{full.name}.agent-edit-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, full)
            temporary = None
            checkpoint_id = _finalize_checkpoint(checkpoint)
            # Verify the edit actually landed on disk: the returned sha must
            # match the bytes on disk, not just the in-memory buffer. This
            # turns silent write failures (e.g. locked file views) into an
            # explicit, retryable error instead of a false success.
            try:
                landed = Path(full).read_bytes()
            except OSError as exc:
                return ToolFailure(
                    code="edit_write_verify_failed",
                    message=f"Committed edit to {full} but could not re-read it: {exc}",
                    retryable=True,
                    recovery_hint="Check the file is accessible and retry.",
                    details={"path": str(full)},
                )
            if hashlib.sha256(landed).hexdigest() != hashlib.sha256(encoded).hexdigest():
                return ToolFailure(
                    code="edit_write_mismatch",
                    message=(
                        f"Edit reported success but did not land on disk for {full} "
                        "(content mismatch after write)"
                    ),
                    retryable=True,
                    recovery_hint=(
                        "The file may be under an external watcher or locked by "
                        "another process; re-read and retry the edit."
                    ),
                    details={"path": str(full)},
                )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return json.dumps({
            "path": str(full),
            "replacements": 1,
            "removed_chars": len(match_old),
            "added_chars": len(replacement),
            "before_sha256": before_sha256,
            "after_sha256": hashlib.sha256(encoded).hexdigest(),
            "encoding": "utf-8-sig" if has_bom else "utf-8",
            "line_endings": "crlf" if "\r\n" in content else "lf",
            "checkpoint_id": checkpoint_id,
        }, ensure_ascii=False)

    def _minimal_editor_max_chars() -> int:
        try:
            value = int(os.getenv("MINIMAL_EDITOR_MAX_OUTPUT_CHARS", "16000"))
        except ValueError:
            value = 16000
        return value if value > 0 else 16000

    def _minimal_editor_clip(content: str) -> str:
        limit = _minimal_editor_max_chars()
        if len(content) <= limit:
            return content
        return PartialResult(content[:limit] + (
            "<response clipped><NOTE>Retry after viewing or searching the relevant range "
            "to keep the next response within the context budget.</NOTE>"
        ))

    def _minimal_editor_file_view(full: Path, view_range: list[int] | None) -> str:
        try:
            content = full.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            return f"Error: file is not valid UTF-8: {exc}"
        lines = content.split("\n")
        initial_line = 1
        final_line: int | None = None
        prompt = (
            f"Here's the content of {full} with line numbers "
            f"(which has a total of {len(lines)} lines)"
        )
        if view_range is not None:
            if (
                len(view_range) != 2
                or not all(isinstance(item, int) and not isinstance(item, bool) for item in view_range)
            ):
                raise ValueError("Invalid view_range. It must contain two integers.")
            initial_line, final_line = view_range
            if initial_line < 1 or initial_line > len(lines):
                raise ValueError(
                    f"Invalid view_range: [{initial_line}, {final_line}]. "
                    f"The first line must be within [1, {len(lines)}]."
                )
            if final_line != -1 and final_line > len(lines):
                raise ValueError(
                    f"Invalid view_range: [{initial_line}, {final_line}]. "
                    f"The second line must be within [1, {len(lines)}]."
                )
            if final_line != -1 and final_line < initial_line:
                raise ValueError(
                    f"Invalid view_range: [{initial_line}, {final_line}]. "
                    "The second line must not precede the first."
                )
            prompt += f" with view_range=[{initial_line}, {final_line}]"
            selected = lines[initial_line - 1:] if final_line == -1 else lines[initial_line - 1:final_line]
        else:
            selected = lines
        numbered = "\n".join(
            f"{initial_line + index:6d}  {line.rstrip(chr(13))}"
            for index, line in enumerate(selected)
        )
        return _minimal_editor_clip(f"{prompt}:\n{numbered}\n")

    def _minimal_editor_directory_view(full: Path) -> str:
        rows = [f"d\t{full}"]

        def visit(directory: Path, depth: int) -> None:
            try:
                entries = sorted(directory.iterdir(), key=lambda item: str(item))
            except OSError as exc:
                rows.append(f"[directory read failed: {exc}]")
                return
            for entry in entries:
                if entry.name.startswith(".") or entry.name in {"node_modules", "__pycache__"}:
                    continue
                try:
                    kind = "d" if entry.is_dir() else "f" if entry.is_file() else "?"
                except OSError:
                    kind = "?"
                rows.append(f"{kind}\t{entry}")
                if kind == "d" and depth < 2:
                    visit(entry, depth + 1)

        visit(full, 1)
        rows.sort(key=lambda row: row.split("\t", 1)[-1])
        listing = _minimal_editor_clip("\n".join(rows) + "\n")
        text = (
            f"Here're the files and directories up to 2 levels deep in {full}, "
            "excluding hidden items, node_modules, and Python cache directories:\n"
            + listing
        )
        return PartialResult(text) if isinstance(listing, PartialResult) else text

    def _minimal_editor_permission_check(args: dict) -> dict | None:
        command = str(args.get("command") or "")
        if command == "view":
            return permission_check("View file", write=False)(args)
        mapped = dict(args)
        if command == "create":
            mapped["content"] = args.get("file_text") or ""
            return permission_check("Create file", write=True)(mapped)
        if command == "str_replace":
            mapped["old"] = args.get("old_str") or ""
            mapped["new"] = args.get("new_str") or ""
            return permission_check("Edit file", write=True)(mapped)
        if command == "insert":
            return permission_check("Insert into file", write=True)(args)
        return None

    async def _str_replace_editor(
        command: str,
        path: str,
        file_text: str | None = None,
        insert_line: int | None = None,
        new_str: str | None = None,
        old_str: str | None = None,
        view_range: list[int] | None = None,
        _task_id: str = "",
    ) -> str | ToolFailure:
        if command == "view":
            full = access.resolve(path)
            if not full.exists():
                return ToolFailure(
                    code="file_not_found",
                    message=f"The path {full} does not exist. Please provide a valid path.",
                    retryable=True,
                    recovery_hint="Check the path or search the workspace before retrying.",
                    details={"path": str(full)},
                )
            if full.is_dir():
                if view_range is not None:
                    raise ValueError("view_range is not allowed when path points to a directory.")
                return _minimal_editor_directory_view(full)
            if not full.is_file():
                return ToolFailure(
                    code="not_a_file",
                    message=f"The path {full} is not a regular file or directory.",
                    retryable=True,
                    recovery_hint="Provide a regular file or directory path.",
                    details={"path": str(full)},
                )
            return _minimal_editor_file_view(full, view_range)

        if command == "create":
            if file_text is None:
                raise ValueError("Parameter file_text is required for command: create")
            existing = access.resolve(path, write=True)
            if existing.exists():
                # write_file's own refusal points at tools this editor's caller may not have.
                return ToolFailure(
                    code="existing_file_requires_edit",
                    message=f"File already exists at: {existing}. The create command does not overwrite files.",
                    retryable=True,
                    recovery_hint=(
                        "Use the view command to read the file, then change it with "
                        "str_replace or insert."
                    ),
                    details={"path": str(existing)},
                )
            result = await _write_file(path, file_text, False, "", _task_id)
            if isinstance(result, ToolFailure):
                return result
            return f"New file created successfully at: {access.resolve(path)}"

        if command == "str_replace":
            if old_str is None:
                raise ValueError("Parameter old_str is required for command: str_replace")
            if not old_str:
                raise ValueError("Parameter old_str must be non-empty for command: str_replace")
            result = await _edit_file(path, old_str, new_str or "", "", _task_id)
            if isinstance(result, ToolFailure):
                return result
            return f"The file {access.resolve(path)} has been edited successfully."

        if command == "insert":
            if insert_line is None:
                raise ValueError("Parameter insert_line is required for command: insert")
            if new_str is None:
                raise ValueError("Parameter new_str is required for command: insert")
            full = access.resolve(path, write=True)
            if not full.exists() or not full.is_file():
                return ToolFailure(
                    code="file_not_found",
                    message=f"The path {full} does not exist as a regular file.",
                    retryable=True,
                    recovery_hint="View the file before inserting into it.",
                    details={"path": str(full)},
                )
            raw = full.read_bytes()
            before_sha256 = hashlib.sha256(raw).hexdigest()
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                return ToolFailure(
                    code="file_not_utf8",
                    message=f"File is not valid UTF-8: {full}",
                    retryable=False,
                    recovery_hint="Convert the file to UTF-8 before editing it.",
                    details={"path": str(full), "error": str(exc)},
                )
            if not isinstance(insert_line, int) or isinstance(insert_line, bool):
                raise ValueError("insert_line must be an integer")
            newline = "\r\n" if "\r\n" in content else "\n"
            logical_lines = content.replace("\r\n", "\n").split("\n")
            if insert_line < 0 or insert_line > len(logical_lines):
                raise ValueError(
                    f"Invalid insert_line: {insert_line}. "
                    f"It must be within [0, {len(logical_lines)}]."
                )
            inserted_lines = new_str.replace("\r\n", "\n").split("\n")
            updated = newline.join(
                logical_lines[:insert_line] + inserted_lines + logical_lines[insert_line:]
            )
            if content:
                result = await _edit_file(path, content, updated, before_sha256, _task_id)
            else:
                result = await _write_file(path, updated, True, before_sha256, _task_id)
            if isinstance(result, ToolFailure):
                return result
            return f"The file {full} has been edited successfully."

        raise ValueError(
            "Unknown command. Allowed options are: view, create, str_replace, insert."
        )

    async def _begin_file_write(
        path: str,
        overwrite: bool = False,
        expected_size: int | None = None,
    ) -> str | ToolFailure:
        target = access.resolve(path, write=True)
        if expected_size is not None and expected_size < 0:
            return ToolFailure(
                code="invalid_arguments",
                message="expected_size must be zero or greater",
                retryable=True,
                recovery_hint="Omit expected_size or pass the final UTF-8 byte count.",
            )
        if target.exists() and not overwrite:
            return ToolFailure(
                code="target_exists",
                message=f"Target already exists and overwrite is false: {target}",
                retryable=True,
                recovery_hint="Pass overwrite=true to replace the whole file, or choose another path.",
                details={"path": str(target)},
            )
        transaction_dir.mkdir(parents=True, exist_ok=True)
        write_id = uuid.uuid4().hex
        temp_path = transaction_dir / f"{write_id}.part"
        manifest_path = transaction_dir / f"{write_id}.manifest.json"
        temp_path.write_bytes(b"")
        transaction = FileWriteTransaction(
            write_id=write_id,
            requested_path=path,
            target=target,
            temp_path=temp_path,
            overwrite=overwrite,
            expected_size=expected_size,
            manifest_path=manifest_path,
            created_at=time.time(),
        )
        transactions[write_id] = transaction
        persist_transaction(transaction)
        audit_transaction("begun", transaction)
        return json.dumps({
            "write_id": write_id,
            "path": str(target),
            "next_sequence": 0,
            "chunk_char_limit": chunk_char_limit,
            "expected_size": expected_size,
            "status": "open",
        }, ensure_ascii=False)

    def unknown_write_id(write_id: str) -> ToolFailure:
        return ToolFailure(
            code="unknown_write_id",
            message=f"Unknown or closed write_id: {write_id}",
            retryable=False,
            recovery_hint=(
                "This transaction was already committed, aborted or discarded. To write "
                "the file, call begin_file_write again and send every chunk from sequence 0."
            ),
            details={"write_id": write_id},
        )

    async def _write_file_chunk(
        write_id: str,
        sequence: int,
        content: str,
    ) -> str | ToolFailure:
        transaction = transactions.get(write_id)
        if transaction is None:
            return unknown_write_id(write_id)
        if sequence != transaction.next_sequence:
            return ToolFailure(
                code="chunk_out_of_order",
                message=(
                    f"out-of-order chunk for {write_id}; "
                    f"expected sequence {transaction.next_sequence}, received {sequence}"
                ),
                retryable=True,
                recovery_hint=(
                    f"Send the chunk with sequence {transaction.next_sequence} next. "
                    "Chunks already accepted are kept; this one was not written."
                ),
                details={
                    "write_id": write_id,
                    "expected_sequence": transaction.next_sequence,
                    "received_sequence": sequence,
                },
            )
        if len(content) > chunk_char_limit:
            return ToolFailure(
                code="chunk_too_large",
                message=(
                    f"Chunk has {len(content)} characters; hard limit is "
                    f"{chunk_char_limit}"
                ),
                retryable=True,
                recovery_hint="Retry this sequence with a smaller complete chunk.",
                details={
                    "write_id": write_id,
                    "sequence": sequence,
                    "characters": len(content),
                    "limit": chunk_char_limit,
                },
            )
        encoded = content.encode("utf-8")
        expected_bytes = transaction.bytes_written + len(encoded)
        with open(transaction.temp_path, "ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        payload = transaction.temp_path.read_bytes()
        if len(payload) != expected_bytes:
            actual_bytes = len(payload)
            discard_transaction(transaction, "chunk_integrity_failed")
            return ToolFailure(
                code="transaction_integrity_failed",
                message=(
                    f"Transactional write storage mismatch after sequence {sequence}: "
                    f"expected {expected_bytes} bytes, found {actual_bytes}"
                ),
                retryable=False,
                recovery_hint="Start a new file transaction; the damaged temporary data was discarded.",
                partial=True,
                details={
                    "write_id": write_id,
                    "sequence": sequence,
                    "expected_bytes": expected_bytes,
                    "actual_bytes": actual_bytes,
                },
            )
        transaction.next_sequence += 1
        transaction.chars += len(content)
        transaction.bytes_written = expected_bytes
        transaction.running_sha256 = hashlib.sha256(payload).hexdigest()
        persist_transaction(transaction)
        chunk_sha256 = hashlib.sha256(encoded).hexdigest()
        audit_transaction(
            "chunk",
            transaction,
            sequence=sequence,
            chunk_sha256=chunk_sha256,
        )
        return json.dumps({
            "write_id": write_id,
            "accepted_sequence": sequence,
            "next_sequence": transaction.next_sequence,
            "chars": transaction.chars,
            "bytes": transaction.bytes_written,
            "chunk_sha256": chunk_sha256,
            "running_sha256": transaction.running_sha256,
            "status": "open",
        }, ensure_ascii=False)

    async def _commit_file_write(
        write_id: str,
        expected_sha256: str = "",
        _task_id: str = "",
    ) -> str | ToolFailure:
        transaction = transactions.get(write_id)
        if transaction is None:
            return unknown_write_id(write_id)
        try:
            current_target = access._candidate(transaction.requested_path)
        except (OSError, ValueError) as exc:
            return ToolFailure(
                code="transaction_target_unavailable",
                message=f"Could not re-resolve transaction target: {exc}",
                retryable=True,
                recovery_hint=(
                    "The target was not modified and the transaction is still open. "
                    "Commit again once the path is reachable, or abort it."
                ),
                details={"write_id": write_id, "path": str(transaction.target)},
            )
        if os.path.normcase(str(current_target)) != os.path.normcase(str(transaction.target)):
            return ToolFailure(
                code="transaction_target_changed",
                message=(
                    "Transaction target changed after approval; "
                    f"was {transaction.target}, now {current_target}"
                ),
                retryable=False,
                recovery_hint=(
                    "The target was not modified. Abort this transaction and begin a new "
                    "one for the intended path."
                ),
                details={
                    "write_id": write_id,
                    "path": str(transaction.target),
                    "current_path": str(current_target),
                },
            )

        payload = transaction.temp_path.read_bytes()
        actual_bytes = len(payload)
        if actual_bytes != transaction.bytes_written:
            return ToolFailure(
                code="transaction_integrity_failed",
                message=(
                    f"Transactional write byte count mismatch for {write_id}: "
                    f"accepted {transaction.bytes_written} bytes, found {actual_bytes}"
                ),
                retryable=False,
                recovery_hint="Abort this transaction and start a new one; the target was not modified.",
                partial=True,
                details={
                    "write_id": write_id,
                    "accepted_bytes": transaction.bytes_written,
                    "actual_bytes": actual_bytes,
                    "accepted_chunks": transaction.next_sequence,
                },
            )
        digest = hashlib.sha256(payload).hexdigest()
        if digest != transaction.running_sha256:
            return ToolFailure(
                code="transaction_integrity_failed",
                message=(
                    f"Transactional write running hash mismatch for {write_id}: "
                    f"accepted {transaction.running_sha256}, found {digest}"
                ),
                retryable=False,
                recovery_hint="Abort this transaction and start a new one; the target was not modified.",
                partial=True,
                details={
                    "write_id": write_id,
                    "accepted_sha256": transaction.running_sha256,
                    "actual_sha256": digest,
                    "accepted_chunks": transaction.next_sequence,
                },
            )
        if transaction.expected_size is not None and len(payload) != transaction.expected_size:
            return ToolFailure(
                code="transaction_size_mismatch",
                message=(
                    f"Transactional write size mismatch for {write_id}: expected "
                    f"{transaction.expected_size} UTF-8 bytes, found {len(payload)}"
                ),
                retryable=True,
                recovery_hint=(
                    "expected_size is optional. Abort and restart without it, or provide "
                    "the exact final UTF-8 byte count."
                ),
                details={
                    "write_id": write_id,
                    "expected_bytes": transaction.expected_size,
                    "actual_bytes": len(payload),
                },
            )
        normalized_expected = expected_sha256.strip().lower()
        if normalized_expected and digest != normalized_expected:
            return ToolFailure(
                code="transaction_hash_mismatch",
                message=(
                    f"Transactional write SHA-256 mismatch for {write_id}: "
                    f"expected {normalized_expected}, found {digest}"
                ),
                retryable=True,
                recovery_hint="Abort and restart with the intended complete content.",
                details={
                    "write_id": write_id,
                    "expected_sha256": normalized_expected,
                    "actual_sha256": digest,
                },
            )
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            return ToolFailure(
                code="transaction_integrity_failed",
                message=f"Transactional write is not valid UTF-8: {exc}",
                retryable=False,
                recovery_hint="Abort this transaction and restart with valid UTF-8 text.",
                partial=True,
                details={"write_id": write_id},
            )
        target = transaction.target
        if target.exists() and not transaction.overwrite:
            return ToolFailure(
                code="target_exists",
                message=f"Target already exists and overwrite is false: {target}",
                retryable=False,
                recovery_hint=(
                    "The file appeared after this transaction began and was not modified. "
                    "Abort this transaction, then begin a new one with overwrite=true or "
                    "another path."
                ),
                details={"write_id": write_id, "path": str(target)},
            )
        previous_text = ""
        target_existed = target.exists()
        if target_existed:
            previous_text = target.read_text(encoding="utf-8", errors="replace")

        target.parent.mkdir(parents=True, exist_ok=True)
        checkpoint = _capture_checkpoint(
            [target], operation="commit_file_write", task_id=_task_id
        )
        checkpoint_id = ""
        sibling_temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.agent-commit-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                sibling_temp = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(sibling_temp, target)
            sibling_temp = None
            checkpoint_id = _finalize_checkpoint(checkpoint)
        except Exception as exc:
            return ToolFailure(
                code="transaction_commit_failed",
                message=f"Could not commit file transaction: {exc}",
                retryable=True,
                recovery_hint=(
                    "The target was not replaced and the transaction is still open. "
                    "Check the target directory, then commit again or abort it."
                ),
                details={"write_id": write_id, "path": str(target)},
            )
        finally:
            if sibling_temp is not None:
                try:
                    sibling_temp.unlink(missing_ok=True)
                except OSError:
                    pass

        transaction.temp_path.unlink(missing_ok=True)
        transaction.manifest_path.unlink(missing_ok=True)
        audit_transaction("committed", transaction, sha256=digest)
        transactions.pop(write_id, None)
        previous_lines = previous_text.splitlines()
        current_lines = text.splitlines()
        additions = 0
        deletions = 0
        matcher = difflib.SequenceMatcher(a=previous_lines, b=current_lines, autojunk=False)
        for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
            if tag in {"replace", "delete"}:
                deletions += old_end - old_start
            if tag in {"replace", "insert"}:
                additions += new_end - new_start
        return json.dumps({
            "write_id": write_id,
            "path": str(target),
            "bytes": len(payload),
            "chars": len(text),
            "lines": len(text.splitlines()),
            "sha256": digest,
            "chunks": transaction.next_sequence,
            "artifact_ref": str(target),
            "diff": {
                "kind": "overwrite" if target_existed else "create",
                "additions": additions,
                "deletions": deletions,
            },
            "status": "committed",
            "checkpoint_id": checkpoint_id,
        }, ensure_ascii=False)

    async def _abort_file_write(write_id: str) -> str | ToolFailure:
        transaction = transactions.pop(write_id, None)
        if transaction is None:
            return unknown_write_id(write_id)
        transaction.temp_path.unlink(missing_ok=True)
        transaction.manifest_path.unlink(missing_ok=True)
        audit_transaction("aborted", transaction, reason="tool_call")
        return json.dumps({
            "write_id": write_id,
            "path": str(transaction.target),
            "status": "aborted",
        }, ensure_ascii=False)

    def cleanup_session_transactions(session_id: str, reason: str) -> None:
        for transaction in list(transactions.values()):
            discard_transaction(transaction, f"session_end:{session_id}:{reason}")

    registry.hooks.on_session_end(cleanup_session_transactions)

    async def _apply_patch(
        patch: str,
        dry_run: bool = False,
        _task_id: str = "",
    ) -> str | ToolFailure:
        try:
            operations = parse_patch(patch)
            checkpoint = None if dry_run else _capture_checkpoint(
                [access.resolve(operation.path, write=True) for operation in operations],
                operation="apply_patch",
                task_id=_task_id,
            )
            result = apply_file_patch(
                patch,
                lambda path: access.resolve(path, write=True),
                dry_run=dry_run,
            )
            checkpoint_id = _finalize_checkpoint(checkpoint)
            if checkpoint_id:
                try:
                    result_payload = json.loads(result)
                except json.JSONDecodeError:
                    result_payload = None
                if isinstance(result_payload, dict):
                    result_payload["checkpoint_id"] = checkpoint_id
                    result = json.dumps(result_payload, ensure_ascii=False)
            return result
        except PatchError as exc:
            if isinstance(exc, PatchFormatError):
                recovery_hint = (
                    "Correct the patch text and resend it; no file was changed. The first "
                    "line is *** Begin Patch and the last is *** End Patch, with no "
                    "indentation or code fence. Each file has one *** Update File:, "
                    "*** Add File: or *** Delete File: section. In an Update File section "
                    "every hunk starts with an @@ line and needs at least one context or "
                    "removed line; each hunk line starts with +, - or one space, and a "
                    "blank context line is a single space. Add File lines start with +."
                )
            else:
                recovery_hint = (
                    f"Use paths relative to workspace root {access.workspace}; then read the "
                    "current target region and retry one smaller, complete patch with enough "
                    "unique context."
                )
            return ToolFailure(
                code="patch_precondition_failed",
                message=(
                    f"Patch was not applied: {exc}. "
                    f"Workspace root: {access.workspace}"
                ),
                retryable=True,
                recovery_hint=recovery_hint,
                details={
                    "dry_run": dry_run,
                    "workspace_root": str(access.workspace),
                    "paths": "workspace-relative",
                },
            )

    async def _checkpoint_list(limit: int = 20) -> str:
        return json.dumps(checkpoints.list(limit), ensure_ascii=False)

    async def _checkpoint_restore(checkpoint_id: str) -> str:
        return json.dumps(checkpoints.restore(checkpoint_id), ensure_ascii=False)

    registry.register(ToolDef(
        name="read_file",
        description=(
            "Read a UTF-8 regular file with bounded memory. A read returns up to limit lines "
            f"(default {read_file_limits()[2]}) starting at line offset, or fewer when the page "
            "size cap is reached. A result that is not the whole file begins with a "
            "[File metadata: {...}] line: truncated=true means more of the file remains after "
            "this page, and next_offset is the offset to pass for the next page. Use "
            "byte_offset only when line_truncated=true, which means one line was too long and "
            "was cut: byte_offset=next_byte_offset continues inside that line. "
            "Set include_metadata=true when a stable SHA-256 is needed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Zero-based starting line; pass a previous next_offset to read the next page",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        f"Maximum lines to return (default {read_file_limits()[2]}); "
                        "it also applies to byte_offset reads"
                    ),
                },
                "byte_offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Byte position from a previous next_byte_offset, for continuing a line "
                        "that was cut (line_truncated=true). The read continues from there for "
                        "up to limit lines, so limit=1 stops at the end of that line; line "
                        "numbers are not reported. Cannot be combined with non-zero offset"
                    ),
                },
                "include_metadata": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Always include the metadata line and add the file's SHA-256. "
                        "total_lines is then counted for any file; otherwise it may be null "
                        "for a file larger than "
                        f"{read_file_count_lines_max_bytes // (1024 * 1024)} MiB"
                    ),
                },
                **approval_justification_schema(),
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        fn=_read_file, risk="read", group="files", sandboxed=False,
        permission_check=permission_check("Read file", write=False),
        permission_grant=permission_grant,
        approval_justification=True,
    ))
    registry.register(ToolDef(
        name="stat_file",
        description=(
            "Read fresh file verification metadata without returning the whole file: "
            "UTF-8 byte size, SHA-256, mtime, line count, and a small first/last-line preview."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "preview_lines": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "default": 3,
                    "description": "Number of first and last lines to include",
                },
                **approval_justification_schema(),
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        fn=_stat_file,
        risk="read",
        group="files",
        sandboxed=False,
        permission_check=permission_check("Inspect file metadata", write=False),
        permission_grant=permission_grant,
        approval_justification=True,
    ))
    registry.register(ToolDef(
        name="write_file",
        description=(
            "Create a new UTF-8 text file atomically. Do not use this to modify an existing file: "
            "use edit_file for one exact replacement or apply_patch for multi-hunk changes. "
            "A rare explicit whole-file overwrite requires overwrite=true and the SHA-256 returned "
            "by read_file(include_metadata=true)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {
                    "type": "string",
                    "description": "Complete content for the new file",
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": "Explicitly allow replacing an existing file",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "Required current file SHA-256 when overwrite=true",
                },
                **approval_justification_schema(),
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        fn=_write_file, risk="write", group="files", sandboxed=False,
        postcondition=_verify_write_file,
        permission_check=permission_check("Write file", write=True),
        permission_grant=permission_grant,
        approval_justification=True,
    ))
    registry.register(ToolDef(
        name="begin_file_write",
        description=(
            "Begin a transactional UTF-8 file write. Returns a write_id; "
            "the target is not modified until commit_file_write succeeds."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Final file path"},
                "overwrite": {"type": "boolean", "default": False},
                "expected_size": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional expected final UTF-8 byte count",
                },
            },
            "required": ["path"],
        },
        fn=_begin_file_write,
        risk="write",
        group="files",
        sandboxed=False,
        permission_check=permission_check("Begin transactional file write", write=True),
        permission_grant=permission_grant,
        postcondition=_verify_no_error,
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="write_file_chunk",
        description=(
            "Append one ordered chunk to an open file transaction. "
            f"Sequence starts at 0; each chunk is at most {chunk_char_limit} characters."
        ),
        parameters={
            "type": "object",
            "properties": {
                "write_id": {"type": "string"},
                "sequence": {"type": "integer", "minimum": 0},
                "content": {"type": "string", "maxLength": chunk_char_limit},
            },
            "required": ["write_id", "sequence", "content"],
        },
        fn=_write_file_chunk,
        risk="write",
        group="files",
        sandboxed=False,
        postcondition=_verify_no_error,
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="commit_file_write",
        description=(
            "Validate and atomically commit an open file transaction. "
            "An optional SHA-256 protects against missing or altered chunks."
        ),
        parameters={
            "type": "object",
            "properties": {
                "write_id": {"type": "string"},
                "expected_sha256": {"type": "string"},
            },
            "required": ["write_id"],
        },
        fn=_commit_file_write,
        risk="write",
        group="files",
        sandboxed=False,
        postcondition=_verify_no_error,
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="abort_file_write",
        description="Abort an open file transaction and remove its temporary data.",
        parameters={
            "type": "object",
            "properties": {"write_id": {"type": "string"}},
            "required": ["write_id"],
        },
        fn=_abort_file_write,
        risk="write",
        group="files",
        sandboxed=False,
        postcondition=_verify_no_error,
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="search_files",
        description=(
            "Search an allowed host directory using a glob pattern. Common dependency, "
            "runtime-state, cache, and build directories are skipped by default; an explicit "
            "search rooted inside one of those directories still works. Returns one path per "
            "line, relative to the workspace root (absolute outside it), ready to pass to the "
            "other file tools; at most 100 paths are listed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {"type": "string", "description": "Search directory"},
                "include_ignored": {
                    "type": "boolean",
                    "default": False,
                    "description": "Include dependency, runtime-state, cache, and build directories, including .sandbox_* files and directories.",
                },
                **approval_justification_schema(),
            },
            "required": ["pattern"],
        },
        fn=_search_files, risk="read", group="files", sandboxed=False,
        permission_check=permission_check("Search directory", write=False),
        permission_grant=permission_grant,
        approval_justification=True,
    ))
    registry.register(ToolDef(
        name="search_text",
        description=(
            "Search file contents with a regex pattern inside an allowed directory. "
            "The regex is applied to each line separately, so it cannot match across lines. "
            "Files larger than 1 MiB and binary files (a NUL byte in the first 8 KiB) are "
            "skipped. Common dependency, runtime-state, cache, and build directories are skipped "
            "by default. Use *glob* to filter files (e.g. \"*.py\" or \"src/**/*.py\") and "
            "*max_results* to cap matches. Returns path:line: text for each hit; paths are "
            "relative to the workspace root (absolute outside it), ready to pass to read_file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for in file contents; applied per line"},
                "path": {"type": "string", "description": "Search directory (default: workspace root)", "default": "."},
                "glob": {
                    "type": "string",
                    "description": (
                        "File filter. Without '/' it is matched against the file name at any "
                        "depth (e.g. \"*.py\"); with '/' it is matched against the path relative "
                        "to the search directory, where '**/' spans directories "
                        "(e.g. \"src/**/*.py\")"
                    ),
                    "default": "*",
                },
                "max_results": {"type": "integer", "description": "Maximum total matches (default: 50)", "default": 50, "minimum": 1, "maximum": 200},
                "include_ignored": {"type": "boolean", "description": "Include all ignored dependency, runtime-state, cache, and build paths, including .sandbox_*.", "default": False},
                **approval_justification_schema(),
            },
            "required": ["pattern"],
        },
        fn=_search_text, risk="read", group="files", sandboxed=False,
        permission_check=permission_check("Search file contents", write=False),
        permission_grant=permission_grant,
        approval_justification=True,
    ))
    registry.register(ToolDef(
        name="edit_file",
        description=(
            "Atomically replace one exact string occurrence in an existing UTF-8 file. "
            "Prefer this over write_file for focused edits; it rejects stale or ambiguous matches."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "old": {"type": "string", "description": "Exact string to replace"},
                "new": {"type": "string", "description": "Replacement string"},
                "expected_sha256": {
                    "type": "string",
                    "description": "Optional SHA-256 of the file read before editing",
                },
                **approval_justification_schema(),
            },
            "required": ["path", "old", "new"],
        },
        fn=_edit_file, risk="write", group="files", sandboxed=False,
        permission_check=permission_check("Edit file", write=True),
        permission_grant=permission_grant,
        postcondition=_verify_no_error,
        approval_justification=True,
    ))
    registry.register(ToolDef(
        name="str_replace_editor",
        description=(
            "DSH-compatible editor for viewing, creating, and editing files. "
            "Commands are view, create, str_replace, and insert. View output includes "
            "line numbers; str_replace requires one exact, unique old_str match. "
            "Paths may be workspace-relative or use the /workspace form."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace", "insert"],
                    "description": (
                        "The command to run: view, create, str_replace, or insert."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": "File or directory path; /workspace is accepted.",
                },
                "file_text": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Required for create; omit or use null for other commands.",
                },
                "insert_line": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": (
                        "Required for insert: new_str is inserted after this line number, "
                        "as numbered by view; 0 inserts at the top of the file. "
                        "Omit or use null for other commands."
                    ),
                },
                "new_str": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Replacement or inserted text; omit or use null when unused.",
                },
                "old_str": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "description": "Required for str_replace; omit or use null for other commands.",
                },
                "view_range": {
                    "anyOf": [{
                        "type": "array",
                        "items": {"type": "integer"},
                    }, {"type": "null"}],
                    "description": (
                        "Optional one-based [start, end] range for view; use -1 as end "
                        "to continue through the end of the file."
                    ),
                },
            },
            "required": ["command", "path"],
            "additionalProperties": False,
        },
        fn=_str_replace_editor,
        risk="write",
        group="minimal",
        sandboxed=False,
        permission_check=_minimal_editor_permission_check,
        permission_grant=permission_grant,
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="apply_patch",
        description=(
            "Apply a validated multi-file patch. "
            f"Every path must be relative to workspace root {access.workspace}; absolute "
            "paths and '..' are rejected. Prefer this for existing code instead of resending "
            "whole files.\n\n"
            "Format (not standard unified diff):\n"
            "The first line is `*** Begin Patch` and the last line is `*** End Patch`. "
            "Write every line of the patch without extra indentation.\n"
            "Each file has one section that starts with a directive line (these replace the "
            "unified-diff ---/+++ headers):\n"
            "`*** Update File: <relpath>` modifies an existing file with hunks\n"
            "`*** Add File: <relpath>` creates a new file; every content line is prefixed with +\n"
            "`*** Delete File: <relpath>` removes a file; no content lines\n"
            "\n"
            "Each hunk starts with a line beginning with @@. Line numbers after @@ are "
            "ignored: a hunk is located by its context and removed lines, which together "
            "must match exactly one place in the file. Hunk line prefixes: + (add), "
            "- (remove), one space (context); a blank context line is a single space.\n"
            "Do not include --- a/ or +++ b/ unified-diff headers.\n"
            "\n"
            "Example:\n"
            "*** Begin Patch\n"
            "*** Update File: src/main.py\n"
            "@@\n"
            " def hello():\n"
            "-    print(\"old\")\n"
            "+    print(\"new\")\n"
            "+    print(\"extra\")\n"
            "*** End Patch"
        ),
        parameters={
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": (
                        "Complete patch text, from the line *** Begin Patch to the line "
                        "*** End Patch.\n"
                        f"Paths are relative to {access.workspace}\n"
                        "\n"
                        "Directives:\n"
                        "*** Update File: <relpath>, then hunks (each starts with @@; no ---/+++ headers)\n"
                        "*** Add File: <relpath>, then lines prefixed with +\n"
                        "*** Delete File: <relpath>, no content needed\n"
                        "\n"
                        "See the tool description for a full example."
                    ),
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "Validate and summarize without changing files",
                },
                **approval_justification_schema(),
            },
            "required": ["patch"],
        },
        fn=_apply_patch,
        risk="write",
        group="files",
        sandboxed=False,
        permission_check=patch_permission_check,
        permission_grant=patch_permission_grant,
        postcondition=_verify_no_error,
        approval_justification=True,
    ))
    registry.register(ToolDef(
        name="checkpoint_list",
        description=(
            "List recent local Agent-only file checkpoints. These are short-lived "
            "undo points separate from Git and do not include manual user edits."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
        fn=_checkpoint_list,
        risk="read",
        group="files",
        sandboxed=False,
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="checkpoint_restore",
        description=(
            "Restore one Agent-only checkpoint only when every affected file still "
            "matches the exact post-change hash. Refuses to overwrite later user edits."
        ),
        parameters={
            "type": "object",
            "properties": {"checkpoint_id": {"type": "string"}},
            "required": ["checkpoint_id"],
            "additionalProperties": False,
        },
        fn=_checkpoint_restore,
        risk="write",
        approval="always",
        group="files",
        sandboxed=False,
        postcondition=_verify_no_error,
        expose_by_default=False,
    ))
