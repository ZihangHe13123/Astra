#!/usr/bin/env python3
"""Read-only 163 mail synchronization and cache query CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, NoReturn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.mail163.config import MailConfigurationError, load_mail_config, resolve_folder
from agent.mail163.models import SyncReport
from agent.mail163.service import MailError, MailService

_COMMANDS = {"sync", "recent", "search", "read", "folders", "attachment", "status"}
_METADATA_KEYS = {"sync_status", "cache_used", "last_success", "errors"}
_CLI_FOLDER_ALIASES = {
    "inbox": "INBOX",
    "收件箱": "INBOX",
    "sent": "已发送",
    "已发送": "已发送",
    "drafts": "草稿箱",
    "草稿箱": "草稿箱",
    "trash": "已删除",
    "已删除": "已删除",
    "deleted": "已删除",
    "junk": "垃圾邮件",
    "垃圾邮件": "垃圾邮件",
    "spam": "垃圾邮件",
}


class _UsageError(ValueError):
    """Raised instead of letting argparse terminate before JSON rendering."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _add_output_options(parser: argparse.ArgumentParser, *, child: bool = False) -> None:
    default: bool | str = argparse.SUPPRESS if child else False
    parser.add_argument("--offline", action="store_true", default=default, help="query only the existing local cache")
    parser.add_argument("--json", action="store_true", default=default, help="emit a stable JSON envelope")
    parser.add_argument("--account", default=argparse.SUPPRESS if child else "",
                        help="the mailbox, by name (main, or one in ASTRA_MAIL_ACCOUNTS) or address; default the first")


def build_parser() -> argparse.ArgumentParser:
    """Build the primary subcommand parser."""
    parser = _ArgumentParser(
        prog="checkmail",
        description="Synchronize and query a read-only local cache of a configured 163 mailbox.",
    )
    _add_output_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="synchronize Inbox and Sent, or explicit folders")
    _add_output_options(sync_parser, child=True)
    sync_parser.add_argument("--folder", action="append", dest="folders", help="folder or stable alias")

    recent_parser = subparsers.add_parser("recent", help="show recent cached messages after refresh")
    _add_output_options(recent_parser, child=True)
    recent_parser.add_argument("--folder", default="INBOX", help="folder or stable alias")
    recent_parser.add_argument("--recent", "--limit", "-r", dest="limit", type=_positive_int, default=30)
    recent_parser.add_argument("--include-removed", action="store_true")

    search_parser = subparsers.add_parser("search", help="search cached sender, subject, and body text")
    _add_output_options(search_parser, child=True)
    search_parser.add_argument("query", nargs="?", default="", help="text to find")
    search_parser.add_argument("--search", dest="query_option", help=argparse.SUPPRESS)
    search_parser.add_argument("--from", "-f", dest="sender", default="", help="sender filter")
    search_parser.add_argument("--folder", help="folder or stable alias; default searches all cached folders")
    search_parser.add_argument("--recent", "--limit", "-r", dest="limit", type=_positive_int, default=30)
    search_parser.add_argument(
        "--window",
        type=_nonnegative_int,
        default=0,
        help="newest cached candidate count before filtering; 0 searches the complete cache",
    )
    search_parser.add_argument("--include-removed", action="store_true")

    read_parser = subparsers.add_parser("read", help="read a cached message by stable UID")
    _add_output_options(read_parser, child=True)
    read_parser.add_argument("uid", type=_positive_int, help="IMAP UID, never a sequence number")
    read_parser.add_argument("--folder", default="INBOX", help="folder or stable alias")
    read_parser.add_argument("--uidvalidity", type=_positive_int, help="explicit UIDVALIDITY generation")

    folders_parser = subparsers.add_parser("folders", help="list remote folders and local sync state")
    _add_output_options(folders_parser, child=True)

    attachment_parser = subparsers.add_parser("attachment", help="explicitly download one known attachment")
    _add_output_options(attachment_parser, child=True)
    attachment_parser.add_argument("--folder", required=True, help="folder or stable alias")
    attachment_parser.add_argument("--uidvalidity", required=True, type=_positive_int)
    attachment_parser.add_argument("--uid", required=True, type=_positive_int)
    attachment_parser.add_argument("--part", required=True, dest="part_id", help="BODYSTRUCTURE part identifier")

    status_parser = subparsers.add_parser("status", help="show local cache synchronization state")
    _add_output_options(status_parser, child=True)
    return parser


def _legacy_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="checkmail")
    parser.add_argument("--search", "-s")
    parser.add_argument("--from", "-f", dest="sender")
    parser.add_argument("--recent", "-r", type=_positive_int, default=30)
    parser.add_argument("--read", "-R", type=_positive_int)
    parser.add_argument("--folder", default="INBOX")
    parser.add_argument("--folders", action="store_true")
    parser.add_argument("--window", type=_nonnegative_int, default=0)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--account", default="")
    return parser


def _leading(argv: Sequence[str]) -> list[str]:
    """The arguments after the options that may come before a command (--json, --offline, --account)."""
    values, index = list(argv), 0
    while index < len(values):
        if values[index] in {"--json", "--offline"} or values[index].startswith("--account="):
            index += 1
        elif values[index] == "--account":
            index += 2
        else:
            break
    return values[index:]


def _uses_primary_command(argv: Sequence[str]) -> bool:
    rest = _leading(argv)
    return bool(rest) and rest[0] in _COMMANDS


def _translate_legacy(argv: Sequence[str]) -> list[str]:
    values = list(argv)
    primary_help = any(value in {"-h", "--help"} for value in values) and all(
        value in {"-h", "--help", "--json", "--offline"} for value in values
    )
    if primary_help or _uses_primary_command(values):
        return values

    legacy = _legacy_parser().parse_args(values)
    output_flags = [flag for flag, enabled in (("--offline", legacy.offline), ("--json", legacy.json)) if enabled]
    if legacy.account:
        output_flags += ["--account", legacy.account]
    if legacy.folders:
        return ["folders", *output_flags]
    if legacy.read is not None:
        return ["read", str(legacy.read), "--folder", legacy.folder, *output_flags]
    if legacy.search is not None or legacy.sender is not None:
        translated = [
            "search",
            legacy.search or "",
            "--folder",
            legacy.folder,
            "--recent",
            str(legacy.recent),
            "--window",
            str(legacy.window),
        ]
        if legacy.sender is not None:
            translated.extend(("--from", legacy.sender))
        return [*translated, *output_flags]
    return ["recent", "--folder", legacy.folder, "--recent", str(legacy.recent), *output_flags]


def _requested_command(argv: Sequence[str]) -> str:
    rest = _leading(argv)
    if rest and rest[0] in _COMMANDS:
        return rest[0]
    if "--folders" in argv:
        return "folders"
    if _has_option(argv, "--read", "-R"):
        return "read"
    if _has_option(argv, "--search", "-s", "--from", "-f"):
        return "search"
    if not argv or _has_option(argv, "--folder", "--recent", "-r", "--window"):
        return "recent"
    return "unknown"


def _has_option(argv: Sequence[str], *options: str) -> bool:
    for value in argv:
        for option in options:
            if value == option:
                return True
            if option.startswith("--") and value.startswith(f"{option}="):
                return True
            if not option.startswith("--") and value.startswith(option) and len(value) > len(option):
                return True
    return False


def _resolve_cli_folder(value: str) -> str:
    legacy = _CLI_FOLDER_ALIASES.get(value.strip().casefold(), value)
    return resolve_folder(legacy)


def create_service(offline: bool = False, account: str = "") -> MailService:
    """Create the service of one mailbox (the first unless named) without loading credentials for offline queries."""
    config = load_mail_config(PROJECT_ROOT, require_credentials=not offline, account=account)
    return MailService(config)


def _execute(args: argparse.Namespace, service: Any) -> SyncReport | dict[str, object]:
    if args.command == "sync":
        folders = None if args.folders is None else [_resolve_cli_folder(folder) for folder in args.folders]
        return service.sync(folders)
    if args.command == "recent":
        kwargs: dict[str, object] = {
            "folder": _resolve_cli_folder(args.folder),
            "limit": args.limit,
            "refresh": not args.offline,
        }
        if args.include_removed:
            kwargs["include_removed"] = True
        return service.recent(**kwargs)
    if args.command == "search":
        query = args.query_option if args.query_option is not None else args.query
        kwargs = {
            "sender": args.sender,
            "folder": None if args.folder is None else _resolve_cli_folder(args.folder),
            "limit": args.limit,
            "window": args.window,
            "refresh": not args.offline,
        }
        if args.include_removed:
            kwargs["include_removed"] = True
        return service.search(query, **kwargs)
    if args.command == "read":
        return service.read(
            _resolve_cli_folder(args.folder),
            args.uid,
            uidvalidity=args.uidvalidity,
            refresh=not args.offline,
        )
    if args.command == "folders":
        return service.folders(refresh=not args.offline)
    if args.command == "status":
        return service.status()
    return service.download_attachment(
        _resolve_cli_folder(args.folder),
        args.uidvalidity,
        args.uid,
        args.part_id,
    )


def _json_value(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _redact(value: str) -> str:
    secret = os.environ.get("ASTRA_163_AUTH_CODE", "")
    return value.replace(secret, "[redacted]") if secret else value


def _safe_errors(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    safe: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        error = {str(key): _json_value(detail) for key, detail in item.items() if key != "message"}
        error["message"] = _redact(str(item.get("message", "Mail operation failed")))
        safe.append(error)
    return safe


def _query_envelope(command: str, result: Mapping[str, object]) -> tuple[dict[str, object], int]:
    sync_status = str(result.get("sync_status", "ok"))
    cache_used = bool(result.get("cache_used", False))
    fallback = cache_used and sync_status in {"failed", "partial"}
    data = {key: _json_value(value) for key, value in result.items() if key not in _METADATA_KEYS}
    envelope: dict[str, object] = {
        "ok": not fallback,
        "command": command,
        "sync_status": sync_status,
        "cache_used": cache_used,
        "last_success": _json_value(result.get("last_success")),
        "data": data,
        "errors": _safe_errors(result.get("errors", [])),
    }
    return envelope, 3 if fallback else 0


def _sync_envelope(report: SyncReport) -> tuple[dict[str, object], int]:
    folders = [asdict(folder) for folder in report.folders]
    errors = [
        {"folder": folder.folder, "code": folder.error_code, "message": _redact(folder.error_message)}
        for folder in report.folders
        if folder.status == "failed"
    ]
    successes = [folder.synced_at for folder in report.folders if folder.synced_at is not None]
    envelope: dict[str, object] = {
        "ok": report.status == "ok",
        "command": "sync",
        "sync_status": report.status,
        "cache_used": False,
        "last_success": max(successes, default=None),
        "data": {"folders": _json_value(folders)},
        "errors": errors,
    }
    return envelope, 0 if report.status == "ok" else 4


def _error_envelope(command: str, error: MailError) -> dict[str, object]:
    return {
        "ok": False,
        "command": command,
        "sync_status": "failed",
        "cache_used": False,
        "last_success": None,
        "data": {},
        "errors": [{"code": error.code, "message": _redact(error.safe_message)}],
    }


def _emit(envelope: Mapping[str, object], *, json_mode: bool, error: bool = False) -> None:
    if json_mode:
        print(json.dumps(_json_value(envelope), ensure_ascii=False, indent=2))
        return
    if error:
        details = envelope.get("errors", [])
        if isinstance(details, list) and details and isinstance(details[0], Mapping):
            print(
                f"Error [{details[0].get('code', 'error')}]: {details[0].get('message', 'Mail operation failed')}",
                file=sys.stderr,
            )
        else:
            print("Error: Mail operation failed", file=sys.stderr)
        return
    _print_human(envelope)


def _print_human(envelope: Mapping[str, object]) -> None:
    print(f"Sync status: {envelope['sync_status']}")
    cache_used = bool(envelope["cache_used"])
    cache_label = "fallback" if cache_used and envelope["sync_status"] != "offline" else "local" if cache_used else "fresh"
    print(f"Cache: {cache_label}")
    if envelope.get("last_success") is not None:
        print(f"Last successful sync: {envelope['last_success']}")
    errors = envelope.get("errors", [])
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, Mapping):
                print(f"Warning [{error.get('code', 'error')}]: {error.get('message', 'Mail operation failed')}")

    data = envelope.get("data", {})
    if not isinstance(data, Mapping):
        return
    command = str(envelope["command"])
    if command == "sync":
        _print_sync_rows(data.get("folders", []))
    elif command in {"recent", "search"}:
        _print_message_rows(data.get("items", []))
    elif command == "read":
        _print_message_rows([data.get("item", {})])
    elif command in {"folders", "status"}:
        _print_folder_rows(data.get("items", []))
    elif command == "attachment" and data.get("path"):
        print(f"Downloaded attachment: {data['path']}")


def _print_sync_rows(rows: object) -> None:
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        print(
            f"Folder {row.get('folder', '')}: {row.get('status', '')}; "
            f"fetched={row.get('fetched', 0)} flags={row.get('updated_flags', 0)} "
            f"remote_removed={row.get('remote_removed', 0)}"
        )


def _print_message_rows(rows: object) -> None:
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        identity = " ".join(
            f"{key}={row[key]}" for key in ("folder", "uidvalidity", "uid") if row.get(key) is not None
        )
        labels = [
            label
            for key, label in (
                ("unread", "UNREAD"),
                ("remote_removed", "REMOTE_REMOVED"),
                ("body_truncated", "BODY_TRUNCATED"),
            )
            if row.get(key) is True
        ]
        suffix = f" [{' '.join(labels)}]" if labels else ""
        print(f"[{identity}]{suffix}")
        if row.get("sender"):
            print(f"  From: {row['sender']}")
        print(f"  Subject: {row.get('subject') or '(no subject)'}")
        if row.get("sent_at"):
            print(f"  Date: {row['sent_at']}")
        if row.get("body_text"):
            print(f"  Body: {row['body_text']}")
        attachments = row.get("attachments", [])
        if isinstance(attachments, list):
            for attachment in attachments:
                if isinstance(attachment, Mapping):
                    print(
                        f"  Attachment: part={attachment.get('part_id', '')} "
                        f"filename={attachment.get('filename', '')}"
                    )


def _print_folder_rows(rows: object) -> None:
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        details = [f"folder={row.get('folder', '')}"]
        for key in ("uidvalidity", "last_success", "last_error_code"):
            if row.get(key) not in (None, ""):
                details.append(f"{key}={row[key]}")
        print(" ".join(details))


def main(argv: Sequence[str] | None = None) -> int:
    """Run one mail command and return its stable process exit code."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(_translate_legacy(raw_argv))
    except _UsageError:
        failure = MailError("usage_error", "Invalid mail command arguments", 2)
        if "--json" not in raw_argv:
            parser.print_usage(file=sys.stderr)
        _emit(
            _error_envelope(_requested_command(raw_argv), failure),
            json_mode="--json" in raw_argv,
            error=True,
        )
        return failure.exit_code
    except SystemExit as error:
        return int(error.code or 0)

    if args.offline and args.command in {"sync", "attachment"}:
        error = MailError(
            "offline_not_supported",
            "This command requires a read-only 163 server connection",
            2,
        )
        _emit(_error_envelope(args.command, error), json_mode=args.json, error=True)
        return error.exit_code

    try:
        service = create_service(offline=bool(args.offline) or args.command == "status", account=args.account)
        result = _execute(args, service)
        envelope, exit_code = (
            _sync_envelope(result) if isinstance(result, SyncReport) else _query_envelope(args.command, result)
        )
        error_only = False
    except MailConfigurationError as error:
        failure = MailError("configuration_error", _redact(str(error)), 2)
        envelope, exit_code = _error_envelope(args.command, failure), failure.exit_code
        error_only = True
    except MailError as error:
        failure = MailError(error.code, _redact(error.safe_message), error.exit_code)
        envelope, exit_code = _error_envelope(args.command, failure), failure.exit_code
        error_only = True
    except Exception:  # noqa: BLE001 - command boundary never exposes raw exception details
        failure = MailError("local_error", "Local mail command failed", 5)
        envelope, exit_code = _error_envelope(args.command, failure), failure.exit_code
        error_only = True

    _emit(envelope, json_mode=args.json, error=error_only)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
