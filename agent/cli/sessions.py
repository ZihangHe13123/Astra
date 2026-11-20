"""Session helpers shared by CLI and Ink backend."""

from agent.runtime.paths import sessions_dir

import os
import re
import time
import unicodedata
from pathlib import Path

from agent.runtime.session_store import SessionStore

PROJECT_ROOT = Path(__file__).parent.parent.parent
SESSION_DIR = sessions_dir(PROJECT_ROOT)
SESSION_DEFAULT = SESSION_DIR / "default.json"
SESSION_EXPORT_DIR = SESSION_DIR / "exports"

_SESSION_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff.-]+$", re.UNICODE)
_ARTIFACT_SUFFIXES = (
    ".tool-failures.jsonl",
    ".context-index-feedback.jsonl",
    ".hindsight-sync.json",
    ".subagents.jsonl",
    ".raw-tool-calls.jsonl",
    ".approvals.jsonl",
)
_SUBAGENT_CONV_RE = re.compile(r"^(.+)\.[A-Za-z0-9][A-Za-z0-9_-]{0,127}\.conv\.")
API_SESSION_ID_MAX_LENGTH = 64
API_SESSION_ID_MAX_UTF8_BYTES = 192


def _entry_mtime(entry: os.DirEntry[str]) -> float | None:
    try:
        return entry.stat(follow_symlinks=entry.is_symlink()).st_mtime
    except OSError:
        return None


def _scan_session_mtimes(
    root: Path,
    *,
    include_header: bool = False,
    include_artifacts: bool = False,
    path_for_branch=None,
) -> dict[str, float]:
    """Scan one session mode directory in a single os.scandir pass."""
    modified: dict[str, float] = {}
    header_mtimes: dict[str, float] = {}
    has_branches = False
    has_artifacts = False

    try:
        with os.scandir(root) as entries:
            for entry in entries:
                name = entry.name
                match_name = name.lower() if os.name == "nt" else name
                if match_name == ".branches":
                    has_branches = entry.is_dir(follow_symlinks=False)
                    continue
                if match_name == ".artifacts":
                    has_artifacts = entry.is_dir(follow_symlinks=False)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    continue
                stem = ""
                is_header = False
                if match_name.endswith(".snapshot.json"):
                    stem = name[:-14]
                elif match_name.endswith(".header.json"):
                    if not include_header:
                        continue
                    stem = name[:-12]
                    is_header = True
                elif match_name.endswith(".jsonl"):
                    stem = name[:-6]
                elif match_name.endswith(".json"):
                    stem = name[:-5]
                else:
                    continue
                if not stem:
                    continue
                mtime = _entry_mtime(entry)
                if mtime is None:
                    continue
                if is_header:
                    if stem not in header_mtimes or mtime > header_mtimes[stem]:
                        header_mtimes[stem] = mtime
                elif stem not in modified or mtime > modified[stem]:
                    modified[stem] = mtime
    except OSError:
        return {}

    if not modified:
        return modified

    def _stem_key(value: str) -> str:
        # Match Windows NTFS 1-to-1 case insensitivity (normcase/lower) rather than
        # Unicode casefold(), so distinct NTFS names like "Straße" and "Strasse" do not collide.
        return os.path.normcase(value) if os.name == "nt" else value

    stems_by_key: dict[str, list[str]] = {}
    for stem in modified:
        stems_by_key.setdefault(_stem_key(stem), []).append(stem)

    if os.name == "nt":
        for group in stems_by_key.values():
            if len(group) > 1:
                shared_mtime = max(modified[stem] for stem in group)
                for stem in group:
                    modified[stem] = shared_mtime

    if include_header and header_mtimes:
        for stem, mtime in header_mtimes.items():
            for canonical in stems_by_key.get(_stem_key(stem), ()):
                if mtime > modified[canonical]:
                    modified[canonical] = mtime

    if include_artifacts and has_artifacts:
        artifacts_dir = root / ".artifacts"
        has_nested_artifacts = False
        try:
            with os.scandir(artifacts_dir) as artifacts:
                for entry in artifacts:
                    name = entry.name
                    match_name = name.lower() if os.name == "nt" else name
                    if entry.is_dir(follow_symlinks=False):
                        if match_name == ".artifacts":
                            has_nested_artifacts = True
                        continue
                    stem = ""
                    for suffix in _ARTIFACT_SUFFIXES:
                        if match_name.endswith(suffix):
                            stem = name[:-len(suffix)]
                            break
                    if not stem:
                        match = _SUBAGENT_CONV_RE.match(name)
                        if match:
                            stem = match.group(1)
                    if not stem:
                        continue
                    targets = stems_by_key.get(_stem_key(stem))
                    if targets:
                        mtime = _entry_mtime(entry)
                        if mtime is not None:
                            for canonical in targets:
                                if mtime > modified[canonical]:
                                    modified[canonical] = mtime
        except OSError:
            pass
        if has_nested_artifacts:
            try:
                with os.scandir(artifacts_dir / ".artifacts") as nested:
                    for entry in nested:
                        if entry.is_dir(follow_symlinks=False):
                            continue
                        match = _SUBAGENT_CONV_RE.match(entry.name)
                        if not match:
                            continue
                        targets = stems_by_key.get(_stem_key(match.group(1)))
                        if targets:
                            mtime = _entry_mtime(entry)
                            if mtime is not None:
                                for canonical in targets:
                                    if mtime > modified[canonical]:
                                        modified[canonical] = mtime
            except OSError:
                pass

    if has_branches and path_for_branch is not None:
        try:
            with os.scandir(root / ".branches") as branches:
                for entry in branches:
                    targets = stems_by_key.get(_stem_key(entry.name))
                    if not targets:
                        continue
                    for canonical in targets:
                        store = SessionStore(path_for_branch(canonical))
                        paths = (
                            store.related_paths()
                            if include_artifacts
                            else (store.jsonl_path, store.legacy_path, store.snapshot_path)
                        )
                        modified[canonical] = max(
                            (p.stat().st_mtime for p in paths if p.exists()),
                            default=0.0,
                        )
        except OSError:
            pass

    return modified


class SessionNameError(ValueError):
    pass


def validate_session_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise SessionNameError("Session name is required")
    if cleaned in {".", ".."} or not _SESSION_NAME_RE.fullmatch(cleaned):
        raise SessionNameError("Session name may only contain letters, numbers, underscore, dash, and dot")
    return cleaned


def validate_api_session_id(session_id: str) -> str:
    """Canonicalize an API id so case-insensitive filesystems cannot collide."""
    cleaned = unicodedata.normalize("NFC", validate_session_name(session_id))
    safe = unicodedata.normalize("NFC", cleaned.casefold())
    safe = validate_session_name(safe)
    if len(safe) > API_SESSION_ID_MAX_LENGTH:
        raise SessionNameError(
            f"session_id must be at most {API_SESSION_ID_MAX_LENGTH} characters"
        )
    if len(safe.encode("utf-8")) > API_SESSION_ID_MAX_UTF8_BYTES:
        raise SessionNameError(
            "session_id UTF-8 representation is too long "
            f"(max {API_SESSION_ID_MAX_UTF8_BYTES} bytes)"
        )
    return safe


def create_session_name(seed: str) -> str:
    text = re.sub(r'(["\'])([^"\']+\.(?:png|jpe?g|webp|gif))\1', " ", seed, flags=re.IGNORECASE)
    text = re.sub(r'(?<!\S)([A-Za-z]:[^\s"\']+\.(?:png|jpe?g|webp|gif)|[./~]?[^\s"\']+[\\/][^\s"\']+\.(?:png|jpe?g|webp|gif))', " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", text, flags=re.UNICODE).strip("_.-")
    if not text:
        text = f"session_{time.strftime('%Y%m%d_%H%M%S')}"
    return validate_session_name(text[:48].strip("_.-") or f"session_{time.strftime('%Y%m%d_%H%M%S')}")


def startup_session_path() -> Path:
    name = os.getenv("AGENT_SESSION", "").strip()
    if name:
        return session_path(name)
    # A backend process owns its default session. Include the PID so two CLI
    # windows started during the same second cannot accidentally share the
    # same append-only session files.
    return session_path(f"session_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}")


def session_path(name: str) -> Path:
    safe = validate_session_name(name)
    root = SESSION_DIR.resolve()
    path = (root / f"{safe}.json").resolve()
    if path.parent != root:
        raise SessionNameError("Session path escapes session directory")
    return path


def api_session_path(session_id: str) -> Path:
    """Return the durable path for one validated OpenAI API conversation."""
    safe = validate_api_session_id(session_id)
    return session_path(f"session_api_{safe}")


def create_bar_session_name() -> str:
    base = f"bar_{time.strftime('%Y%m%d_%H%M%S')}"
    name = base
    suffix = 2
    while bar_session_exists(name):
        name = f"{base}_{suffix}"
        suffix += 1
    return name


def bar_session_path(name: str) -> Path:
    safe = validate_session_name(name)
    root = (SESSION_DIR / "bar").resolve()
    path = (root / f"{safe}.json").resolve()
    if path.parent != root:
        raise SessionNameError("Bar session path escapes bar session directory")
    return path


def list_bar_sessions() -> list[str]:
    mtimes = _scan_session_mtimes(
        SESSION_DIR / "bar",
        include_header=True,
        include_artifacts=True,
        path_for_branch=bar_session_path,
    )
    return sorted(mtimes, key=mtimes.__getitem__, reverse=True)


def bar_session_msg_count(name: str) -> int:
    store = SessionStore(bar_session_path(name))
    if not store.exists:
        return 0
    try:
        return len(store.load().get("messages", []))
    except KeyError:
        return 0


def bar_session_exists(name: str) -> bool:
    return SessionStore(bar_session_path(name)).exists


def create_minimal_session_name() -> str:
    base = f"minimal_{time.strftime('%Y%m%d_%H%M%S')}"
    name = base
    suffix = 2
    while minimal_session_exists(name):
        name = f"{base}_{suffix}"
        suffix += 1
    return name


def minimal_session_path(name: str = "minimal") -> Path:
    """Return a validated path in the isolated Minimal session directory."""
    root = (SESSION_DIR / "minimal").resolve()
    safe = validate_session_name(name)
    path = (root / f"{safe}.json").resolve()
    if path.parent != root:
        raise SessionNameError("Minimal session path escapes minimal session directory")
    return path


def list_minimal_sessions() -> list[str]:
    mtimes = _scan_session_mtimes(
        SESSION_DIR / "minimal",
        include_header=True,
        include_artifacts=True,
        path_for_branch=minimal_session_path,
    )
    return sorted(mtimes, key=mtimes.__getitem__, reverse=True)


def minimal_session_msg_count(name: str) -> int:
    store = SessionStore(minimal_session_path(name))
    if not store.exists:
        return 0
    try:
        return len(store.load().get("messages", []))
    except KeyError:
        return 0


def minimal_session_exists(name: str) -> bool:
    return SessionStore(minimal_session_path(name)).exists


def list_sessions() -> list[str]:
    names = set(_scan_session_mtimes(SESSION_DIR, include_header=False, include_artifacts=False))
    if SESSION_DEFAULT.exists():
        names.add("default")
    return sorted(names, key=lambda x: (x != "default", x))


def session_msg_count(name: str) -> int:
    store = SessionStore(session_path(name))
    if not store.exists:
        return 0
    try:
        data = store.load()
        return len(data.get("messages", []))
    except KeyError:
        return 0


def session_exists(name: str) -> bool:
    return SessionStore(session_path(name)).exists


def export_session_markdown(name: str) -> Path:
    store = SessionStore(session_path(name))
    if not store.exists:
        raise FileNotFoundError(f"Session '{name}' not found")

    SESSION_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = SESSION_EXPORT_DIR / f"{validate_session_name(name)}.md"
    return store.export_markdown(name, out)


def rename_session(old_name: str, new_name: str) -> None:
    from agent.ui.session_ownership import claim_session
    leases = [claim_session(path) for path in sorted((session_path(old_name), session_path(new_name)))]
    try:
        _rename_session(old_name, new_name)
    finally:
        leases.clear()


def _rename_session(old_name: str, new_name: str) -> None:
    from agent.runtime.conversation_branches import ConversationBranches

    old_store = SessionStore(session_path(old_name))
    new_store = SessionStore(session_path(new_name))
    if not old_store.exists:
        raise FileNotFoundError(f"Session '{old_name}' not found")
    if new_store.exists:
        raise FileExistsError(f"Session '{new_name}' already exists")
    # The manifest and every branch head form one logical conversation. A
    # sequence of file renames cannot move that publication boundary atomically.
    # Refuse before moving media or any source; GUI display titles remain safe.
    if (ConversationBranches(old_store.logical_path).all_related_paths()
            or ConversationBranches(new_store.logical_path).all_related_paths()):
        raise SessionNameError("Conversation has reply versions and cannot be renamed on disk; change its display title instead")
    old_paths = old_store.related_paths()
    pairs = list(zip(old_store.related_paths(include_branches=False), new_store.related_paths(include_branches=False)))
    paired = {old for old, _ in pairs}
    # Preserve legacy subagent journals included by the ownership inventory.
    for old_path in old_paths:
        if old_path not in paired:
            suffix = old_path.name[len(old_store.logical_path.stem):]
            pairs.append((old_path, old_path.with_name(new_store.logical_path.stem + suffix)))
    for _, destination in pairs:
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"Session destination already exists: {destination.name}")
    old_store.appshot_media.rename_to(new_store.appshot_media)
    for old_path, new_path in pairs:
        if old_path.exists():
            new_path.parent.mkdir(parents=True, exist_ok=True)
            old_path.rename(new_path)


def delete_session(name: str) -> None:
    from agent.ui.session_ownership import claim_session
    lease = claim_session(session_path(name))
    try:
        _delete_session(name)
    finally:
        del lease


def _delete_session(name: str) -> None:
    # Reuse the branch inventory, media checks, projection disposal and scoped
    # working-state transaction. The session lease is reentrant in this process.
    from agent.ui.session_actions import act

    act({"action": "delete", "name": name, "mode": "work"})
