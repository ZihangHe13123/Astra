"""Browser session state machine and durable persistence.

Phase 3 ROADMAP item:
  新增 BrowserSessionManager，持久化 session_id / profile_ref / tabs /
  current_url / mode / last_snapshot；执行阶梯 static HTTP → semantic
  DOM/headless → screenshot/vision → headed human takeover；验证码、2FA、
  支付和难以自动判断的弹窗进入 takeover，用户完成后在原 session 继续。

Security invariants (from ROADMAP):
  - Only a profile *reference* is stored, never cookies/passwords/tokens.
  - Snapshots are sanitized before persistence — auth material is stripped.
  - The legacy browser-extractor path stays a read-only adapter.

The concrete CDP transport is a pluggable BrowserBackend; this module owns
the durable state machine that survives restarts and model/session switches.
"""

from __future__ import annotations

from agent.runtime.paths import state_path

import re
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Protocol

if TYPE_CHECKING:
    from .browser_lifecycle import BrowserLifecycle


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Execution ladder
# ---------------------------------------------------------------------------

class BrowserMode(str, Enum):
    """Escalation ladder for page access.

    Order matters: each step is strictly more capable (and more expensive /
    more human-invasive) than the previous. The manager only escalates
    forward and restores the pre-takeover mode on resume.
    """

    READ_ONLY = "read_only"        # static HTTP extraction (legacy adapter)
    HEADLESS = "headless"          # semantic DOM via headless browser
    SCREENSHOT = "screenshot"      # screenshot + vision
    HEADED_TAKEOVER = "headed_takeover"  # human drives the real browser


# Strict escalation order. Index == capability rank.
_LADDER: list[BrowserMode] = [
    BrowserMode.READ_ONLY,
    BrowserMode.HEADLESS,
    BrowserMode.SCREENSHOT,
    BrowserMode.HEADED_TAKEOVER,
]
_LADDER_RANK = {mode: idx for idx, mode in enumerate(_LADDER)}

# Reasons a page escalates all the way to human takeover.
TAKEOVER_REASONS = ("captcha", "2fa", "payment", "dialog", "login", "unknown")


def next_mode(mode: BrowserMode) -> BrowserMode | None:
    """Return the next rung up the ladder, or None at the top."""
    rank = _LADDER_RANK[mode]
    if rank + 1 >= len(_LADDER):
        return None
    return _LADDER[rank + 1]


# ---------------------------------------------------------------------------
# Snapshot sanitization (security invariant)
# ---------------------------------------------------------------------------

# Patterns that must NEVER reach the durable store or the model context.
_SENSITIVE_PATTERNS = [
    # Cookie headers / document.cookie values
    re.compile(r"(?i)(set-cookie|cookie)\s*[:=]\s*[^\n;]+"),
    # Authorization headers
    re.compile(r"(?i)authorization\s*[:=]\s*[^\n]+"),
    # Bearer / Basic tokens
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._\-+/=]{8,}"),
    # Generic API key / token assignments
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|passwd|pwd)\s*[:=]\s*[^\s\"']+"),
    # Session ids in query strings
    re.compile(r"(?i)([?&](sessionid|sid|jsessionid|phpsessid|token)=[^\s&\"']+)"),
    # Password input values in DOM dumps
    re.compile(r"(?i)(<input[^>]*type=[\"']?password[\"']?[^>]*>)"),
]


def sanitize_snapshot(text: str) -> str:
    """Strip auth material from a page snapshot before persistence.

    This is a defense-in-depth filter: backends should never emit secrets,
    but the durable store must not retain them even if they do.
    """
    if not text:
        return ""
    def clean_string(value: str) -> str:
        for pattern in _SENSITIVE_PATTERNS:
            value = pattern.sub("[REDACTED]", value)
        return value

    # Structured page observations must remain parseable. Header regexes applied
    # to serialized JSON could consume every field following a text value.
    try:
        structured = json.loads(text)
    except (ValueError, RecursionError):
        structured = None
    if isinstance(structured, (dict, list)):
        def clean(value):
            if isinstance(value, str):
                return clean_string(value)
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value
        # Preserve compact form observations through persistence and tool-output
        # sanitization, including the form snapshot inside action receipts.
        after = structured.get("after") if isinstance(structured, dict) else None
        compact = isinstance(structured, dict) and (
            structured.get("scope") == "form" or isinstance(after, dict) and after.get("scope") == "form"
        )
        return json.dumps(clean(structured), ensure_ascii=False, separators=(",", ":") if compact else None)
    return clean_string(text)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class TabState:
    tab_id: str
    url: str
    title: str = ""
    mode: BrowserMode = BrowserMode.READ_ONLY
    pre_takeover_mode: BrowserMode | None = None
    takeover_reason: str = ""
    last_snapshot: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tab_id": self.tab_id,
            "url": self.url,
            "title": self.title,
            "mode": self.mode.value,
            "pre_takeover_mode": self.pre_takeover_mode.value if self.pre_takeover_mode else None,
            "takeover_reason": self.takeover_reason,
            "last_snapshot": self.last_snapshot,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class BrowserSession:
    session_id: str
    profile_ref: str = ""
    current_tab_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    tabs: dict[str, TabState] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "profile_ref": self.profile_ref,
            "current_tab_id": self.current_tab_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tabs": {tid: tab.to_dict() for tid, tab in self.tabs.items()},
        }


# ---------------------------------------------------------------------------
# Backend protocol (pluggable transport)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BackendCapabilities:
    read: bool
    interactive: bool
    takeover: bool


class BrowserBackend(Protocol):
    """Transport contract. The session manager owns state; the backend
    performs the actual page access. Backends must never return secrets."""

    @property
    def name(self) -> str: ...

    @property
    def capabilities(self) -> BackendCapabilities: ...

    async def extract(self, url: str, *, max_length: int = 12000) -> str:
        """Read-only text extraction (static or headless)."""
        ...

    async def status(self) -> tuple[bool, str]:
        """Return (available, detail)."""
        ...

    async def interactive_click(self, selector: str, *, tab_id: str = "", url: str = "") -> str: ...

    async def interactive_type(
        self, selector: str, text: str, *, tab_id: str = "", url: str = ""
    ) -> str: ...

    async def interactive_select(
        self, selector: str, value: str, *, tab_id: str = "", url: str = ""
    ) -> str: ...

    async def interactive_wait(
        self,
        *,
        tab_id: str = "",
        url: str = "",
        selector: str = "",
        text: str = "",
        url_contains: str = "",
        timeout_ms: int = 10000,
    ) -> str: ...

    async def interactive_screenshot(
        self, *, tab_id: str = "", url: str = "", output_path: str = ""
    ) -> str: ...

    async def close_connection(self, tab_id: str = "") -> None: ...

    async def interactive_state(
        self, *, tab_id: str = "", url: str = ""
    ) -> tuple[str, str, str]: ...

    async def interactive_handoff(
        self, *, tab_id: str, url: str, profile_dir: str = ""
    ) -> str: ...

    async def interactive_resume(
        self, *, tab_id: str, url: str, profile_dir: str = ""
    ) -> str: ...


class LegacyBrowserExtractBackend:
    """Read-only adapter wrapping the configured browser-extractor path.

    This preserves the current behaviour as the bottom rung of the ladder
    (BrowserMode.READ_ONLY). It cannot do interactive or takeover work.
    """

    # Preserve the reported backend identifier for existing integrations.
    name = "legacy-wsl-extract"
    capabilities = BackendCapabilities(read=True, interactive=False, takeover=False)

    def __init__(self, extract_fn, status_fn):
        # extract_fn: async (url, max_length) -> str
        # status_fn: async () -> (bool, str)
        self._extract = extract_fn
        self._status = status_fn

    async def extract(self, url: str, *, max_length: int = 12000) -> str:
        return await self._extract(url, max_length)

    async def status(self) -> tuple[bool, str]:
        return await self._status()

    async def interactive_click(self, selector: str, *, tab_id: str = "", url: str = "") -> str:
        raise RuntimeError("interactive browser backend is unavailable")

    async def interactive_type(
        self, selector: str, text: str, *, tab_id: str = "", url: str = ""
    ) -> str:
        raise RuntimeError("interactive browser backend is unavailable")

    async def interactive_select(
        self, selector: str, value: str, *, tab_id: str = "", url: str = ""
    ) -> str:
        raise RuntimeError("interactive browser backend is unavailable")

    async def interactive_wait(
        self,
        *,
        tab_id: str = "",
        url: str = "",
        selector: str = "",
        text: str = "",
        url_contains: str = "",
        timeout_ms: int = 10000,
    ) -> str:
        raise RuntimeError("interactive browser backend is unavailable")

    async def interactive_screenshot(
        self, *, tab_id: str = "", url: str = "", output_path: str = ""
    ) -> str:
        raise RuntimeError("interactive browser backend is unavailable")

    async def close_connection(self, tab_id: str = "") -> None:
        return None

    async def interactive_state(
        self, *, tab_id: str = "", url: str = ""
    ) -> tuple[str, str, str]:
        raise RuntimeError("interactive browser backend is unavailable")

    async def interactive_handoff(
        self, *, tab_id: str, url: str, profile_dir: str = ""
    ) -> str:
        raise RuntimeError("interactive browser backend is unavailable")

    async def interactive_resume(
        self, *, tab_id: str, url: str, profile_dir: str = ""
    ) -> str:
        raise RuntimeError("interactive browser backend is unavailable")


# Compatibility alias for integrations that still import the WSL-specific name.
LegacyWslExtractBackend = LegacyBrowserExtractBackend


# ---------------------------------------------------------------------------
# Session manager (durable state machine)
# ---------------------------------------------------------------------------

def browser_db_path() -> Path:
    import os
    override = os.getenv("AGENT_BROWSER_DB", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return state_path("browser.db")


class BrowserSessionManager:
    """Durable browser session state.

    Persists sessions and tabs in SQLite (WAL) so a page-access workflow
    retains history across restarts and session switches. Live tool handles
    are scoped separately to each conversation activation. The manager
    enforces the execution ladder and the takeover/resume lifecycle, and
    sanitizes every snapshot before it touches disk.
    """

    def __init__(self, path: str | Path | None = None, backend: BrowserBackend | None = None):
        self.path = Path(path) if path is not None else browser_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.backend = backend
        # Set by register_browser_tools; the runtime hands control over through it.
        self.lifecycle: BrowserLifecycle | None = None
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS browser_sessions (
                    session_id TEXT PRIMARY KEY,
                    profile_ref TEXT NOT NULL DEFAULT '',
                    current_tab_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_tabs (
                    tab_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES browser_sessions(session_id) ON DELETE CASCADE,
                    url TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'read_only',
                    pre_takeover_mode TEXT,
                    takeover_reason TEXT NOT NULL DEFAULT '',
                    last_snapshot TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_browser_tabs_session
                    ON browser_tabs(session_id, updated_at DESC);
                """
            )

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session(self, *, profile_ref: str = "") -> BrowserSession:
        """Create a new browser session bound to a named profile reference.

        profile_ref is an opaque name (e.g. 'default', 'work'); it is NEVER
        a credential. Only the reference is stored.
        """
        now = _now()
        session_id = uuid.uuid4().hex[:12]
        clean_ref = " ".join(str(profile_ref).split())[:120]
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO browser_sessions (session_id, profile_ref, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, clean_ref, now, now),
            )
        session = self.get_session(session_id)
        assert session is not None
        return session

    def get_session(self, session_id: str) -> BrowserSession | None:
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT * FROM browser_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            tab_rows = db.execute(
                "SELECT * FROM browser_tabs WHERE session_id=? ORDER BY updated_at DESC",
                (session_id,),
            ).fetchall()
        session = BrowserSession(
            session_id=str(row["session_id"]),
            profile_ref=str(row["profile_ref"]),
            current_tab_id=str(row["current_tab_id"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
        for tab_row in tab_rows:
            tab = self._tab_from_row(tab_row)
            session.tabs[tab.tab_id] = tab
        return session

    def list_sessions(self, *, limit: int = 20) -> list[BrowserSession]:
        with self._lock, self._connection() as db:
            rows = db.execute(
                "SELECT session_id FROM browser_sessions ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        return [s for row in rows if (s := self.get_session(str(row["session_id"]))) is not None]

    # ------------------------------------------------------------------
    # Tab lifecycle
    # ------------------------------------------------------------------

    def open_tab(self, session_id: str, url: str, *, title: str = "") -> TabState:
        """Open a new tab at READ_ONLY (bottom of the ladder)."""
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(f"Unknown browser session: {session_id}")
        now = _now()
        tab_id = uuid.uuid4().hex[:12]
        clean_url = " ".join(str(url).split())[:2000]
        with self._lock, self._connection() as db:
            db.execute(
                "INSERT INTO browser_tabs "
                "(tab_id, session_id, url, title, mode, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'read_only', ?, ?)",
                (tab_id, session_id, clean_url, str(title)[:500], now, now),
            )
            db.execute(
                "UPDATE browser_sessions SET current_tab_id=?, updated_at=? WHERE session_id=?",
                (tab_id, now, session_id),
            )
        tab = self.get_tab(tab_id)
        assert tab is not None
        return tab

    def get_tab(self, tab_id: str) -> TabState | None:
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT * FROM browser_tabs WHERE tab_id=?", (tab_id,)
            ).fetchone()
        return self._tab_from_row(row) if row else None

    def set_current_tab(self, session_id: str, tab_id: str) -> None:
        session = self.get_session(session_id)
        if session is None:
            raise KeyError(f"Unknown browser session: {session_id}")
        if tab_id not in session.tabs:
            raise KeyError(f"Tab {tab_id} not in session {session_id}")
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE browser_sessions SET current_tab_id=?, updated_at=? WHERE session_id=?",
                (tab_id, _now(), session_id),
            )

    def close_tab(self, tab_id: str) -> None:
        """Remove a durable tab and select the most recent remaining tab."""
        tab = self.get_tab(tab_id)
        if tab is None:
            raise KeyError(f"Unknown tab: {tab_id}")
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT session_id FROM browser_tabs WHERE tab_id=?", (tab_id,)
            ).fetchone()
            assert row is not None
            session_id = str(row["session_id"])
            db.execute("DELETE FROM browser_tabs WHERE tab_id=?", (tab_id,))
            replacement = db.execute(
                "SELECT tab_id FROM browser_tabs WHERE session_id=? ORDER BY updated_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            db.execute(
                "UPDATE browser_sessions SET current_tab_id=?, updated_at=? WHERE session_id=?",
                (str(replacement["tab_id"]) if replacement else "", _now(), session_id),
            )

    def record_snapshot(self, tab_id: str, snapshot: str) -> TabState:
        """Persist a sanitized snapshot for a tab.

        The snapshot is run through sanitize_snapshot() before storage so
        auth material never reaches disk.
        """
        tab = self.get_tab(tab_id)
        if tab is None:
            raise KeyError(f"Unknown tab: {tab_id}")
        clean = sanitize_snapshot(snapshot)
        now = _now()
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE browser_tabs SET last_snapshot=?, updated_at=? WHERE tab_id=?",
                (clean, now, tab_id),
            )
        updated = self.get_tab(tab_id)
        assert updated is not None
        return updated

    def update_page_state(
        self,
        tab_id: str,
        *,
        url: str,
        title: str = "",
        snapshot: str | None = None,
    ) -> TabState:
        """Synchronize durable tab metadata after a real browser action."""
        if self.get_tab(tab_id) is None:
            raise KeyError(f"Unknown tab: {tab_id}")
        clean_url = sanitize_snapshot(" ".join(str(url).split())[:2000])
        clean_title = sanitize_snapshot(str(title))[:500]
        now = _now()
        with self._lock, self._connection() as db:
            if snapshot is None:
                db.execute(
                    "UPDATE browser_tabs SET url=?, title=?, updated_at=? WHERE tab_id=?",
                    (clean_url, clean_title, now, tab_id),
                )
            else:
                db.execute(
                    "UPDATE browser_tabs SET url=?, title=?, last_snapshot=?, updated_at=? WHERE tab_id=?",
                    (clean_url, clean_title, sanitize_snapshot(snapshot), now, tab_id),
                )
        updated = self.get_tab(tab_id)
        assert updated is not None
        return updated

    # ------------------------------------------------------------------
    # Execution ladder
    # ------------------------------------------------------------------

    def escalate(self, tab_id: str) -> TabState:
        """Move a tab one rung up the execution ladder.

        Raises ValueError if the tab is already at the top rung. A tab in
        takeover cannot be escalated further (it is already at the top).
        """
        tab = self.get_tab(tab_id)
        if tab is None:
            raise KeyError(f"Unknown tab: {tab_id}")
        nxt = next_mode(tab.mode)
        if nxt is None:
            raise ValueError(f"Tab {tab_id} is already at the top of the ladder ({tab.mode.value})")
        return self._set_mode(tab_id, nxt)

    def escalate_to(self, tab_id: str, target: BrowserMode) -> TabState:
        """Escalate a tab directly to a specific mode (must be higher)."""
        tab = self.get_tab(tab_id)
        if tab is None:
            raise KeyError(f"Unknown tab: {tab_id}")
        if _LADDER_RANK[target] <= _LADDER_RANK[tab.mode]:
            raise ValueError(
                f"Target {target.value} is not above current {tab.mode.value}"
            )
        return self._set_mode(tab_id, target)

    def can_escalate(self, tab_id: str) -> bool:
        tab = self.get_tab(tab_id)
        if tab is None:
            return False
        return next_mode(tab.mode) is not None

    def _set_mode(self, tab_id: str, mode: BrowserMode) -> TabState:
        now = _now()
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE browser_tabs SET mode=?, updated_at=? WHERE tab_id=?",
                (mode.value, now, tab_id),
            )
        updated = self.get_tab(tab_id)
        assert updated is not None
        return updated

    # ------------------------------------------------------------------
    # Takeover / resume lifecycle
    # ------------------------------------------------------------------

    def request_takeover(self, tab_id: str, reason: str = "unknown") -> TabState:
        """Escalate a tab to headed human takeover.

        Records the pre-takeover mode so resume_from_takeover() can restore
        it. reason must be one of TAKEOVER_REASONS.
        """
        tab = self.get_tab(tab_id)
        if tab is None:
            raise KeyError(f"Unknown tab: {tab_id}")
        clean_reason = str(reason).strip().lower()
        if clean_reason not in TAKEOVER_REASONS:
            clean_reason = "unknown"
        if tab.mode == BrowserMode.HEADED_TAKEOVER:
            # Already in takeover; just refresh the reason.
            now = _now()
            with self._lock, self._connection() as db:
                db.execute(
                    "UPDATE browser_tabs SET takeover_reason=?, updated_at=? WHERE tab_id=?",
                    (clean_reason, now, tab_id),
                )
            updated = self.get_tab(tab_id)
            assert updated is not None
            return updated

        now = _now()
        pre_mode = tab.pre_takeover_mode or tab.mode
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE browser_tabs SET mode=?, pre_takeover_mode=?, takeover_reason=?, updated_at=? "
                "WHERE tab_id=?",
                (BrowserMode.HEADED_TAKEOVER.value, pre_mode.value, clean_reason, now, tab_id),
            )
        updated = self.get_tab(tab_id)
        assert updated is not None
        return updated

    def resume_from_takeover(self, tab_id: str) -> TabState:
        """Return a tab from human takeover to its pre-takeover mode.

        If no pre-takeover mode was recorded, defaults to HEADLESS (a safe
        autonomous rung). Clears the takeover reason.
        """
        tab = self.get_tab(tab_id)
        if tab is None:
            raise KeyError(f"Unknown tab: {tab_id}")
        if tab.mode != BrowserMode.HEADED_TAKEOVER:
            raise ValueError(f"Tab {tab_id} is not in takeover (mode={tab.mode.value})")
        restore = tab.pre_takeover_mode or BrowserMode.HEADLESS
        now = _now()
        with self._lock, self._connection() as db:
            db.execute(
                "UPDATE browser_tabs SET mode=?, pre_takeover_mode=NULL, takeover_reason='', updated_at=? "
                "WHERE tab_id=?",
                (restore.value, now, tab_id),
            )
        updated = self.get_tab(tab_id)
        assert updated is not None
        return updated

    def is_in_takeover(self, tab_id: str) -> bool:
        tab = self.get_tab(tab_id)
        return tab is not None and tab.mode == BrowserMode.HEADED_TAKEOVER

    # ------------------------------------------------------------------
    # Backend dispatch (read path)
    # ------------------------------------------------------------------

    async def extract(self, url: str, *, max_length: int = 12000) -> str:
        """Read-only extraction via the configured backend (legacy adapter).

        Returns a sanitized result. If no backend is configured, returns a
        clear error rather than failing silently.
        """
        if self.backend is None:
            return "[Browser Error] No browser backend configured."
        result = await self.backend.extract(url, max_length=max_length)
        return sanitize_snapshot(result)

    async def backend_status(self) -> tuple[bool, str]:
        if self.backend is None:
            return False, "no backend configured"
        return await self.backend.status()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _tab_from_row(row: sqlite3.Row) -> TabState:
        pre_mode_raw = row["pre_takeover_mode"]
        pre_mode = BrowserMode(pre_mode_raw) if pre_mode_raw else None
        return TabState(
            tab_id=str(row["tab_id"]),
            url=str(row["url"]),
            title=str(row["title"]),
            mode=BrowserMode(row["mode"]),
            pre_takeover_mode=pre_mode,
            takeover_reason=str(row["takeover_reason"]),
            last_snapshot=str(row["last_snapshot"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


# ---------------------------------------------------------------------------
# Formatting (for /browser command output)
# ---------------------------------------------------------------------------

def format_session(session: BrowserSession) -> str:
    lines = [
        f"Browser session {session.session_id}",
        f"Profile ref: {session.profile_ref or '(default)'}",
        f"Current tab: {session.current_tab_id or '(none)'}",
        f"Tabs ({len(session.tabs)}):",
    ]
    for tab in session.tabs.values():
        marker = "*" if tab.tab_id == session.current_tab_id else " "
        suffix = f" [{tab.takeover_reason}]" if tab.takeover_reason else ""
        lines.append(f"  {marker} {tab.tab_id}  {tab.mode.value:<16}{suffix}  {tab.url}")
    if not session.tabs:
        lines.append("  (none)")
    return "\n".join(lines)
