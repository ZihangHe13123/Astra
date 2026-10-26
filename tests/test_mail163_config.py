from pathlib import Path

import pytest

from agent.mail163.config import (
    MailConfigurationError,
    decode_mailbox_name,
    encode_mailbox_name,
    load_mail_config,
    resolve_folder,
)


def test_process_environment_overrides_project_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "ASTRA_163_EMAIL=file@163.com\nASTRA_163_AUTH_CODE=file-secret\n",
        encoding="utf-8",
    )
    config = load_mail_config(
        tmp_path,
        environ={"ASTRA_163_EMAIL": "env@163.com", "ASTRA_163_AUTH_CODE": "env-secret"},
    )
    assert config.account == "env@163.com"
    assert config.auth_code == "env-secret"
    assert config.database_path == tmp_path / ".astra" / "mail" / "163.sqlite3"
    assert config.default_folders == ("INBOX", "已发送")
    assert config.max_body_bytes == 5 * 1024 * 1024


def test_missing_credentials_does_not_create_local_state(tmp_path: Path) -> None:
    with pytest.raises(MailConfigurationError, match="ASTRA_163_EMAIL"):
        load_mail_config(tmp_path, environ={})
    assert not (tmp_path / ".astra").exists()


@pytest.mark.parametrize(
    ("source", "encoded"),
    [("INBOX", b"INBOX"), ("A&B", b"A&-B"), ("已发送", b"&XfJT0ZAB-")],
)
def test_modified_utf7_round_trip(source: str, encoded: bytes) -> None:
    assert encode_mailbox_name(source) == encoded
    assert decode_mailbox_name(encoded) == source


def test_folder_aliases_are_stable() -> None:
    assert resolve_folder("sent") == "已发送"
    assert resolve_folder("收件箱") == "INBOX"


@pytest.mark.parametrize(("address", "host", "sent", "send_id"), [
    ("someone@gmail.com", "imap.gmail.com", "[Gmail]/Sent Mail", False),
    ("someone@qq.com", "imap.qq.com", "Sent Messages", False),
    ("someone@icloud.com", "imap.mail.me.com", "Sent Messages", False),
    ("someone@126.com", "imap.126.com", "已发送", True),
])
def test_known_providers_follow_from_the_address(tmp_path: Path, address, host, sent, send_id) -> None:
    config = load_mail_config(tmp_path, environ={"ASTRA_MAIL_ADDRESS": address, "ASTRA_MAIL_PASSWORD": "app-password"})
    assert (config.host, config.sent_folder, config.send_id, config.port) == (host, sent, send_id, 993)
    assert config.default_folders == ("INBOX", sent)
    assert config.database_path == tmp_path / ".astra" / "mail" / f"{host}.sqlite3"
    assert resolve_folder("sent", config.sent_folder) == sent and resolve_folder("收件箱", config.sent_folder) == "INBOX"


def test_another_provider_needs_its_host_and_may_name_its_sent_folder(tmp_path: Path) -> None:
    with pytest.raises(MailConfigurationError, match="ASTRA_MAIL_IMAP_HOST"):
        load_mail_config(tmp_path, environ={"ASTRA_MAIL_ADDRESS": "me@example.org", "ASTRA_MAIL_PASSWORD": "x"})
    config = load_mail_config(tmp_path, environ={
        "ASTRA_MAIL_ADDRESS": "me@example.org", "ASTRA_MAIL_PASSWORD": "x", "ASTRA_MAIL_IMAP_HOST": "mail.example.org",
        "ASTRA_MAIL_IMAP_PORT": "1993", "ASTRA_MAIL_SENT_FOLDER": "Sent Items"})
    assert (config.host, config.port, config.sent_folder, config.send_id) == ("mail.example.org", 1993, "Sent Items", False)
    # The newer names win over the 163 ones when both are set.
    both = load_mail_config(tmp_path, environ={"ASTRA_MAIL_ADDRESS": "me@gmail.com", "ASTRA_MAIL_PASSWORD": "x",
                                               "ASTRA_163_EMAIL": "old@163.com", "ASTRA_163_AUTH_CODE": "y"})
    assert both.account == "me@gmail.com" and both.host == "imap.gmail.com"
