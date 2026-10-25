from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class MailConfig:
    project_root: Path
    account: str
    auth_code: str
    host: str
    port: int
    database_path: Path
    attachments_dir: Path
    max_body_bytes: int
    default_folders: tuple[str, ...]
    # The account's sent folder, which "sent" means; and whether the server needs the IMAP ID
    # command before login (163 and its sister domains refuse a login without it).
    sent_folder: str = "已发送"
    send_id: bool = True
    # A folder's first sync caches only its newest messages; later syncs fetch what arrived
    # since, never older history.
    sync_limit: int = 300


@dataclass(frozen=True)
class BodyPart:
    part_id: str
    content_type: str
    charset: str
    transfer_encoding: str
    encoded_size: int
    disposition: str
    filename: str


@dataclass(frozen=True)
class AttachmentMeta:
    part_id: str
    filename: str
    content_type: str
    size: int


@dataclass(frozen=True)
class RemoteMessage:
    folder: str
    uidvalidity: int
    uid: int
    message_id: str
    sender: str
    recipients: str
    subject: str
    sent_at: str
    received_at: float
    flags: tuple[str, ...]
    size: int
    body_text: str
    body_truncated: bool
    attachments: tuple[AttachmentMeta, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class FolderSnapshot:
    folder: str
    uidvalidity: int
    uids: frozenset[int]
    flags: Mapping[int, tuple[str, ...]]


@dataclass(frozen=True)
class FolderSyncResult:
    folder: str
    status: Literal["ok", "failed"]
    fetched: int
    updated_flags: int
    remote_removed: int
    synced_at: float | None
    error_code: str
    error_message: str


@dataclass(frozen=True)
class SyncReport:
    status: Literal["ok", "partial", "failed"]
    folders: tuple[FolderSyncResult, ...]
