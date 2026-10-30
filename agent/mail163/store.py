from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .models import AttachmentMeta, RemoteMessage

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS folders (
  account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL,
  last_success REAL, last_error_code TEXT NOT NULL DEFAULT '', last_error_message TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (account, folder)
);
CREATE TABLE IF NOT EXISTS messages (
  account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
  message_id TEXT NOT NULL DEFAULT '', sender TEXT NOT NULL DEFAULT '', recipients TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '', sent_at TEXT NOT NULL DEFAULT '', received_at REAL NOT NULL,
  flags_json TEXT NOT NULL DEFAULT '[]',
  size INTEGER NOT NULL DEFAULT 0, body_text TEXT NOT NULL DEFAULT '', body_truncated INTEGER NOT NULL DEFAULT 0,
  warnings_json TEXT NOT NULL DEFAULT '[]', remote_removed INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account, folder, uidvalidity, uid)
);
CREATE TABLE IF NOT EXISTS attachments (
  account TEXT NOT NULL, folder TEXT NOT NULL, uidvalidity INTEGER NOT NULL, uid INTEGER NOT NULL,
  part_id TEXT NOT NULL, filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL,
  downloaded_path TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (account, folder, uidvalidity, uid, part_id),
  FOREIGN KEY (account, folder, uidvalidity, uid)
    REFERENCES messages(account, folder, uidvalidity, uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_recent ON messages(account, folder, received_at DESC, uid DESC);
CREATE INDEX IF NOT EXISTS idx_messages_subject ON messages(account, subject);
"""

_MESSAGE_COLUMNS = """
account, folder, uidvalidity, uid, message_id, sender, recipients, subject,
sent_at, received_at, flags_json, size, body_text, body_truncated,
warnings_json, remote_removed
"""


@dataclass(frozen=True)
class FolderState:
    uidvalidity: int
    last_success: float | None
    last_error_code: str
    last_error_message: str


class MailStore:
    """Transactional local cache for 163 mail metadata and text bodies."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = path
        self.read_only = read_only
        if not read_only:
            self._prepare_writable_path()
            self._initialize_schema()

    def exists(self) -> bool:
        return self.path.is_file()

    def folder_state(self, account: str, folder: str) -> FolderState | None:
        with self._session() as db:
            row = db.execute(
                """
                SELECT uidvalidity, last_success, last_error_code, last_error_message
                FROM folders WHERE account = ? AND folder = ?
                """,
                (account, folder),
            ).fetchone()
        if row is None:
            return None
        return FolderState(
            uidvalidity=row["uidvalidity"],
            last_success=row["last_success"],
            last_error_code=row["last_error_code"],
            last_error_message=row["last_error_message"],
        )

    def known_uids(self, account: str, folder: str, uidvalidity: int) -> set[int]:
        with self._session() as db:
            rows = db.execute(
                """
                SELECT uid FROM messages
                WHERE account = ? AND folder = ? AND uidvalidity = ?
                """,
                (account, folder, uidvalidity),
            ).fetchall()
        return {row["uid"] for row in rows}

    def apply_folder_sync(
        self,
        account: str,
        folder: str,
        uidvalidity: int,
        remote_uids: set[int],
        messages: Sequence[RemoteMessage],
        flags: Mapping[int, tuple[str, ...]],
        synced_at: float,
    ) -> tuple[int, int, int]:
        self._require_writable()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            remote_removed = self._mark_prior_generations_removed(db, account, folder, uidvalidity)

            for remote_message in messages:
                if remote_message.folder != folder or remote_message.uidvalidity != uidvalidity:
                    raise ValueError("message identity does not match folder synchronization target")
                self._upsert_message(db, account, remote_message)
                self._replace_attachments(
                    db,
                    account,
                    folder,
                    uidvalidity,
                    remote_message.uid,
                    remote_message.attachments,
                )

            updated_flags = self._update_flags(db, account, folder, uidvalidity, flags)
            remote_removed += self._update_remote_removals(db, account, folder, uidvalidity, remote_uids)
            db.execute(
                """
                INSERT INTO folders (
                    account, folder, uidvalidity, last_success, last_error_code, last_error_message
                ) VALUES (?, ?, ?, ?, '', '')
                ON CONFLICT(account, folder) DO UPDATE SET
                    uidvalidity = excluded.uidvalidity,
                    last_success = excluded.last_success,
                    last_error_code = '',
                    last_error_message = ''
                """,
                (account, folder, uidvalidity, synced_at),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
        return len(messages), updated_flags, remote_removed

    def record_folder_error(self, account: str, folder: str, code: str, message: str) -> None:
        self._require_writable()
        with self._session() as db:
            db.execute(
                """
                INSERT INTO folders (
                    account, folder, uidvalidity, last_success, last_error_code, last_error_message
                ) VALUES (?, ?, 0, NULL, ?, ?)
                ON CONFLICT(account, folder) DO UPDATE SET
                    last_error_code = excluded.last_error_code,
                    last_error_message = excluded.last_error_message
                """,
                (account, folder, code, message),
            )

    def recent(
        self,
        account: str,
        *,
        folder: str,
        limit: int,
        include_removed: bool = False,
    ) -> list[dict[str, object]]:
        removed_clause = "" if include_removed else "AND remote_removed = 0"
        with self._session() as db:
            rows = db.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS} FROM messages
                WHERE account = ? AND folder = ? {removed_clause}
                ORDER BY received_at DESC, uid DESC
                LIMIT ?
                """,
                (account, folder, limit),
            ).fetchall()
        return [self._message_dict(row) for row in rows]

    def after(self, account: str, folder: str, uidvalidity: int, uid: int, *, limit: int) -> list[dict[str, object]]:
        """Messages of one folder generation numbered above `uid`, oldest first (removed ones left out)."""
        with self._session() as db:
            rows = db.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS} FROM messages
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid > ? AND remote_removed = 0
                ORDER BY uid
                LIMIT ?
                """,
                (account, folder, uidvalidity, uid, limit),
            ).fetchall()
        return [self._message_dict(row) for row in rows]

    def search(
        self,
        account: str,
        query: str = "",
        *,
        sender: str = "",
        folder: str | None,
        limit: int,
        window: int = 0,
        include_removed: bool = False,
    ) -> list[dict[str, object]]:
        candidate_filters = ["account = ?"]
        candidate_parameters: list[object] = [account]
        if folder is not None:
            candidate_filters.append("folder = ?")
            candidate_parameters.append(folder)
        if not include_removed:
            candidate_filters.append("remote_removed = 0")

        result_filters: list[str] = []
        result_parameters: list[object] = []
        if query:
            pattern = f"%{_escape_like(query)}%"
            result_filters.append(
                """(
                    sender LIKE ? ESCAPE '\\' OR recipients LIKE ? ESCAPE '\\'
                    OR subject LIKE ? ESCAPE '\\' OR body_text LIKE ? ESCAPE '\\'
                )"""
            )
            result_parameters.extend([pattern] * 4)
        if sender:
            result_filters.append("sender LIKE ? ESCAPE '\\'")
            result_parameters.append(f"%{_escape_like(sender)}%")

        candidates_where = " AND ".join(candidate_filters)
        results_where = f"WHERE {' AND '.join(result_filters)}" if result_filters else ""
        if window > 0:
            sql = f"""
                SELECT {_MESSAGE_COLUMNS} FROM (
                    SELECT {_MESSAGE_COLUMNS} FROM messages
                    WHERE {candidates_where}
                    ORDER BY received_at DESC, uid DESC
                    LIMIT ?
                ) AS candidates
                {results_where}
                ORDER BY received_at DESC, uid DESC
                LIMIT ?
            """
            parameters = [*candidate_parameters, window, *result_parameters, limit]
        else:
            all_filters = [*candidate_filters, *result_filters]
            sql = f"""
                SELECT {_MESSAGE_COLUMNS} FROM messages
                WHERE {" AND ".join(all_filters)}
                ORDER BY received_at DESC, uid DESC
                LIMIT ?
            """
            parameters = [*candidate_parameters, *result_parameters, limit]

        with self._session() as db:
            rows = db.execute(sql, parameters).fetchall()
        return [self._message_dict(row) for row in rows]

    def read(self, account: str, folder: str, uidvalidity: int, uid: int) -> dict[str, object] | None:
        with self._session() as db:
            row = db.execute(
                f"""
                SELECT {_MESSAGE_COLUMNS} FROM messages
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                """,
                (account, folder, uidvalidity, uid),
            ).fetchone()
            if row is None:
                return None
            attachments = db.execute(
                """
                SELECT part_id, filename, content_type, size, downloaded_path
                FROM attachments
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                ORDER BY part_id
                """,
                (account, folder, uidvalidity, uid),
            ).fetchall()
        result = self._message_dict(row)
        result["attachments"] = [self._attachment_dict(attachment_row) for attachment_row in attachments]
        return result

    def attachment(
        self,
        account: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        part_id: str,
    ) -> dict[str, object] | None:
        with self._session() as db:
            row = db.execute(
                """
                SELECT part_id, filename, content_type, size, downloaded_path
                FROM attachments
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ? AND part_id = ?
                """,
                (account, folder, uidvalidity, uid, part_id),
            ).fetchone()
        if row is None:
            return None
        result = self._attachment_dict(row)
        result.update({"folder": folder, "uidvalidity": uidvalidity, "uid": uid})
        return result

    def mark_attachment_downloaded(
        self,
        account: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        part_id: str,
        path: Path,
    ) -> None:
        self._require_writable()
        with self._session() as db:
            db.execute(
                """
                UPDATE attachments SET downloaded_path = ?
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ? AND part_id = ?
                """,
                (str(path), account, folder, uidvalidity, uid, part_id),
            )

    def folder_states(self, account: str) -> list[dict[str, object]]:
        with self._session() as db:
            rows = db.execute(
                """
                SELECT folder, uidvalidity, last_success, last_error_code, last_error_message
                FROM folders WHERE account = ? ORDER BY folder
                """,
                (account,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _prepare_writable_path(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.path.parent, 0o700)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        if os.name == "posix":
            os.chmod(self.path, 0o600)

    def _initialize_schema(self) -> None:
        with self._session() as db:
            db.executescript(_SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", "1"),
            )

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            uri = f"file:{quote(str(self.path.resolve()), safe='/')}?mode=ro"
            db = sqlite3.connect(uri, uri=True)
        else:
            db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _require_writable(self) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("mail cache is read-only")

    @staticmethod
    def _mark_prior_generations_removed(
        db: sqlite3.Connection,
        account: str,
        folder: str,
        uidvalidity: int,
    ) -> int:
        cursor = db.execute(
            """
            UPDATE messages SET remote_removed = 1
            WHERE account = ? AND folder = ? AND uidvalidity <> ? AND remote_removed = 0
            """,
            (account, folder, uidvalidity),
        )
        return cursor.rowcount

    @staticmethod
    def _upsert_message(db: sqlite3.Connection, account: str, message: RemoteMessage) -> None:
        db.execute(
            """
            INSERT INTO messages (
                account, folder, uidvalidity, uid, message_id, sender, recipients, subject,
                sent_at, received_at, flags_json, size, body_text, body_truncated,
                warnings_json, remote_removed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(account, folder, uidvalidity, uid) DO UPDATE SET
                message_id = excluded.message_id,
                sender = excluded.sender,
                recipients = excluded.recipients,
                subject = excluded.subject,
                sent_at = excluded.sent_at,
                received_at = excluded.received_at,
                flags_json = excluded.flags_json,
                size = excluded.size,
                body_text = excluded.body_text,
                body_truncated = excluded.body_truncated,
                warnings_json = excluded.warnings_json,
                remote_removed = 0
            """,
            (
                account,
                message.folder,
                message.uidvalidity,
                message.uid,
                message.message_id,
                message.sender,
                message.recipients,
                message.subject,
                message.sent_at,
                message.received_at,
                _encode_json(message.flags),
                message.size,
                message.body_text,
                int(message.body_truncated),
                _encode_json(message.warnings),
            ),
        )

    @staticmethod
    def _replace_attachments(
        db: sqlite3.Connection,
        account: str,
        folder: str,
        uidvalidity: int,
        uid: int,
        attachments: Sequence[AttachmentMeta],
    ) -> None:
        if attachments:
            placeholders = ", ".join("?" for _ in attachments)
            db.execute(
                f"""
                DELETE FROM attachments
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                  AND part_id NOT IN ({placeholders})
                """,
                (account, folder, uidvalidity, uid, *(attachment.part_id for attachment in attachments)),
            )
        else:
            db.execute(
                """
                DELETE FROM attachments
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ?
                """,
                (account, folder, uidvalidity, uid),
            )
        db.executemany(
            """
            INSERT INTO attachments (
                account, folder, uidvalidity, uid, part_id, filename, content_type, size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account, folder, uidvalidity, uid, part_id) DO UPDATE SET
                filename = excluded.filename,
                content_type = excluded.content_type,
                size = excluded.size
            """,
            [
                (
                    account,
                    folder,
                    uidvalidity,
                    uid,
                    attachment.part_id,
                    attachment.filename,
                    attachment.content_type,
                    attachment.size,
                )
                for attachment in attachments
            ],
        )

    @staticmethod
    def _update_flags(
        db: sqlite3.Connection,
        account: str,
        folder: str,
        uidvalidity: int,
        flags: Mapping[int, tuple[str, ...]],
    ) -> int:
        updated = 0
        for uid, current_flags in flags.items():
            encoded = _encode_json(current_flags)
            cursor = db.execute(
                """
                UPDATE messages SET flags_json = ?
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND uid = ? AND flags_json <> ?
                """,
                (encoded, account, folder, uidvalidity, uid, encoded),
            )
            updated += cursor.rowcount
        return updated

    @staticmethod
    def _update_remote_removals(
        db: sqlite3.Connection,
        account: str,
        folder: str,
        uidvalidity: int,
        remote_uids: set[int],
    ) -> int:
        if remote_uids:
            db.execute(
                "CREATE TEMP TABLE IF NOT EXISTS remote_sync_uids "
                "(uid INTEGER PRIMARY KEY) WITHOUT ROWID"
            )
            db.execute("DELETE FROM remote_sync_uids")
            db.executemany(
                "INSERT INTO remote_sync_uids (uid) VALUES (?)",
                ((uid,) for uid in sorted(remote_uids)),
            )
            removed = db.execute(
                """
                UPDATE messages SET remote_removed = 1
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND remote_removed = 0
                  AND NOT EXISTS (
                    SELECT 1 FROM remote_sync_uids WHERE remote_sync_uids.uid = messages.uid
                  )
                """,
                (account, folder, uidvalidity),
            ).rowcount
            db.execute(
                """
                UPDATE messages SET remote_removed = 0
                WHERE account = ? AND folder = ? AND uidvalidity = ? AND remote_removed = 1
                  AND EXISTS (
                    SELECT 1 FROM remote_sync_uids WHERE remote_sync_uids.uid = messages.uid
                  )
                """,
                (account, folder, uidvalidity),
            )
            return removed
        return db.execute(
            """
            UPDATE messages SET remote_removed = 1
            WHERE account = ? AND folder = ? AND uidvalidity = ? AND remote_removed = 0
            """,
            (account, folder, uidvalidity),
        ).rowcount

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, object]:
        flags = tuple(json.loads(row["flags_json"]))
        return {
            "folder": row["folder"],
            "uidvalidity": row["uidvalidity"],
            "uid": row["uid"],
            "message_id": row["message_id"],
            "sender": row["sender"],
            "recipients": row["recipients"],
            "subject": row["subject"],
            "sent_at": row["sent_at"],
            "received_at": row["received_at"],
            "flags": flags,
            "unread": "\\Seen" not in flags,
            "size": row["size"],
            "body_text": row["body_text"],
            "body_truncated": bool(row["body_truncated"]),
            "warnings": tuple(json.loads(row["warnings_json"])),
            "remote_removed": bool(row["remote_removed"]),
        }

    @staticmethod
    def _attachment_dict(row: sqlite3.Row) -> dict[str, object]:
        return {
            "part_id": row["part_id"],
            "filename": row["filename"],
            "content_type": row["content_type"],
            "size": row["size"],
            "downloaded_path": row["downloaded_path"],
        }


def _encode_json(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
