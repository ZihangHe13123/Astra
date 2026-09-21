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
API_SESSION_ID_MAX_LENGTH = 64
API_SESSION_ID_MAX_UTF8_BYTES = 192


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
    root = SESSION_DIR / "bar"
    if not root.exists():
        return []
    sessions = set()
    for path in root.glob("*.json"):
        if not path.name.endswith((".snapshot.json", ".header.json")):
            sessions.add(path.stem)
    for path in root.glob("*.jsonl"):
        sessions.add(path.stem)
    for path in root.glob("*.snapshot.json"):
        sessions.add(path.name.removesuffix(".snapshot.json"))
    def modified_at(name: str) -> float:
        paths = SessionStore(bar_session_path(name)).related_paths()
        return max((path.stat().st_mtime for path in paths if path.exists()), default=0.0)

    return sorted(sessions, key=modified_at, reverse=True)


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
    root = SESSION_DIR / "minimal"
    if not root.exists():
        return []
    sessions = set()
    for path in root.glob("*.json"):
        if not path.name.endswith((".snapshot.json", ".header.json")):
            sessions.add(path.stem)
    for path in root.glob("*.jsonl"):
        sessions.add(path.stem)
    for path in root.glob("*.snapshot.json"):
        sessions.add(path.name.removesuffix(".snapshot.json"))

    def modified_at(name: str) -> float:
        paths = SessionStore(minimal_session_path(name)).related_paths()
        return max((path.stat().st_mtime for path in paths if path.exists()), default=0.0)

    return sorted(sessions, key=modified_at, reverse=True)


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
    sessions = set()
    if SESSION_DEFAULT.exists():
        sessions.add("default")
    if SESSION_DIR.exists():
        for f in SESSION_DIR.glob("*.json"):
            if not f.name.endswith((".snapshot.json", ".header.json")):
                sessions.add(f.stem)
        for f in SESSION_DIR.glob("*.jsonl"):
            sessions.add(f.stem)
        for f in SESSION_DIR.glob("*.snapshot.json"):
            sessions.add(f.name.removesuffix(".snapshot.json"))
    return sorted(sessions, key=lambda x: (x != "default", x))


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
    old_store = SessionStore(session_path(old_name))
    new_store = SessionStore(session_path(new_name))
    if not old_store.exists:
        raise FileNotFoundError(f"Session '{old_name}' not found")
    if new_store.exists:
        raise FileExistsError(f"Session '{new_name}' already exists")
    old_store.appshot_media.rename_to(new_store.appshot_media)
    for old_path, new_path in zip(old_store.related_paths(), new_store.related_paths()):
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
    store = SessionStore(session_path(name))
    if not store.exists:
        raise FileNotFoundError(f"Session '{name}' not found")
    store.appshot_media.delete()
    for path in store.related_paths():
        if path.exists():
            path.unlink()
