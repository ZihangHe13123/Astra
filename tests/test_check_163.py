from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent.mail163.config import MailConfigurationError
from agent.mail163.models import FolderSyncResult, SyncReport
from agent.mail163.service import MailError
from scripts import check_163

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeService:
    def __init__(self, **results: object) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, object]]] = []

    def sync(self, folders: list[str] | None = None) -> SyncReport:
        self.calls.append(("sync", {"folders": folders}))
        result = self.results.get("sync")
        if isinstance(result, Exception):
            raise result
        if isinstance(result, SyncReport):
            return result
        return SyncReport(
            status="ok",
            folders=(
                FolderSyncResult("INBOX", "ok", 1, 0, 0, 100.0, "", ""),
            ),
        )

    def recent(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("recent", kwargs))
        return self._dict_result("recent")

    def search(self, query: str = "", **kwargs: object) -> dict[str, object]:
        self.calls.append(("search", {"query": query, **kwargs}))
        return self._dict_result("search")

    def read(self, folder: str, uid: int, **kwargs: object) -> dict[str, object]:
        self.calls.append(("read", {"folder": folder, "uid": uid, **kwargs}))
        return self._dict_result("read")

    def folders(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(("folders", kwargs))
        return self._dict_result("folders")

    def status(self) -> dict[str, object]:
        self.calls.append(("status", {}))
        return self._dict_result("status")

    def download_attachment(
        self,
        folder: str,
        uidvalidity: int,
        uid: int,
        part_id: str,
    ) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "folder": folder,
            "uidvalidity": uidvalidity,
            "uid": uid,
            "part_id": part_id,
        }
        self.calls.append(("attachment", kwargs))
        return self._dict_result("attachment")

    def _dict_result(self, command: str) -> dict[str, object]:
        result = self.results.get(command)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, dict):
            return result
        if command == "read":
            return {"item": {"uid": 352, "subject": "Hello"}, "sync_status": "ok"}
        return {"items": [], "sync_status": "ok"}


def patch_service(monkeypatch: pytest.MonkeyPatch, service: FakeService) -> list[bool]:
    offline_calls: list[bool] = []

    def factory(offline: bool = False, account: str = "") -> FakeService:
        offline_calls.append(offline)
        return service

    monkeypatch.setattr(check_163, "create_service", factory)
    return offline_calls


def json_output(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def test_legacy_recent_argument_refreshes_then_queries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeService(recent={"items": [{"uid": 7, "subject": "Hello"}], "sync_status": "ok"})
    patch_service(monkeypatch, fake)

    assert check_163.main(["--recent", "10"]) == 0

    assert fake.calls == [("recent", {"folder": "INBOX", "limit": 10, "refresh": True})]
    assert "Hello" in capsys.readouterr().out


def test_no_arguments_preserves_default_recent_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeService()
    patch_service(monkeypatch, fake)

    assert check_163.main([]) == 0

    assert fake.calls == [("recent", {"folder": "INBOX", "limit": 30, "refresh": True})]


def test_json_cache_fallback_has_stable_nonzero_status(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeService(
        recent={
            "items": [{"uid": 7}],
            "cache_used": True,
            "sync_status": "failed",
            "last_success": 100.0,
            "errors": [{"code": "network_error", "message": "Unable to reach imap.163.com"}],
        }
    )
    patch_service(monkeypatch, fake)

    assert check_163.main(["recent", "--json"]) == 3

    assert json_output(capsys) == {
        "ok": False,
        "command": "recent",
        "sync_status": "failed",
        "cache_used": True,
        "last_success": 100.0,
        "data": {"items": [{"uid": 7}]},
        "errors": [{"code": "network_error", "message": "Unable to reach imap.163.com"}],
    }


def test_legacy_read_number_is_current_generation_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeService()
    patch_service(monkeypatch, fake)

    assert check_163.main(["--read", "352", "--folder", "INBOX"]) == 0

    assert fake.calls == [
        ("read", {"folder": "INBOX", "uid": 352, "uidvalidity": None, "refresh": True})
    ]


def test_legacy_search_preserves_sender_limit_window_and_folder_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeService()
    patch_service(monkeypatch, fake)

    assert (
        check_163.main(
            ["--search", "invoice", "--from", "billing.example.com", "--recent", "8", "--window", "100", "--folder", "sent"]
        )
        == 0
    )

    assert fake.calls == [
        (
            "search",
            {
                "query": "invoice",
                "sender": "billing.example.com",
                "folder": "已发送",
                "limit": 8,
                "window": 100,
                "refresh": True,
            },
        )
    ]


def test_sync_subcommand_accepts_explicit_folder_aliases(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeService()
    patch_service(monkeypatch, fake)

    assert check_163.main(["sync", "--folder", "inbox", "--folder", "sent", "--json"]) == 0

    assert fake.calls == [("sync", {"folders": ["INBOX", "已发送"]})]
    payload = json_output(capsys)
    assert payload["command"] == "sync"
    assert payload["sync_status"] == "ok"
    assert payload["data"]["folders"][0]["folder"] == "INBOX"


@pytest.mark.parametrize(
    ("argv", "expected_call"),
    [
        (
            ["recent", "--folder", "DRAFTS"],
            ("recent", {"folder": "草稿箱", "limit": 30, "refresh": True}),
        ),
        (
            ["search", "project", "--folder", "trash"],
            (
                "search",
                {
                    "query": "project",
                    "sender": "",
                    "folder": "已删除",
                    "limit": 30,
                    "window": 0,
                    "refresh": True,
                },
            ),
        ),
        (
            ["read", "9", "--folder", "deleted"],
            ("read", {"folder": "已删除", "uid": 9, "uidvalidity": None, "refresh": True}),
        ),
        (
            ["sync", "--folder", "junk"],
            ("sync", {"folders": ["垃圾邮件"]}),
        ),
        (
            ["attachment", "--folder", "spam", "--uidvalidity", "77", "--uid", "9", "--part", "2"],
            ("attachment", {"folder": "垃圾邮件", "uidvalidity": 77, "uid": 9, "part_id": "2"}),
        ),
    ],
)
def test_legacy_folder_aliases_route_to_canonical_names_across_commands(
    argv: list[str],
    expected_call: tuple[str, dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeService()
    patch_service(monkeypatch, fake)

    assert check_163.main(argv) == 0

    assert fake.calls == [expected_call]


def test_recent_offline_loads_cache_without_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeService(recent={"items": [], "cache_used": True, "sync_status": "offline"})
    offline_calls = patch_service(monkeypatch, fake)

    assert check_163.main(["recent", "--folder", "sent", "--recent", "4", "--offline"]) == 0

    assert offline_calls == [True]
    assert fake.calls == [("recent", {"folder": "已发送", "limit": 4, "refresh": False})]


def test_search_subcommand_forwards_complete_cache_window(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeService()
    patch_service(monkeypatch, fake)

    assert check_163.main(["search", "project", "--from", "alice", "--window", "0", "--recent", "5"]) == 0

    assert fake.calls == [
        (
            "search",
            {
                "query": "project",
                "sender": "alice",
                "folder": None,
                "limit": 5,
                "window": 0,
                "refresh": True,
            },
        )
    ]


def test_read_subcommand_accepts_explicit_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeService()
    patch_service(monkeypatch, fake)

    assert check_163.main(["read", "9", "--folder", "收件箱", "--uidvalidity", "77"]) == 0

    assert fake.calls == [
        ("read", {"folder": "INBOX", "uid": 9, "uidvalidity": 77, "refresh": True})
    ]


def test_folders_offline_and_status_do_not_require_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeService(
        folders={"items": [], "cache_used": True, "sync_status": "offline"},
        status={"items": [], "cache_used": True, "sync_status": "offline"},
    )
    offline_calls = patch_service(monkeypatch, fake)

    assert check_163.main(["folders", "--offline"]) == 0
    assert check_163.main(["status"]) == 0

    assert offline_calls == [True, True]
    assert fake.calls == [("folders", {"refresh": False}), ("status", {})]


def test_legacy_folders_option_is_still_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeService()
    patch_service(monkeypatch, fake)

    assert check_163.main(["--folders"]) == 0

    assert fake.calls == [("folders", {"refresh": True})]


def test_attachment_is_the_only_command_that_requests_a_download(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    target = tmp_path / "report.pdf"
    fake = FakeService(
        attachment={
            "folder": "INBOX",
            "uidvalidity": 77,
            "uid": 9,
            "part_id": "2",
            "filename": "report.pdf",
            "path": target,
        }
    )
    patch_service(monkeypatch, fake)

    assert (
        check_163.main(
            [
                "attachment",
                "--folder",
                "inbox",
                "--uidvalidity",
                "77",
                "--uid",
                "9",
                "--part",
                "2",
                "--json",
            ]
        )
        == 0
    )

    assert fake.calls == [
        (
            "attachment",
            {"folder": "INBOX", "uidvalidity": 77, "uid": 9, "part_id": "2"},
        )
    ]
    assert json_output(capsys)["data"]["path"] == str(target)


@pytest.mark.parametrize("command", ["sync", "attachment --folder INBOX --uidvalidity 1 --uid 1 --part 2"])
def test_remote_commands_reject_offline_mode_before_service_creation(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(check_163, "create_service", lambda offline=False, account="": calls.append(offline))

    assert check_163.main([*command.split(), "--offline", "--json"]) == 2

    assert calls == []
    payload = json_output(capsys)
    assert payload["errors"] == [
        {"code": "offline_not_supported", "message": "This command requires a read-only 163 server connection"}
    ]


def test_human_output_puts_cache_status_before_stable_message_identifiers_and_labels(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeService(
        recent={
            "items": [
                {
                    "folder": "INBOX",
                    "uidvalidity": 77,
                    "uid": 9,
                    "subject": "Hello",
                    "sender": "sender@example.com",
                    "unread": True,
                    "remote_removed": True,
                    "body_truncated": True,
                }
            ],
            "cache_used": True,
            "sync_status": "failed",
            "last_success": 100.0,
            "errors": [{"code": "network_error", "message": "Network unavailable"}],
        }
    )
    patch_service(monkeypatch, fake)

    assert check_163.main(["recent"]) == 3

    output = capsys.readouterr().out
    assert output.index("Sync status: failed") < output.index("Hello")
    assert "Cache: fallback" in output
    assert "folder=INBOX uidvalidity=77 uid=9" in output
    assert all(label in output for label in ("UNREAD", "REMOTE_REMOVED", "BODY_TRUNCATED"))


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_human_nonzero_sync_report_keeps_all_folder_results_and_errors(
    status: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inbox_status = "ok" if status == "partial" else "failed"
    inbox_error_code = "" if inbox_status == "ok" else "network_error"
    inbox_error_message = "" if inbox_status == "ok" else "Inbox unavailable"
    report = SyncReport(
        status=status,
        folders=(
            FolderSyncResult("INBOX", inbox_status, 2 if inbox_status == "ok" else 0, 1, 0, 100.0, inbox_error_code, inbox_error_message),
            FolderSyncResult("已发送", "failed", 0, 0, 0, None, "folder_unavailable", "Sent unavailable"),
        ),
    )
    fake = FakeService(sync=report)
    patch_service(monkeypatch, fake)

    assert check_163.main(["sync"]) == 4

    captured = capsys.readouterr()
    assert captured.err == ""
    assert f"Sync status: {status}" in captured.out
    assert f"Folder INBOX: {inbox_status}" in captured.out
    assert "Folder 已发送: failed" in captured.out
    assert "Warning [folder_unavailable]: Sent unavailable" in captured.out
    if status == "failed":
        assert "Warning [network_error]: Inbox unavailable" in captured.out


def test_human_raised_command_error_remains_error_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        check_163,
        "create_service",
        lambda offline=False, account="": (_ for _ in ()).throw(MailError("network_error", "Unable to reach server", 4)),
    )

    assert check_163.main(["sync"]) == 4

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Error [network_error]: Unable to reach server\n"


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (MailConfigurationError("ASTRA_163_EMAIL is required"), 2),
        (MailError("no_cache", "No local mail cache is available", 4), 4),
        (MailError("storage_error", "Local mail cache operation failed", 5), 5),
    ],
)
def test_stable_error_exit_codes_and_json_envelope(
    error: Exception,
    expected_code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(check_163, "create_service", lambda offline=False, account="": (_ for _ in ()).throw(error))

    assert check_163.main(["recent", "--json"]) == expected_code

    payload = json_output(capsys)
    assert payload.keys() == {
        "ok",
        "command",
        "sync_status",
        "cache_used",
        "last_success",
        "data",
        "errors",
    }
    assert payload["ok"] is False
    assert payload["command"] == "recent"
    assert payload["errors"][0]["message"]


def test_cli_preserves_unsupported_platform_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    error = MailError(
        "unsupported_platform",
        "Attachment download is unsupported on this platform",
        2,
    )
    patch_service(monkeypatch, FakeService(attachment=error))

    assert (
        check_163.main(
            [
                "attachment",
                "--folder",
                "INBOX",
                "--uidvalidity",
                "10",
                "--uid",
                "2",
                "--part",
                "2",
                "--json",
            ]
        )
        == 2
    )

    payload = json_output(capsys)
    assert payload["errors"] == [
        {
            "code": "unsupported_platform",
            "message": "Attachment download is unsupported on this platform",
        }
    ]


@pytest.mark.parametrize(
    ("error_code", "safe_message"),
    [
        ("tls_error", "TLS connection to imap.163.com failed"),
        ("protocol_error", "163 mail server response could not be processed"),
        ("auth_failed", "163 authentication failed"),
        ("unsafe_login", "163 rejected login as unsafe"),
    ],
)
def test_cli_preserves_stable_remote_error_classification(
    error_code: str,
    safe_message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = SyncReport(
        status="failed",
        folders=(
            FolderSyncResult("INBOX", "failed", 0, 0, 0, None, error_code, safe_message),
        ),
    )
    patch_service(monkeypatch, FakeService(sync=report))

    assert check_163.main(["sync", "--json"]) == 4

    payload = json_output(capsys)
    assert payload["errors"] == [
        {"folder": "INBOX", "code": error_code, "message": safe_message},
    ]


def test_error_output_never_contains_authorization_code(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ASTRA_163_AUTH_CODE", "do-not-print")
    monkeypatch.setattr(
        check_163,
        "create_service",
        lambda offline=False, account="": (_ for _ in ()).throw(
            MailError("auth_failed", "163 authentication failed: do-not-print", 4)
        ),
    )

    assert check_163.main(["sync", "--json"]) == 4

    captured = capsys.readouterr()
    assert "do-not-print" not in captured.out
    assert "do-not-print" not in captured.err


def test_unexpected_exception_is_not_rendered_or_leaked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ASTRA_163_AUTH_CODE", "secret-value")
    monkeypatch.setattr(
        check_163,
        "create_service",
        lambda offline=False, account="": (_ for _ in ()).throw(RuntimeError("raw secret-value traceback detail")),
    )

    assert check_163.main(["status", "--json"]) == 5

    captured = capsys.readouterr()
    assert "secret-value" not in captured.out + captured.err
    assert "traceback" not in captured.out.casefold() + captured.err.casefold()
    assert json.loads(captured.out)["errors"] == [
        {"code": "local_error", "message": "Local mail command failed"}
    ]


def test_offline_missing_cache_does_not_load_credentials_or_create_database(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(check_163, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("ASTRA_163_EMAIL", raising=False)
    monkeypatch.delenv("ASTRA_163_AUTH_CODE", raising=False)

    assert check_163.main(["status", "--offline", "--json"]) == 4

    assert json_output(capsys)["errors"][0]["code"] == "no_cache"
    assert not (tmp_path / ".astra").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ["read"],
        ["attachment", "--folder", "INBOX", "--uid", "1", "--part", "2"],
        ["recent", "--recent", "0"],
        ["search", "query", "--window", "-1"],
        ["unknown"],
    ],
)
def test_usage_errors_return_two(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert check_163.main(argv) == 2
    assert "usage:" in capsys.readouterr().err


def test_json_usage_error_uses_the_stable_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_163.main(["read", "--json"]) == 2
    assert json_output(capsys) == {
        "ok": False,
        "command": "read",
        "sync_status": "failed",
        "cache_used": False,
        "last_success": None,
        "data": {},
        "errors": [{"code": "usage_error", "message": "Invalid mail command arguments"}],
    }


@pytest.mark.parametrize(
    ("argv", "expected_command"),
    [
        (["--folder", "sync", "--unknown", "--json"], "recent"),
        (["--search", "read", "--unknown", "--json"], "search"),
    ],
)
def test_json_usage_command_detection_ignores_option_values(
    argv: list[str],
    expected_command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check_163.main(argv) == 2
    assert json_output(capsys)["command"] == expected_command


@pytest.mark.parametrize(
    ("argument", "expected_command"),
    [
        ("--search=read", "search"),
        ("-sread", "search"),
        ("--from=sync", "search"),
        ("-fsync", "search"),
        ("--read=0", "read"),
        ("-R0", "read"),
        ("--recent=0", "recent"),
        ("-r0", "recent"),
        ("--folder=sync", "recent"),
        ("--window=-1", "recent"),
    ],
)
def test_json_usage_command_detection_supports_attached_option_values(
    argument: str,
    expected_command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check_163.main([argument, "--unknown", "--json"]) == 2
    assert json_output(capsys)["command"] == expected_command


def test_help_is_successful_and_lists_all_primary_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert check_163.main(["--help"]) == 0
    output = capsys.readouterr().out
    for command in ("sync", "recent", "search", "read", "folders", "attachment", "status"):
        assert command in output


@pytest.mark.parametrize("global_options", [["--json"], ["--offline"], ["--offline", "--json"]])
def test_global_options_with_help_show_primary_command_help(
    global_options: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert check_163.main([*global_options, "--help"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    for command in ("sync", "recent", "search", "read", "folders", "attachment", "status"):
        assert command in captured.out


def test_build_parser_exposes_primary_subcommands() -> None:
    parser = check_163.build_parser()
    assert parser.parse_args(["search", "hello", "--folder", "sent", "--offline"]).command == "search"


def test_script_entry_point_imports_the_worktree_package_from_any_cwd(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "check_163.py"), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "sync" in completed.stdout


def test_env_example_documents_only_safe_commented_163_credentials() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    expected = (
        "# Optional read-only 163 mail integration. Use a 163 client authorization code, not the web password.\n"
        "# ASTRA_163_EMAIL=your-account@163.com\n"
        "# ASTRA_163_AUTH_CODE=\n"
        "# ASTRA_163_MAX_BODY_BYTES=5242880"
    )
    assert expected in env_example
    assert "\nASTRA_163_AUTH_CODE=" not in env_example


def test_guide_documents_complete_read_only_163_mail_workflow() -> None:
    readme = (PROJECT_ROOT / "docs" / "integrations.md").read_text(encoding="utf-8")
    section_start = readme.index("## Read-only 163 mail")
    next_section = readme.find("\n## ", section_start + 4)
    section = readme[section_start:] if next_section == -1 else readme[section_start:next_section]

    for value in (
        "ASTRA_163_EMAIL",
        "ASTRA_163_AUTH_CODE",
        "authorization code",
        "scripts\\checkmail.bat",
        "./scripts/checkmail.sh",
        "sync --json",
        "search",
        "read",
        "--offline",
        "attachment",
        ".astra/mail/163.sqlite3",
        ".astra/mail/attachments/",
        "INBOX",
        "已发送",
    ):
        assert value in section
    assert "does not mark messages read, move, delete, reply to, or send mail" in section
    assert "does not download attachment payloads during synchronization" in section
    assert "Explicit attachment download currently fails closed on Windows" in section
    assert ".\\scripts\\checkmail.bat attachment" not in section
    assert "no subcommand" in section


def test_account_picks_one_mailbox_in_every_form_of_the_command(tmp_path, monkeypatch, capsys) -> None:
    """Through the real service: each mailbox's cache holds one message, and --account reads that one's."""
    from test_mail163_service import ClientFactory, ScriptedClient, message

    from agent.mail163.config import load_mail_accounts
    from agent.mail163.models import FolderSnapshot
    from agent.mail163.service import MailService

    (tmp_path / ".env").write_text(
        "ASTRA_MAIL_ADDRESS=me@gmail.com\nASTRA_MAIL_PASSWORD=main-secret\n"
        "ASTRA_MAIL_ACCOUNTS=work\nASTRA_MAIL_WORK_ADDRESS=me@qq.com\nASTRA_MAIL_WORK_PASSWORD=work-secret\n",
        encoding="utf-8")
    for config in load_mail_accounts(tmp_path):
        item = message("INBOX", 7, 4, subject=f"In the {config.name} mailbox")
        client = ScriptedClient(snapshots={"INBOX": FolderSnapshot("INBOX", 7, frozenset({4}), {4: ()}),
                                           config.sent_folder: FolderSnapshot(config.sent_folder, 8, frozenset(), {})},
                                messages={("INBOX", 4): item})
        MailService(config, client_factory=ClientFactory(client)).sync()
    monkeypatch.setattr(check_163, "PROJECT_ROOT", tmp_path)

    def subjects(*argv: str) -> list[str]:
        assert check_163.main(list(argv)) == 0
        return [item["subject"] for item in json_output(capsys)["data"]["items"]]

    assert subjects("recent", "--offline", "--json") == ["In the main mailbox"]
    assert subjects("recent", "--offline", "--json", "--account", "work") == ["In the work mailbox"]
    assert subjects("--account", "work", "--offline", "search", "mailbox", "--json") == ["In the work mailbox"]
    assert subjects("--account=me@qq.com", "--offline", "--json") == ["In the work mailbox"]  # The older form.
    assert subjects("--search", "mailbox", "--account", "work", "--offline", "--json") == ["In the work mailbox"]
    assert check_163.main(["recent", "--offline", "--json", "--account", "school"]) == 2
    failure = json_output(capsys)
    assert failure["errors"][0]["code"] == "configuration_error" and "main, work" in failure["errors"][0]["message"]


@pytest.mark.parametrize(("default_settings", "bad_setting"), [
    ("ASTRA_MAIL_ADDRESS=default@gmail.com\nASTRA_MAIL_ACCOUNTS=work\n", "ASTRA_MAIL_IMAP_PORT"),
    ("ASTRA_MAIL_ACCOUNTS=first,work\nASTRA_MAIL_FIRST_ADDRESS=default@gmail.com\n", "ASTRA_MAIL_FIRST_IMAP_PORT"),
])
def test_invalid_default_fails_without_reading_the_next_mailbox(tmp_path, monkeypatch, capsys, default_settings, bad_setting):
    from agent.mail163.config import load_mail_config
    from agent.mail163.store import MailStore
    from test_mail163_service import message

    (tmp_path / ".env").write_text(default_settings + f"{bad_setting}=invalid\nASTRA_MAIL_WORK_ADDRESS=me@qq.com\n",
                                   encoding="utf-8")
    work = load_mail_config(tmp_path, account="work", require_credentials=False)
    receipt = message("INBOX", 7, 4, subject="Only in work")
    MailStore(work.database_path).apply_folder_sync(work.address, "INBOX", 7, {4}, [receipt], {4: ()}, 100)
    monkeypatch.setattr(check_163, "PROJECT_ROOT", tmp_path)
    args = ["recent", "--offline", "--json"]
    assert check_163.main(args) == 2
    failure = json_output(capsys)
    assert failure["errors"] == [{"code": "configuration_error", "message": f"{bad_setting} must be a positive integer"}]
    assert "Only in work" not in str(failure)
    assert check_163.main([*args, "--account", "me@qq.com"]) == 0
    assert [item["subject"] for item in json_output(capsys)["data"]["items"]] == ["Only in work"]
