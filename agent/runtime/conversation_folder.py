"""Read and check the folder that owns a saved conversation."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .session_store import SessionStore

if TYPE_CHECKING:
    from .context import AgentContext

logger = logging.getLogger(__name__)

_FileStamp = tuple[int | None, int | None]
_Folder = tuple[str, str]
_HEADER_CACHE: dict[tuple[str, int | None, int | None], _Folder] = {}
_RECALL_CACHE: dict[Path, tuple[tuple[_FileStamp, _FileStamp], dict[str, str]]] = {}
_CACHE_LOCK = threading.Lock()


def current_folder() -> str:
    return os.path.realpath(os.environ.get("SANDBOX_WORKDIR") or os.getcwd())


def same_folder(a: str, b: str) -> bool:
    return bool(a and b and os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b)))


def folder_label(path: str) -> str:
    if not path:
        return "(no folder recorded)"
    try:
        relative = Path(path).relative_to(Path.home())
    except ValueError:
        return path
    return str(Path("~") / relative)


def _read_header(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _header_folder(path: Path) -> _Folder:
    data = _read_header(path) or {}
    workspace = data.get("workspace")
    if isinstance(workspace, str) and workspace:
        return workspace, "recorded"
    snapshot = data.get("guidance_snapshot")
    scope = snapshot.get("scope") if isinstance(snapshot, dict) else None
    workdir = scope.get("workdir") if isinstance(scope, dict) else None
    if isinstance(workdir, str) and workdir:
        return workdir, "guidance"
    return "", ""


def _recall_path() -> Path:
    from .session_recall import configured_session_recall_path

    return configured_session_recall_path().resolve()


def _read_only_recall(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


def inferred_folder(store: SessionStore) -> _Folder:
    folder, source = _header_folder(store.header_path)
    if folder:
        return folder, source
    path = _recall_path()
    if path.exists():
        try:
            with closing(_read_only_recall(path)) as connection:
                row = connection.execute(
                    "SELECT workspace_root FROM sessions WHERE source_session_key = ? "
                    "AND workspace_root IS NOT NULL AND workspace_root <> '' ORDER BY started_at DESC LIMIT 1",
                    (store.logical_path.stem,),
                ).fetchone()
        except sqlite3.Error:
            pass
        else:
            if row:
                return row[0], "recall"
    return "", ""


def _file_stamp(path: Path) -> _FileStamp:
    try:
        stat = path.stat()
    except OSError:
        return None, None
    return stat.st_size, stat.st_mtime_ns


def _read_recall_folders(path: Path) -> dict[str, str]:
    try:
        with closing(_read_only_recall(path)) as connection:
            rows = connection.execute(
                "SELECT source_session_key, workspace_root FROM sessions WHERE source_session_key IS NOT NULL "
                "AND workspace_root IS NOT NULL AND workspace_root <> '' ORDER BY started_at DESC"
            )
            folders: dict[str, str] = {}
            for key, folder in rows:
                folders.setdefault(key, folder)
            return folders
    except sqlite3.Error:
        return {}


def listing_folder(store: SessionStore) -> _Folder:
    header_path = store.header_path
    header_key = (str(header_path), *_file_stamp(header_path))
    with _CACHE_LOCK:
        result = _HEADER_CACHE.get(header_key)
        if result is None:
            result = _header_folder(header_path)
            _HEADER_CACHE[header_key] = result
            if len(_HEADER_CACHE) > 4096:
                _HEADER_CACHE.clear()
        if result[0]:
            return result

        path = _recall_path()
        db_stamp = _file_stamp(path)
        if db_stamp[0] is None:
            _RECALL_CACHE.pop(path, None)
            return "", ""
        stamp = (db_stamp, _file_stamp(Path(f"{path}-wal")))
        cached = _RECALL_CACHE.get(path)
        if cached is None or cached[0] != stamp:
            cached = (stamp, _read_recall_folders(path))
            _RECALL_CACHE[path] = cached
        folder = cached[1].get(store.logical_path.stem, "")
        return (folder, "recall") if folder else ("", "")


def clear_caches() -> None:
    with _CACHE_LOCK:
        _HEADER_CACHE.clear()
        _RECALL_CACHE.clear()


def check(folder: str, current: str) -> Literal["same", "unbound", "other", "missing"]:
    if not folder:
        return "unbound"
    if same_folder(folder, current):
        return "same"
    return "other" if os.path.isdir(folder) else "missing"


@dataclass(frozen=True)
class FolderDecision:
    status: Literal["open", "refuse"]
    folder: str
    rebind: bool
    notice: str
    code: str = ""
    system: bool = False


def decide(
    session_path: str | Path, current: str, *, here: bool = False, move_from: str = "", system: bool = False
) -> FolderDecision:
    name = Path(session_path).stem
    try:
        store = SessionStore(session_path)
        if not store.exists:
            return FolderDecision("open", "", False, "")
        folder, _ = inferred_folder(store)
    except (OSError, ValueError):
        return FolderDecision("open", "", False, "")
    state = check(folder, current)
    if state == "same":
        return FolderDecision("open", folder, False, "")
    if state == "unbound":
        return FolderDecision("open", folder, True, f"Session '{name}' had no recorded folder; it is now recorded as {current}.")
    if system:
        return FolderDecision("open", folder, True, "", system=True)
    if here or (move_from and same_folder(move_from, folder)):
        notice = (f"Moved session '{name}' from {folder} to {current}. Project instructions for {current} "
                  "apply from the next turn, added after the earlier ones.")
        return FolderDecision("open", folder, True, notice)
    if state == "other":
        notice = f"Session '{name}' belongs to {folder}. Open it there, or use /session {name} --here to move it to {current}."
    else:
        notice = f"Session '{name}' belongs to {folder}, which no longer exists. Use /session {name} --here to move it to {current}."
    return FolderDecision("refuse", folder, False, notice, code="workspace_mismatch")


def apply_decision(context: AgentContext, decision: FolderDecision) -> str:
    if decision.rebind:
        old = context.workspace
        context.workspace = current_folder()
        if decision.system and old and not same_folder(old, context.workspace):
            logger.info("conversation %s moved with its owner: %s -> %s", Path(context.session_path).stem, old, context.workspace)
    return decision.notice if decision.status == "open" else ""


def rebind_system_context(context: AgentContext, reason: str) -> None:
    current = current_folder()
    if context.workspace and not same_folder(context.workspace, current):
        logger.info("%s conversation moved with its owner: %s -> %s", reason, context.workspace, current)
        context.workspace = current
