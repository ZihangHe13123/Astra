"""Private, canonical SQLite archive for local computer activity."""

from __future__ import annotations

from agent.runtime.paths import state_path

import os
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


_MAX_RESULTS = 20
_MAX_ITEM_CHARS = 500
_MAX_TOTAL_CHARS = 12_000
_REDACTED_STRUCTURED_TEXT = "<redacted structured text>"


@dataclass(frozen=True)
class ActivityEvent:
    segment_id: str
    event_id: int
    occurred_at: str
    kind: str
    app_name: str
    bundle_id: str
    window_title: str
    url: str
    selection_text: str
    searchable_text: str
    raw_json: str
    imported_at: str


@dataclass(frozen=True)
class ActivitySummary:
    summary_id: str
    source_path: str
    granularity: str
    period_start: str
    period_end: str
    content: str
    content_hash: str
    source_mtime_ns: int
    imported_at: str


@dataclass(frozen=True)
class SourceCursor:
    source_path: str
    source_kind: str
    source_identity: str
    byte_offset: int
    observed_size: int
    observed_mtime_ns: int
    last_success_at: str
    last_error: str


def default_activity_db_path() -> Path:
    configured = os.getenv("ASTRA_ACTIVITY_DB", "").strip()
    return Path(configured).expanduser() if configured else state_path("activity-history.sqlite3")


def sanitize_url(value: str) -> str:
    """Return only the safe, model-visible components of an HTTP(S) URL."""
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
    except ValueError:
        return ""


def _url_search_text(url: str) -> str:
    """Keep URL host/path useful for search while dropping sensitive components."""
    return sanitize_url(url)


def _utc_timestamp(value: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty ISO-8601 value")
    fraction = re.search(r"\.(\d+)(?:Z|[+-]\d{2}:\d{2})$", value.strip())
    if fraction is not None and len(fraction.group(1)) > 6:
        raise ValueError("timestamp precision must not exceed microseconds")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    try:
        normalized = parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("timestamp cannot be normalized to UTC") from exc
    return normalized, normalized.isoformat()


def normalize_timestamp(value: str) -> tuple[datetime, str]:
    """Apply the canonical timestamp validation and UTC normalization used by activity search."""
    return _utc_timestamp(value)


def _timestamp_microseconds(value: str) -> int | None:
    try:
        timestamp, _normalized = _utc_timestamp(value)
    except ValueError:
        return None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = timestamp - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def is_valid_timestamp(value: str) -> bool:
    try:
        _utc_timestamp(value)
    except ValueError:
        return False
    return True


def _url_host(value: str) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").casefold()
    except ValueError:
        return ""


def _domain_host(value: str) -> str:
    try:
        parsed = urlsplit(str(value or ""))
        if parsed.scheme and not parsed.netloc:
            return ""
        if not parsed.netloc:
            parsed = urlsplit(f"//{value}")
        host = (parsed.hostname or "").casefold()
        if not host or any(char.isspace() for char in host):
            return ""
        _ = parsed.port  # Force urlsplit to validate a supplied port.
        if ":" in host:
            import ipaddress
            ipaddress.IPv6Address(host)
        elif any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
            return ""
        return host
    except ValueError:
        return ""


def _clamp(value: int, lower: int, upper: int) -> int:
    try:
        return max(lower, min(int(value), upper))
    except (TypeError, ValueError):
        return lower


def _search_terms(query: str) -> list[str]:
    return [term for term in str(query or "").split() if term]


def _requires_like_fallback(terms: list[str]) -> bool:
    """Trigram cannot index short CJK terms, so use a safe canonical fallback."""
    return any(any("\u4e00" <= char <= "\u9fff" for char in term) and len(term) < 3 for term in terms)


def _fts_query(terms: list[str]) -> str:
    return " AND ".join(f'"{term.replace(chr(34), "")}"' for term in terms)


def _redact_text(value: str) -> str:
    """Keep snippets useful without exposing URL credentials or common secret values."""
    text = str(value or "")
    text = _redact_json_text(text)
    text = re.sub(r"https?://[^\s)>}\"']+", lambda match: sanitize_url(match.group(0)), text, flags=re.IGNORECASE)
    secret_keys = r"(?:api[_-]?key|(?:access|refresh)[_-]?token|client[_-]?secret|token|password|secret|cookie|authorization)"
    text = re.sub(
        rf'(?i)("{secret_keys}"\s*:\s*")(?:\\.|[^"])*(?=")',
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        rf"(?i)('{secret_keys}'\s*:\s*')(?:\\.|[^'])*(?=')",
        r"\1<redacted>",
        text,
    )
    text = re.sub(
        rf'(?i)(\b{secret_keys}\b\s*[:=]\s*")(?:\\.|[^"])*"',
        r'\1<redacted>"',
        text,
    )
    text = re.sub(
        rf"(?i)(\b{secret_keys}\b\s*[:=]\s*')(?:\\.|[^'])*'",
        r"\1<redacted>'",
        text,
    )
    text = re.sub(r"(?im)(\bauthorization\b\s*[:=]\s*)[\"']?(?:bearer|basic|digest|token)?[^\r\n]*", r"\1<redacted>", text)
    text = re.sub(r"(?i)\bbearer\s+[^\s,;]+", "Bearer <redacted>", text)
    text = re.sub(
        r"(?i)\bauthorization\s*[:=]\s*(?:(?:bearer|basic|digest|token)\s+)?[^\s,;]+",
        "Authorization: <redacted>",
        text,
    )
    text = re.sub(
        r'(?i)(\b(?:api[_-]?key|(?:access|refresh)[_-]?token|client[_-]?secret|token|password|secret|cookie)\b\s*[=:]\s*["\']?)[^\s,;"\']+',
        r"\1<redacted>",
        text,
    )
    return text


def _is_secret_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    return normalized in {
        "apikey", "accesstoken", "refreshtoken", "clientsecret", "token",
        "password", "secret", "cookie", "authorization",
    }


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _is_secret_key(key) else _redact_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    return value


def _redact_json_text(value: str) -> str:
    """Redact semantic keys after JSON escape decoding, including non-string values."""
    decoder = json.JSONDecoder()
    pieces: list[str] = []
    cursor = 0
    for candidate in re.finditer(r"[\[{]", value):
        if candidate.start() < cursor:
            continue
        try:
            parsed, end = decoder.raw_decode(value, candidate.start())
        except json.JSONDecodeError:
            continue
        except (RecursionError, ValueError, OverflowError):
            return _REDACTED_STRUCTURED_TEXT
        if not isinstance(parsed, (dict, list)):
            continue
        try:
            redacted = json.dumps(_redact_json_value(parsed), ensure_ascii=False, separators=(",", ":"))
        except (RecursionError, ValueError, OverflowError):
            return _REDACTED_STRUCTURED_TEXT
        pieces.extend((value[cursor:candidate.start()], redacted))
        cursor = end
    if cursor == 0:
        return value
    pieces.append(value[cursor:])
    return "".join(pieces)


def _sanitize_model_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_sanitize_model_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _sanitize_model_value(item) for key, item in value.items() if key != "raw_json"}
    return value


def _string_paths(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(value, str):
        return [path]
    if isinstance(value, list):
        return [child for index, item in enumerate(value) for child in _string_paths(item, path + (index,))]
    if isinstance(value, dict):
        return [child for key, item in value.items() for child in _string_paths(item, path + (key,))]
    return []


def _at_path(value: Any, path: tuple[Any, ...]) -> Any:
    for part in path:
        value = value[part]
    return value


def _replace_path(value: Any, path: tuple[Any, ...], replacement: str) -> None:
    for part in path[:-1]:
        value = value[part]
    value[path[-1]] = replacement


def _bounded_payload(value: dict[str, Any], maximum: int = _MAX_ITEM_CHARS) -> dict[str, Any]:
    """Bound serialized model-visible evidence without changing its schema."""
    payload = _sanitize_model_value(value)
    while len(json.dumps(payload, ensure_ascii=False)) > maximum:
        paths = _string_paths(payload)
        if not paths:
            break
        longest = max(paths, key=lambda path: len(_at_path(payload, path)))
        current = _at_path(payload, longest)
        if not current:
            break
        _replace_path(payload, longest, current[:max(0, len(current) // 2 - 1)] + ("…" if len(current) > 1 else ""))
    return payload


def _bounded_item(item: dict[str, Any]) -> dict[str, Any]:
    """Bound one result, copying its expand locators whole: a shortened id cannot be expanded."""
    bounded = _bounded_payload({key: value for key, value in item.items() if key != "summary_ids"})
    if "summary_ids" in item:
        bounded["summary_ids"] = list(item["summary_ids"])
    return bounded


def _time_window(start: str, end: str) -> tuple[str, str]:
    start_utc = _utc_timestamp(start)[1] if start else ""
    end_utc = _utc_timestamp(end)[1] if end else ""
    if start_utc and end_utc and _utc_timestamp(start_utc)[0] > _utc_timestamp(end_utc)[0]:
        raise ValueError("start must not be after end")
    return start_utc, end_utc


class ActivityStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path is not None else default_activity_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.connection = sqlite3.connect(str(self.path), timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.create_function("activity_timestamp_us", 1, _timestamp_microseconds, deterministic=True)
        self.connection.create_function("activity_url_host", 1, _url_host, deterministic=True)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.path.chmod(0o600)
        self._create_schema()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS activity_events (
                segment_id TEXT NOT NULL,
                event_id INTEGER NOT NULL,
                occurred_at TEXT NOT NULL,
                occurred_at_us INTEGER,
                kind TEXT NOT NULL,
                app_name TEXT NOT NULL DEFAULT '',
                bundle_id TEXT NOT NULL DEFAULT '',
                window_title TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                url_search_text TEXT NOT NULL,
                selection_text TEXT NOT NULL DEFAULT '',
                searchable_text TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                PRIMARY KEY(segment_id, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_activity_events_occurred_at
                ON activity_events(occurred_at);
            CREATE TABLE IF NOT EXISTS activity_summaries (
                summary_id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                granularity TEXT NOT NULL,
                period_start TEXT NOT NULL DEFAULT '',
                period_end TEXT NOT NULL DEFAULT '',
                period_end_us INTEGER,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source_mtime_ns INTEGER NOT NULL,
                imported_at TEXT NOT NULL
                ,UNIQUE(source_path)
            );
            CREATE INDEX IF NOT EXISTS idx_activity_summaries_period
                ON activity_summaries(period_start, period_end);
            CREATE TABLE IF NOT EXISTS sync_files (
                source_path TEXT PRIMARY KEY,
                source_kind TEXT NOT NULL,
                source_identity TEXT NOT NULL DEFAULT '',
                byte_offset INTEGER NOT NULL DEFAULT 0,
                observed_size INTEGER NOT NULL DEFAULT 0,
                observed_mtime_ns INTEGER NOT NULL DEFAULT 0,
                last_success_at TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS activity_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS activity_events_fts USING fts5(
                app_name, window_title, url_search_text, selection_text, searchable_text,
                content='activity_events', content_rowid='rowid', tokenize='trigram'
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS activity_summaries_fts USING fts5(
                content, content='activity_summaries', content_rowid='rowid', tokenize='trigram'
            );
            CREATE TRIGGER IF NOT EXISTS activity_events_ai AFTER INSERT ON activity_events BEGIN
                INSERT INTO activity_events_fts(rowid, app_name, window_title, url_search_text, selection_text, searchable_text)
                VALUES (new.rowid, new.app_name, new.window_title, new.url_search_text, new.selection_text, new.searchable_text);
            END;
            CREATE TRIGGER IF NOT EXISTS activity_events_ad AFTER DELETE ON activity_events BEGIN
                INSERT INTO activity_events_fts(activity_events_fts, rowid, app_name, window_title, url_search_text, selection_text, searchable_text)
                VALUES ('delete', old.rowid, old.app_name, old.window_title, old.url_search_text, old.selection_text, old.searchable_text);
            END;
            CREATE TRIGGER IF NOT EXISTS activity_events_au AFTER UPDATE ON activity_events BEGIN
                INSERT INTO activity_events_fts(activity_events_fts, rowid, app_name, window_title, url_search_text, selection_text, searchable_text)
                VALUES ('delete', old.rowid, old.app_name, old.window_title, old.url_search_text, old.selection_text, old.searchable_text);
                INSERT INTO activity_events_fts(rowid, app_name, window_title, url_search_text, selection_text, searchable_text)
                VALUES (new.rowid, new.app_name, new.window_title, new.url_search_text, new.selection_text, new.searchable_text);
            END;
            CREATE TRIGGER IF NOT EXISTS activity_summaries_ai AFTER INSERT ON activity_summaries BEGIN
                INSERT INTO activity_summaries_fts(rowid, content) VALUES (new.rowid, new.content);
            END;
            CREATE TRIGGER IF NOT EXISTS activity_summaries_ad AFTER DELETE ON activity_summaries BEGIN
                INSERT INTO activity_summaries_fts(activity_summaries_fts, rowid, content)
                VALUES ('delete', old.rowid, old.content);
            END;
            CREATE TRIGGER IF NOT EXISTS activity_summaries_au AFTER UPDATE ON activity_summaries BEGIN
                INSERT INTO activity_summaries_fts(activity_summaries_fts, rowid, content)
                VALUES ('delete', old.rowid, old.content);
                INSERT INTO activity_summaries_fts(rowid, content) VALUES (new.rowid, new.content);
            END;
            """
        )
        # This migration must add the column before creating its index.  It is
        # intentionally idempotent: concurrent reopeners either see the
        # completed schema or wait for the normal SQLite writer transaction.
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            event_columns = {
                str(row[1]) for row in self.connection.execute("PRAGMA table_info(activity_events)")
            }
            added_event_epoch = "occurred_at_us" not in event_columns
            if added_event_epoch:
                self.connection.execute("ALTER TABLE activity_events ADD COLUMN occurred_at_us INTEGER")
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_events_occurred_at_us "
                "ON activity_events(occurred_at_us)"
            )
            summary_columns = {
                str(row[1]) for row in self.connection.execute("PRAGMA table_info(activity_summaries)")
            }
            added_summary_epoch = "period_end_us" not in summary_columns
            if added_summary_epoch:
                self.connection.execute("ALTER TABLE activity_summaries ADD COLUMN period_end_us INTEGER")
            self.connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_activity_summaries_period_end_us "
                "ON activity_summaries(period_end_us)"
            )
            # A pre-FTS legacy archive can have rows but no external-content
            # index entries yet. Rebuild before the backfill update fires the
            # normal update trigger.
            if added_event_epoch or added_summary_epoch:
                self.connection.execute(
                    "INSERT INTO activity_events_fts(activity_events_fts) VALUES ('rebuild')"
                )
                self.connection.execute(
                    "INSERT INTO activity_summaries_fts(activity_summaries_fts) VALUES ('rebuild')"
                )
            self.connection.execute(
                "UPDATE activity_events SET occurred_at_us = activity_timestamp_us(occurred_at) "
                "WHERE occurred_at_us IS NULL"
            )
            self.connection.execute(
                "UPDATE activity_summaries SET period_end_us = activity_timestamp_us(period_end) "
                "WHERE period_end_us IS NULL"
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def table_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
        )
        return {row[0] for row in rows}

    def journal_mode(self) -> str:
        return str(self.connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def add_event_batch(self, events: Iterable[ActivityEvent], cursor: SourceCursor) -> int:
        rows = list(events)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            cutoff = self._cutoff_in_transaction()
            inserted = 0
            for event in rows:
                if not is_valid_timestamp(event.occurred_at):
                    continue
                if cutoff is not None and self._is_before(event.occurred_at, cutoff):
                    continue
                result = self.connection.execute(
                    """INSERT OR IGNORE INTO activity_events
                    (segment_id, event_id, occurred_at, occurred_at_us, kind, app_name, bundle_id,
                     window_title, url, url_search_text, selection_text, searchable_text, raw_json, imported_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event.segment_id, event.event_id, event.occurred_at,
                     _timestamp_microseconds(event.occurred_at), event.kind,
                     event.app_name, event.bundle_id, event.window_title, event.url,
                     _url_search_text(event.url), event.selection_text, event.searchable_text,
                     event.raw_json, event.imported_at),
                )
                inserted += result.rowcount
            self._upsert_cursor(cursor)
            self.connection.commit()
            return inserted
        except Exception:
            self.connection.rollback()
            raise

    def _upsert_cursor(self, cursor: SourceCursor) -> None:
        self.connection.execute(
            """INSERT INTO sync_files
            (source_path, source_kind, source_identity, byte_offset, observed_size,
             observed_mtime_ns, last_success_at, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET
              source_kind=excluded.source_kind, source_identity=excluded.source_identity,
              byte_offset=excluded.byte_offset, observed_size=excluded.observed_size,
              observed_mtime_ns=excluded.observed_mtime_ns,
              last_success_at=excluded.last_success_at, last_error=excluded.last_error""",
            (
                cursor.source_path, cursor.source_kind, cursor.source_identity,
                cursor.byte_offset, cursor.observed_size, cursor.observed_mtime_ns,
                cursor.last_success_at, cursor.last_error,
            ),
        )

    def record_source_error(self, path: str, kind: str, detail: str) -> None:
        """Persist bounded source diagnostics without retaining source content."""
        prior = self.get_cursor(path)
        cursor = SourceCursor(
            source_path=path,
            source_kind=prior.source_kind if prior else "",
            source_identity=prior.source_identity if prior else "",
            byte_offset=prior.byte_offset if prior else 0,
            observed_size=prior.observed_size if prior else 0,
            observed_mtime_ns=prior.observed_mtime_ns if prior else 0,
            last_success_at=prior.last_success_at if prior else "",
            last_error=f"{kind}:{detail}" if detail else kind,
        )
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self._upsert_cursor(cursor)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def upsert_summary(self, summary: ActivitySummary) -> bool:
        if not is_valid_timestamp(summary.period_start) or not is_valid_timestamp(summary.period_end):
            return False
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            changed = self._upsert_summary_in_transaction(summary)
            self.connection.commit()
            return changed
        except Exception:
            self.connection.rollback()
            raise

    def _upsert_summary_in_transaction(self, summary: ActivitySummary) -> bool:
        """Write under the caller's transaction, preserving retention and FTS.

        Used when a generated summary and its input revision must commit or
        roll back together. The caller must acquire the write transaction.
        """
        if not self.connection.in_transaction:
            raise RuntimeError("summary write requires an active transaction")
        if not is_valid_timestamp(summary.period_start) or not is_valid_timestamp(summary.period_end):
            return False
        cutoff = self._cutoff_in_transaction()
        if cutoff is not None and self._is_at_or_before(summary.period_end, cutoff):
            return False
        prior = self.connection.execute(
            "SELECT content_hash, source_mtime_ns FROM activity_summaries WHERE summary_id=?",
            (summary.summary_id,),
        ).fetchone()
        self.connection.execute(
            """INSERT INTO activity_summaries
            (summary_id, source_path, granularity, period_start, period_end, period_end_us, content,
             content_hash, source_mtime_ns, imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(summary_id) DO UPDATE SET source_path=excluded.source_path,
             granularity=excluded.granularity, period_start=excluded.period_start,
             period_end=excluded.period_end, period_end_us=excluded.period_end_us, content=excluded.content,
             content_hash=excluded.content_hash, source_mtime_ns=excluded.source_mtime_ns,
             imported_at=excluded.imported_at""",
            (
                summary.summary_id, summary.source_path, summary.granularity,
                summary.period_start, summary.period_end,
                _timestamp_microseconds(summary.period_end), summary.content,
                summary.content_hash, summary.source_mtime_ns, summary.imported_at,
            ),
        )
        return prior is None or (prior[0], prior[1]) != (summary.content_hash, summary.source_mtime_ns)

    def get_cursor(self, source_path: str) -> SourceCursor | None:
        row = self.connection.execute("SELECT * FROM sync_files WHERE source_path=?", (source_path,)).fetchone()
        return SourceCursor(**dict(row)) if row else None

    def get_summary_by_source(self, source_path: str) -> ActivitySummary | None:
        row = self.connection.execute(
            "SELECT * FROM activity_summaries WHERE source_path=?", (source_path,)
        ).fetchone()
        if row is None:
            return None
        values = dict(row)
        values.pop("period_end_us", None)
        return ActivitySummary(**values)

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM activity_meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def clear(self, before: str) -> dict[str, int]:
        """Delete canonical evidence before a UTC cutoff and make that cutoff permanent."""
        _requested_at, requested = _utc_timestamp(before)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            prior = self._cutoff_in_transaction()
            effective = requested
            if prior is not None and _utc_timestamp(prior)[0] > _requested_at:
                effective = prior
            event_result = self.connection.execute(
                """DELETE FROM activity_events
                WHERE activity_timestamp_us(occurred_at) IS NULL
                   OR activity_timestamp_us(occurred_at) < activity_timestamp_us(?)""", (effective,)
            )
            summary_result = self.connection.execute(
                """DELETE FROM activity_summaries
                WHERE activity_timestamp_us(period_start) IS NULL
                   OR activity_timestamp_us(period_end) IS NULL
                   OR activity_timestamp_us(period_end) <= activity_timestamp_us(?)""", (effective,)
            )
            self.connection.execute(
                """INSERT INTO activity_meta(key, value) VALUES ('do_not_import_before', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (effective,),
            )
            self.connection.commit()
            return {"events_deleted": event_result.rowcount, "summaries_deleted": summary_result.rowcount}
        except Exception:
            self.connection.rollback()
            raise

    def rebuild_fts(self) -> dict[str, int]:
        """Rebuild the two derived FTS indexes without changing canonical evidence."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("INSERT INTO activity_events_fts(activity_events_fts) VALUES ('rebuild')")
            self.connection.execute("INSERT INTO activity_summaries_fts(activity_summaries_fts) VALUES ('rebuild')")
            self._validate_fts()
            counts = {
                "events": self.connection.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0],
                "summaries": self.connection.execute("SELECT COUNT(*) FROM activity_summaries").fetchone()[0],
            }
            self.connection.commit()
            return counts
        except Exception:
            self.connection.rollback()
            raise

    def _validate_fts(self) -> None:
        """Verify each external-content index against its canonical content table."""
        self.connection.execute("SAVEPOINT activity_fts_validation")
        try:
            self.connection.execute(
                "INSERT INTO activity_events_fts(activity_events_fts, rank) VALUES ('integrity-check', 1)"
            )
            self.connection.execute(
                "INSERT INTO activity_summaries_fts(activity_summaries_fts, rank) VALUES ('integrity-check', 1)"
            )
            self.connection.execute("RELEASE activity_fts_validation")
        except Exception:
            self.connection.execute("ROLLBACK TO activity_fts_validation")
            self.connection.execute("RELEASE activity_fts_validation")
            raise

    def integrity_status(self) -> dict[str, str]:
        """Check canonical SQLite and derived FTS integrity without changing archive rows."""
        rows = self.connection.execute("PRAGMA integrity_check").fetchall()
        canonical_status = "ok" if len(rows) == 1 and rows[0][0] == "ok" else "error"
        try:
            self._validate_fts()
        except sqlite3.DatabaseError:
            fts_status = "error"
        else:
            fts_status = "ok"
        return {"integrity_status": canonical_status, "fts_status": fts_status}

    def search(
        self,
        query: str,
        *,
        start: str = "",
        end: str = "",
        app: str = "",
        domain: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Search bounded, sanitized local evidence. Summaries precede raw events."""
        terms = _search_terms(query)
        if not terms:
            return self.browse(limit=limit, start=start, end=end)
        start_utc, end_utc = _time_window(start, end)
        bounded_limit = _clamp(limit, 1, _MAX_RESULTS)
        summaries = self._query_summaries(terms, start_utc, end_utc, bounded_limit)
        events = self._query_events(terms, start_utc, end_utc, app, domain, bounded_limit)
        if app or domain:
            # Summaries carry no app or domain, so they cannot be filtered by one.
            # They stay first but only take the room the matching events leave.
            summaries = summaries[:max(0, bounded_limit - len(events))]
        return self._bound_results([*summaries, *events], bounded_limit)

    def browse(self, limit: int = 5, *, start: str = "", end: str = "") -> list[dict[str, Any]]:
        """Hour buckets of recent activity, newest first, inside ``start``/``end`` when given."""
        bounded_limit = _clamp(limit, 1, _MAX_RESULTS)
        start_utc, end_utc = _time_window(start, end)
        summary_clauses: list[str] = []
        event_clauses: list[str] = []
        window: list[str] = []
        if start_utc:
            summary_clauses.append("activity_timestamp_us(period_end) >= activity_timestamp_us(?)")
            event_clauses.append("activity_timestamp_us(occurred_at) >= activity_timestamp_us(?)")
            window.append(start_utc)
        if end_utc:
            summary_clauses.append("activity_timestamp_us(period_start) <= activity_timestamp_us(?)")
            event_clauses.append("activity_timestamp_us(occurred_at) <= activity_timestamp_us(?)")
            window.append(end_utc)
        summary_rows = self.connection.execute(
            "SELECT * FROM activity_summaries "
            + (f"WHERE {' AND '.join(summary_clauses)} " if summary_clauses else "")
            + "ORDER BY period_end_us DESC, rowid DESC LIMIT ?",
            (*window, bounded_limit * 4),
        ).fetchall()
        event_rows = self.connection.execute(
            "SELECT * FROM activity_events "
            + (f"WHERE {' AND '.join(event_clauses)} " if event_clauses else "")
            + "ORDER BY activity_timestamp_us(occurred_at) DESC, rowid DESC LIMIT ?",
            (*window, bounded_limit * 40),
        ).fetchall()
        buckets: dict[str, dict[str, Any]] = {}

        def bucket_for(timestamp: str) -> dict[str, Any] | None:
            try:
                parsed, _normalized = _utc_timestamp(timestamp)
            except ValueError:
                return None
            start = parsed.replace(minute=0, second=0, microsecond=0)
            key = start.isoformat()
            return buckets.setdefault(
                key,
                {
                    "source": "computer_history",
                    "evidence_type": "summary",
                    "untrusted_observation": True,
                    "bucket_start": key,
                    "bucket_end": (start + timedelta(hours=1)).isoformat(),
                    "principal_apps": Counter(),
                    "summary_previews": [],
                    "summary_ids": [],
                },
            )

        for row in summary_rows:
            bucket = bucket_for(str(row["period_start"]))
            if bucket is not None and len(bucket["summary_previews"]) < 3:
                bucket["summary_previews"].append(str(row["content"]))
                # Same order as the previews: what expand needs to read one in full.
                bucket["summary_ids"].append(str(row["summary_id"]))
        for row in event_rows:
            bucket = bucket_for(str(row["occurred_at"]))
            app_name = str(row["app_name"] or "")
            if bucket is not None and app_name:
                bucket["principal_apps"][app_name] += 1

        output: list[dict[str, Any]] = []
        for key in sorted(buckets, reverse=True)[:bounded_limit]:
            bucket = buckets[key]
            app_counts: Counter[str] = bucket["principal_apps"]
            principal_apps = [
                app
                for app, _count in sorted(
                    app_counts.items(),
                    key=lambda item: (-item[1], item[0].casefold()),
                )[:5]
            ]
            previews = list(bucket["summary_previews"])
            snippet = previews[0] if previews else (
                f"Principal applications: {', '.join(principal_apps)}"
                if principal_apps
                else "Recent activity"
            )
            output.append(_bounded_item({
                **bucket,
                "principal_apps": principal_apps,
                "summary_previews": previews,
                "snippet": snippet,
            }))
        return self._bound_results(output, bounded_limit)

    def expand(
        self,
        *,
        summary_id: str = "",
        segment_id: str = "",
        event_id: int = 0,
        window: int = 3,
    ) -> dict[str, Any]:
        if summary_id:
            row = self.connection.execute("SELECT * FROM activity_summaries WHERE summary_id=?", (summary_id,)).fetchone()
            return _bounded_payload(self._summary_output(row)) if row else {}
        if not segment_id or not event_id:
            return {}
        target = self.connection.execute(
            "SELECT * FROM activity_events WHERE segment_id=? AND event_id=?", (segment_id, event_id)
        ).fetchone()
        if target is None:
            return {}
        bounded_window = _clamp(window, 0, _MAX_RESULTS)
        nearby = self.connection.execute(
            """WITH ordered AS (
                SELECT activity_events.*, ROW_NUMBER() OVER (ORDER BY event_id, rowid) AS position
                FROM activity_events WHERE segment_id=?
            ), target_position AS (
                SELECT position FROM ordered WHERE event_id=?
            )
            SELECT * FROM ordered
            WHERE position BETWEEN (SELECT position - ? FROM target_position)
                               AND (SELECT position + ? FROM target_position)
            ORDER BY position""",
            (segment_id, event_id, bounded_window, bounded_window),
        ).fetchall()
        output = _bounded_payload(self._event_output(target))
        output["context"] = [_bounded_payload(self._event_output(row)) for row in nearby if row["event_id"] != event_id]
        return _bounded_payload(output, _MAX_TOTAL_CHARS)

    def _cutoff_in_transaction(self) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM activity_meta WHERE key='do_not_import_before'"
        ).fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def _is_before(value: str, cutoff: str) -> bool:
        try:
            return _utc_timestamp(value)[0] < _utc_timestamp(cutoff)[0]
        except ValueError:
            return True

    @staticmethod
    def _is_at_or_before(value: str, cutoff: str) -> bool:
        try:
            return _utc_timestamp(value)[0] <= _utc_timestamp(cutoff)[0]
        except ValueError:
            return True

    def _query_summaries(self, terms: list[str], start: str, end: str, limit: int) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if _requires_like_fallback(terms):
            for term in terms:
                clauses.append("activity_summaries.content LIKE ?")
                parameters.append(f"%{term}%")
            rank = "0.0"
        else:
            clauses.append("activity_summaries_fts MATCH ?")
            parameters.append(_fts_query(terms))
            rank = "bm25(activity_summaries_fts)"
        if start:
            clauses.append("activity_timestamp_us(activity_summaries.period_end) >= activity_timestamp_us(?)")
            parameters.append(start)
        if end:
            clauses.append("activity_timestamp_us(activity_summaries.period_start) <= activity_timestamp_us(?)")
            parameters.append(end)
        rows = self.connection.execute(
            f"""SELECT activity_summaries.* FROM activity_summaries
            {'JOIN activity_summaries_fts ON activity_summaries_fts.rowid=activity_summaries.rowid' if not _requires_like_fallback(terms) else ''}
            WHERE {' AND '.join(clauses)} ORDER BY {rank}, activity_summaries.rowid DESC LIMIT ?""",
            (*parameters, limit),
        ).fetchall()
        return [self._summary_output(row) for row in rows]

    def _query_events(
        self, terms: list[str], start: str, end: str, app: str, domain: str, limit: int
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        searchable = " || ' ' || ".join(
            ("activity_events.app_name", "activity_events.window_title", "activity_events.url_search_text", "activity_events.selection_text", "activity_events.searchable_text")
        )
        if _requires_like_fallback(terms):
            for term in terms:
                clauses.append(f"({searchable}) LIKE ?")
                parameters.append(f"%{term}%")
            rank = "0.0"
        else:
            clauses.append("activity_events_fts MATCH ?")
            parameters.append(_fts_query(terms))
            rank = "bm25(activity_events_fts)"
        if start:
            clauses.append("activity_timestamp_us(activity_events.occurred_at) >= activity_timestamp_us(?)")
            parameters.append(start)
        if end:
            clauses.append("activity_timestamp_us(activity_events.occurred_at) <= activity_timestamp_us(?)")
            parameters.append(end)
        if app:
            clauses.append("activity_events.app_name = ? COLLATE NOCASE")
            parameters.append(app)
        if domain:
            normalized_domain = _domain_host(domain)
            if not normalized_domain:
                return []
            clauses.append("activity_url_host(activity_events.url_search_text) = ?")
            parameters.append(normalized_domain)
        rows = self.connection.execute(
            f"""SELECT activity_events.* FROM activity_events
            {'JOIN activity_events_fts ON activity_events_fts.rowid=activity_events.rowid' if not _requires_like_fallback(terms) else ''}
            WHERE {' AND '.join(clauses)} ORDER BY {rank}, activity_timestamp_us(activity_events.occurred_at) DESC, activity_events.rowid DESC LIMIT ?""",
            (*parameters, limit),
        ).fetchall()
        return [self._event_output(row) for row in rows]

    @staticmethod
    def _summary_output(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "evidence_type": "summary", "source": "computer_history", "untrusted_observation": True,
            "summary_id": row["summary_id"], "granularity": row["granularity"],
            "period_start": row["period_start"], "period_end": row["period_end"], "snippet": row["content"],
        }

    @staticmethod
    def _event_output(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "evidence_type": "raw_event", "source": "computer_history", "untrusted_observation": True,
            "segment_id": row["segment_id"], "event_id": row["event_id"], "occurred_at": row["occurred_at"],
            "kind": row["kind"], "app": row["app_name"], "window_title": row["window_title"],
            "url": sanitize_url(row["url"]), "snippet": " ".join(
                part for part in (row["selection_text"], row["searchable_text"]) if part
            ),
        }

    @staticmethod
    def _dedupe_key(item: dict[str, Any]) -> str:
        if item.get("bucket_start"):
            return f"activity_bucket:{item['bucket_start']}"
        return " ".join(str(item.get("snippet", "")).casefold().split())

    def _bound_results(self, items: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            dedupe_key = self._dedupe_key(item)
            if dedupe_key and dedupe_key in seen:
                continue
            if dedupe_key:
                seen.add(dedupe_key)
            bounded = _bounded_item(item)
            candidate = [*results, bounded]
            if len(json.dumps(candidate, ensure_ascii=False)) > _MAX_TOTAL_CHARS:
                break
            results.append(bounded)
            if len(results) >= limit:
                break
        return results

    def stats(self) -> dict[str, Any]:
        return {
            "events": self.connection.execute("SELECT COUNT(*) FROM activity_events").fetchone()[0],
            "summaries": self.connection.execute("SELECT COUNT(*) FROM activity_summaries").fetchone()[0],
            "sync_files": self.connection.execute("SELECT COUNT(*) FROM sync_files").fetchone()[0],
        }

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ActivityStore":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()
