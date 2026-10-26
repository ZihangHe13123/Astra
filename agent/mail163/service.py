from __future__ import annotations

import os
import secrets
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from .attachment_storage import AttachmentDirectory, AttachmentStorage, attachment_storage
from .config import resolve_folder
from .imap_client import (
    Imap163Client,
    MailAuthenticationError,
    MailNetworkError,
    MailTlsError,
    MailUnsafeLoginError,
)
from .mime import MailProtocolError, sanitize_filename
from .models import FolderSyncResult, MailConfig, SyncReport
from .store import MailStore


class MailError(RuntimeError):
    """Stable, caller-safe error raised by the mail service."""

    def __init__(self, code: str, safe_message: str, exit_code: int) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message
        self.exit_code = exit_code


@dataclass
class _AttachmentTarget:
    path: Path
    directory: AttachmentDirectory
    reservation_name: str
    released: bool = False

    @property
    def name(self) -> str:
        return self.path.name

    def exists(self) -> bool:
        return self.path.exists()


class MailService:
    """Synchronize a read-only IMAP mailbox into the local SQLite cache."""

    def __init__(
        self,
        config: MailConfig,
        *,
        store: MailStore | None = None,
        client_factory: Callable[[MailConfig], Any] = Imap163Client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.store = MailStore(config.database_path, read_only=not bool(config.account)) if store is None else store
        self.client_factory = client_factory
        self.clock = clock
        self._resolved_account = config.account or None
        self._attachment_storage: AttachmentStorage = attachment_storage()

    def sync(self, folders: Sequence[str] | None = None) -> SyncReport:
        requested = self._canonical_folders(folders)
        results: list[FolderSyncResult] = []
        try:
            with self.client_factory(self.config) as client:
                for folder in requested:
                    try:
                        snapshot = client.snapshot(folder)
                        known = self.store.known_uids(
                            self._account(),
                            folder,
                            snapshot.uidvalidity,
                        )
                        # Newest first and bounded: a large mailbox's first sync stays quick,
                        # and nothing older than the oldest cached message is fetched later.
                        floor = min(known) if known else 0
                        wanted = sorted(uid for uid in set(snapshot.uids) - known if uid > floor)
                        messages = client.fetch_messages(folder, snapshot, set(wanted[-self.config.sync_limit:]))
                        synced_at = self.clock()
                        fetched, updated_flags, remote_removed = self.store.apply_folder_sync(
                            self._account(),
                            folder,
                            snapshot.uidvalidity,
                            set(snapshot.uids),
                            messages,
                            snapshot.flags,
                            synced_at,
                        )
                        results.append(
                            FolderSyncResult(
                                folder=folder,
                                status="ok",
                                fetched=fetched,
                                updated_flags=updated_flags,
                                remote_removed=remote_removed,
                                synced_at=synced_at,
                                error_code="",
                                error_message="",
                            )
                        )
                    except Exception as error:  # noqa: BLE001 - service boundary sanitizes client failures
                        results.append(self._folder_failure(folder, error))
        except Exception as error:  # noqa: BLE001 - service boundary sanitizes lifecycle failures
            results.extend(self._folder_failure(folder, error) for folder in requested)
        return SyncReport(status=self._report_status(results), folders=tuple(results))

    def recent(
        self,
        *,
        folder: str = "INBOX",
        limit: int = 30,
        refresh: bool = True,
        include_removed: bool = False,
    ) -> dict[str, object]:
        canonical = resolve_folder(folder, self.config.sent_folder)
        report = self._refresh(canonical) if refresh else None
        self._require_cache()
        try:
            items = self.store.recent(
                self._account(),
                folder=canonical,
                limit=limit,
                include_removed=include_removed,
            )
        except sqlite3.Error as error:
            raise self._mail_error(error) from None
        self._require_fallback_cache(report, folder=canonical)
        return self._query_result(items, report, canonical)

    def search(
        self,
        query: str = "",
        *,
        sender: str = "",
        folder: str | None = None,
        limit: int = 30,
        window: int = 0,
        refresh: bool = True,
        include_removed: bool = False,
    ) -> dict[str, object]:
        canonical = resolve_folder(folder, self.config.sent_folder) if folder is not None else None
        report = self._refresh(canonical) if refresh else None
        self._require_cache()
        try:
            items = self.store.search(
                self._account(),
                query,
                sender=sender,
                folder=canonical,
                limit=limit,
                window=window,
                include_removed=include_removed,
            )
        except sqlite3.Error as error:
            raise self._mail_error(error) from None
        self._require_fallback_cache(report, folder=canonical)
        return self._query_result(items, report, canonical)

    def read(
        self,
        folder: str,
        uid: int,
        *,
        uidvalidity: int | None = None,
        refresh: bool = True,
    ) -> dict[str, object]:
        canonical = resolve_folder(folder, self.config.sent_folder)
        report = self._refresh(canonical) if refresh else None
        self._require_cache()
        self._require_fallback_cache(report, folder=canonical)
        try:
            if uidvalidity is None:
                state = self.store.folder_state(self._account(), canonical)
                uidvalidity = None if state is None or state.uidvalidity == 0 else state.uidvalidity
            item = None if uidvalidity is None else self.store.read(self._account(), canonical, uidvalidity, uid)
        except sqlite3.Error as error:
            raise self._mail_error(error) from None
        if item is None:
            raise MailError("message_not_found", "Cached message was not found", 4)
        return {"item": item, **self._result_metadata(report, canonical)}

    def folders(self, *, refresh: bool = True) -> dict[str, object]:
        if not refresh:
            states = self._folder_states()
            return {
                "items": states,
                "cache_used": True,
                "sync_status": "offline",
                "last_success": self._last_success(states),
                "errors": self._stored_errors(states),
            }

        try:
            with self.client_factory(self.config) as client:
                remote_folders = client.list_folders()
        except Exception as error:  # noqa: BLE001 - service boundary sanitizes client failures
            failure = self._mail_error(error)
            states = self._folder_states()
            if not states:
                raise MailError("no_cache", "No local mail cache is available", 4) from None
            return {
                "items": states,
                "cache_used": True,
                "sync_status": "failed",
                "last_success": self._last_success(states),
                "errors": [{"code": failure.code, "message": failure.safe_message}],
            }

        states = self._folder_states(allow_missing=True)
        return {
            "items": self._merge_folder_states(remote_folders, states, self.config.sent_folder),
            "cache_used": False,
            "sync_status": "ok",
            "last_success": self._last_success(states),
            "errors": [],
        }

    def status(self) -> dict[str, object]:
        states = self._folder_states()
        return {
            "items": states,
            "cache_used": True,
            "sync_status": "offline",
            "last_success": self._last_success(states),
            "errors": self._stored_errors(states),
        }

    def download_attachment(
        self,
        folder: str,
        uidvalidity: int,
        uid: int,
        part_id: str,
    ) -> dict[str, object]:
        canonical = resolve_folder(folder, self.config.sent_folder)
        self._require_cache()
        try:
            metadata = self.store.attachment(
                self._account(),
                canonical,
                uidvalidity,
                uid,
                part_id,
            )
        except sqlite3.Error as error:
            raise self._mail_error(error) from None
        if metadata is None:
            raise MailError("attachment_not_found", "Cached attachment metadata was not found", 4)
        try:
            with self.client_factory(self.config) as client:
                payload = client.download_attachment(canonical, uidvalidity, uid, part_id)
        except Exception as error:  # noqa: BLE001 - service boundary sanitizes client failures
            raise self._mail_error(error) from None

        while True:
            target: _AttachmentTarget | None = None
            try:
                target = self._attachment_target(canonical, uidvalidity, uid, str(metadata["filename"]))
                self._atomic_write(target, payload)
            except FileExistsError:
                continue
            except OSError:
                if target is not None and not target.released:
                    try:
                        self._release_attachment_target(target, remove_file=True)
                    except OSError:
                        pass
                raise MailError("storage_error", "Local attachment write failed", 5) from None
            break
        target_path = target.path
        try:
            self.store.mark_attachment_downloaded(
                self._account(),
                canonical,
                uidvalidity,
                uid,
                part_id,
                target_path,
            )
        except sqlite3.Error as error:
            raise self._mail_error(error) from None
        return {**metadata, "path": target_path}

    def _folder_failure(self, folder: str, error: Exception) -> FolderSyncResult:
        failure = self._mail_error(error)
        if self.store.exists():
            try:
                self.store.record_folder_error(
                    self._account(),
                    folder,
                    failure.code,
                    failure.safe_message,
                )
            except sqlite3.Error:
                pass
        return FolderSyncResult(
            folder=folder,
            status="failed",
            fetched=0,
            updated_flags=0,
            remote_removed=0,
            synced_at=None,
            error_code=failure.code,
            error_message=failure.safe_message,
        )

    def _query_result(
        self,
        items: list[dict[str, object]],
        report: SyncReport | None,
        folder: str | None,
    ) -> dict[str, object]:
        return {"items": items, **self._result_metadata(report, folder)}

    def _refresh(self, folder: str | None) -> SyncReport:
        if folder is None or folder in self.config.default_folders:
            return self.sync()
        return self.sync([folder])

    def _result_metadata(self, report: SyncReport | None, folder: str | None) -> dict[str, object]:
        if report is None:
            sync_status = "offline"
            cache_used = True
            errors: list[dict[str, str]] = []
        else:
            sync_status = report.status
            cache_used = report.status != "ok"
            errors = self._report_errors(report)
        return {
            "cache_used": cache_used,
            "sync_status": sync_status,
            "last_success": self._folder_last_success(folder),
            "errors": errors,
        }

    def _require_fallback_cache(self, report: SyncReport | None, *, folder: str | None) -> None:
        if report is not None and report.status == "ok":
            return
        try:
            if folder is not None:
                state = self.store.folder_state(self._account(), folder)
                usable = state is not None and state.last_success is not None
            else:
                states = self.store.folder_states(self._account())
                usable = any(state["last_success"] is not None for state in states)
        except sqlite3.Error as error:
            raise self._mail_error(error) from None
        if not usable:
            raise MailError("no_cache", "No local mail cache is available", 4)

    def _folder_last_success(self, folder: str | None) -> float | None:
        try:
            if folder is not None:
                state = self.store.folder_state(self._account(), folder)
                return None if state is None else state.last_success
            return self._last_success(self.store.folder_states(self._account()))
        except sqlite3.Error as error:
            raise self._mail_error(error) from None

    def _folder_states(self, *, allow_missing: bool = False) -> list[dict[str, object]]:
        if not self.store.exists():
            if allow_missing:
                return []
            raise MailError("no_cache", "No local mail cache is available", 4)
        try:
            return self.store.folder_states(self._account())
        except sqlite3.Error as error:
            raise self._mail_error(error) from None

    def _account(self) -> str:
        if self._resolved_account is not None:
            return self._resolved_account
        self._require_cache()
        uri = f"file:{quote(str(self.config.database_path.resolve()), safe='/')}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as database:
                rows = database.execute(
                    "SELECT account FROM folders UNION SELECT account FROM messages ORDER BY account"
                ).fetchall()
        except sqlite3.Error as error:
            raise self._mail_error(error) from None
        if not rows:
            raise MailError("no_cache", "No local mail cache is available", 4)
        if len(rows) != 1:
            raise MailError("account_required", "Select an account for the local mail cache", 2)
        self._resolved_account = str(rows[0][0])
        return self._resolved_account

    def _require_cache(self) -> None:
        if not self.store.exists():
            raise MailError("no_cache", "No local mail cache is available", 4)

    def _attachment_target(
        self,
        folder: str,
        uidvalidity: int,
        uid: int,
        filename: str,
    ) -> _AttachmentTarget:
        target_dir, directory = self._attachment_directory(folder, uidvalidity, uid)
        safe_name = sanitize_filename(filename)
        source = Path(safe_name)
        index = 0
        target: _AttachmentTarget | None = None
        try:
            while True:
                name = safe_name if index == 0 else f"{source.stem} ({index}){source.suffix}"
                reservation_name = f".{name}.astra-download"
                target = _AttachmentTarget(target_dir / name, directory, reservation_name)
                try:
                    descriptor = self._open_target_file(
                        target,
                        reservation_name,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                    )
                except FileExistsError:
                    index += 1
                    continue
                try:
                    os.close(descriptor)
                    if self._target_entry_exists(target, name):
                        self._unlink_target_entry(target, reservation_name)
                        index += 1
                        continue
                except BaseException:
                    try:
                        self._release_attachment_target(target)
                    except OSError:
                        pass
                    raise
                return target
        except BaseException:
            if target is None or not target.released:
                self._attachment_storage.close_directory(directory)
            raise

    def _attachment_directory(
        self,
        folder: str,
        uidvalidity: int,
        uid: int,
    ) -> tuple[Path, AttachmentDirectory]:
        root = self.config.attachments_dir
        components = (sanitize_filename(folder), str(uidvalidity), str(uid))
        target_dir = root.joinpath(*components)
        return target_dir, self._attachment_storage.open_directory(root, components)

    @staticmethod
    def _reservation_path(target: _AttachmentTarget | Path) -> Path:
        if isinstance(target, _AttachmentTarget):
            return target.path.with_name(target.reservation_name)
        return target.with_name(f".{target.name}.astra-download")

    def _open_target_file(
        self,
        target: _AttachmentTarget,
        name: str,
        flags: int,
        mode: int,
    ) -> int:
        return self._attachment_storage.open_file(target.directory, name, flags, mode)

    def _target_entry_exists(self, target: _AttachmentTarget, name: str) -> bool:
        return self._attachment_storage.entry_exists(target.directory, name)

    def _unlink_target_entry(self, target: _AttachmentTarget, name: str) -> None:
        self._attachment_storage.unlink(target.directory, name)

    def _release_attachment_target(
        self,
        target: _AttachmentTarget,
        *,
        remove_file: bool = False,
        temporary_name: str | None = None,
    ) -> None:
        if target.released:
            return
        failures: list[OSError] = []
        names = [name for name in (temporary_name, target.name if remove_file else None) if name is not None]
        names.append(target.reservation_name)
        for name in names:
            try:
                self._unlink_target_entry(target, name)
            except OSError as error:
                failures.append(error)
        try:
            self._attachment_storage.close_directory(target.directory)
        except OSError as error:
            failures.append(error)
        finally:
            target.released = True
        if failures:
            raise failures[0]

    def _atomic_write(self, target: _AttachmentTarget, payload: bytes) -> None:
        temporary_name: str | None = None
        replaced = False
        try:
            while True:
                temporary_name = f".{target.name}.{secrets.token_hex(8)}.tmp"
                try:
                    descriptor = self._open_target_file(
                        target,
                        temporary_name,
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o600,
                    )
                except FileExistsError:
                    continue
                break
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                self._attachment_storage.secure_stream(stream.fileno())
                os.fsync(stream.fileno())
            target_name = target.name
            self._attachment_storage.publish(target.directory, temporary_name, target_name)
            replaced = True
            self._unlink_target_entry(target, temporary_name)
            temporary_name = None
            target.path = self._attachment_storage.current_path(target.directory) / target_name
        except BaseException:
            try:
                self._release_attachment_target(
                    target,
                    remove_file=replaced,
                    temporary_name=temporary_name,
                )
            except OSError:
                pass
            raise
        self._release_attachment_target(target)

    def _mail_error(self, error: Exception) -> MailError:
        if isinstance(error, MailError):
            safe = self._redact(error.safe_message)
            return MailError(error.code, safe, error.exit_code)
        if isinstance(error, sqlite3.Error):
            return MailError("storage_error", "Local mail cache operation failed", 5)
        if isinstance(error, MailTlsError):
            return MailError("tls_error", f"TLS connection to {self.config.host} failed", 4)
        if isinstance(error, MailUnsafeLoginError):
            return MailError("unsafe_login", "The mail server rejected the login as unsafe", 4)
        if isinstance(error, MailAuthenticationError):
            return MailError("auth_failed", "Mail login failed", 4)
        if isinstance(error, MailNetworkError):
            return MailError("network_error", f"Unable to reach {self.config.host}", 4)
        if isinstance(error, OSError):
            return MailError("network_error", f"Unable to reach {self.config.host}", 4)
        if isinstance(error, MailProtocolError):
            description = self._redact(str(error)).casefold()
            if "login" in description or "auth" in description:
                return MailError("auth_failed", "Mail login failed", 4)
            if "connection" in description:
                return MailError("network_error", f"Unable to reach {self.config.host}", 4)
            if "select" in description or "folder" in description or "mailbox" in description:
                return MailError("folder_unavailable", "Mailbox folder is unavailable", 4)
            return MailError("protocol_error", "The mail server response could not be processed", 4)
        return MailError("sync_failed", "Mail synchronization failed", 4)

    def _redact(self, value: str) -> str:
        redacted = value
        for secret in (self.config.auth_code,):
            if secret:
                redacted = redacted.replace(secret, "[redacted]")
        return redacted

    def _canonical_folders(self, folders: Sequence[str] | None) -> tuple[str, ...]:
        requested = self.config.default_folders if folders is None else folders
        return tuple(dict.fromkeys(resolve_folder(folder, self.config.sent_folder) for folder in requested))

    @staticmethod
    def _report_status(results: Sequence[FolderSyncResult]) -> Literal["ok", "partial", "failed"]:
        successful = sum(result.status == "ok" for result in results)
        if successful == len(results):
            return "ok"
        if successful:
            return "partial"
        return "failed"

    @staticmethod
    def _report_errors(report: SyncReport) -> list[dict[str, str]]:
        return [
            {
                "folder": result.folder,
                "code": result.error_code,
                "message": result.error_message,
            }
            for result in report.folders
            if result.status == "failed"
        ]

    @staticmethod
    def _last_success(states: Sequence[dict[str, object]]) -> float | None:
        values = [value for state in states if isinstance((value := state.get("last_success")), (int, float))]
        return max(values, default=None)

    @staticmethod
    def _stored_errors(states: Sequence[dict[str, object]]) -> list[dict[str, str]]:
        return [
            {
                "folder": str(state["folder"]),
                "code": str(state["last_error_code"]),
                "message": str(state["last_error_message"]),
            }
            for state in states
            if state.get("last_error_code")
        ]

    @staticmethod
    def _merge_folder_states(
        remote_folders: Sequence[str],
        local_states: Sequence[dict[str, object]],
        sent_folder: str = "已发送",
    ) -> list[dict[str, object]]:
        local_by_folder = {str(state["folder"]): state for state in local_states}
        merged: list[dict[str, object]] = []
        seen: set[str] = set()
        for folder in remote_folders:
            canonical = resolve_folder(folder, sent_folder)
            if canonical in seen:
                continue
            seen.add(canonical)
            state = local_by_folder.get(canonical)
            merged.append(
                {
                    "folder": canonical,
                    "uidvalidity": None if state is None else state["uidvalidity"],
                    "last_success": None if state is None else state["last_success"],
                    "last_error_code": "" if state is None else state["last_error_code"],
                    "last_error_message": "" if state is None else state["last_error_message"],
                }
            )
        for state in local_states:
            folder = str(state["folder"])
            if folder not in seen:
                merged.append(dict(state))
        return merged
