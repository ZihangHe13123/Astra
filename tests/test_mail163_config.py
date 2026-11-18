from pathlib import Path

import pytest

from agent.mail163.config import (
    MailConfigurationError,
    decode_mailbox_name,
    encode_mailbox_name,
    load_mail_accounts,
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


def test_more_mailboxes_are_named_in_the_list_each_with_its_own_settings_and_cache(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "ASTRA_MAIL_ADDRESS=me@gmail.com\nASTRA_MAIL_PASSWORD=main-secret\n"
        "ASTRA_MAIL_ACCOUNTS=Work, qq\n"
        "ASTRA_MAIL_WORK_ADDRESS=me@example.org\nASTRA_MAIL_WORK_PASSWORD=file-secret\n"
        "ASTRA_MAIL_WORK_IMAP_HOST=mail.example.org\nASTRA_MAIL_WORK_SENT_FOLDER=Sent Items\n",
        encoding="utf-8",
    )
    # The process environment still wins over the file, for a named mailbox too.
    configs = load_mail_accounts(tmp_path, environ={
        "ASTRA_MAIL_WORK_PASSWORD": "env-secret", "ASTRA_MAIL_QQ_ADDRESS": "123@qq.com",
        "ASTRA_MAIL_QQ_PASSWORD": "qq-secret"})
    assert [(c.name, c.account, c.auth_code, c.host, c.sent_folder) for c in configs] == [
        ("main", "me@gmail.com", "main-secret", "imap.gmail.com", "[Gmail]/Sent Mail"),
        ("work", "me@example.org", "env-secret", "mail.example.org", "Sent Items"),
        ("qq", "123@qq.com", "qq-secret", "imap.qq.com", "Sent Messages"),
    ]
    mail_dir = tmp_path / ".astra" / "mail"
    # main keeps the cache it always had; the others have their own, so no two share a cache or attachments.
    assert configs[0].database_path == mail_dir / "imap.gmail.com.sqlite3"
    assert configs[0].attachments_dir == mail_dir / "attachments"
    assert [(c.database_path, c.attachments_dir) for c in configs[1:]] == [
        (mail_dir / "accounts" / "work" / "mail.sqlite3", mail_dir / "accounts" / "work" / "attachments"),
        (mail_dir / "accounts" / "qq" / "mail.sqlite3", mail_dir / "accounts" / "qq" / "attachments"),
    ]
    # One mailbox by its name or its address; without either, the first.
    environ = {"ASTRA_MAIL_WORK_PASSWORD": "x", "ASTRA_MAIL_QQ_ADDRESS": "123@qq.com", "ASTRA_MAIL_QQ_PASSWORD": "y"}
    assert load_mail_config(tmp_path, environ=environ, account="QQ").account == "123@qq.com"
    assert load_mail_config(tmp_path, environ=environ, account="ME@example.org").name == "work"
    assert load_mail_config(tmp_path, environ=environ).name == "main"
    with pytest.raises(MailConfigurationError, match="main, work, qq"):
        load_mail_config(tmp_path, environ=environ, account="school")
    assert not (tmp_path / ".astra").exists()


@pytest.mark.parametrize(("settings", "message"), [
    ({"ASTRA_MAIL_ACCOUNTS": "work-1"}, "not a mailbox name"),
    ({"ASTRA_MAIL_ACCOUNTS": "main"}, "main is the mailbox of ASTRA_MAIL_ADDRESS"),
    ({"ASTRA_MAIL_ACCOUNTS": "work,WORK"}, "names work twice"),
    ({"ASTRA_MAIL_ACCOUNTS": "work"}, "ASTRA_MAIL_WORK_ADDRESS is required"),
    ({"ASTRA_MAIL_ACCOUNTS": "work", "ASTRA_MAIL_WORK_ADDRESS": "me@qq.com"}, "ASTRA_MAIL_WORK_PASSWORD is required"),
    ({"ASTRA_MAIL_ACCOUNTS": "work", "ASTRA_MAIL_WORK_ADDRESS": "me@example.org", "ASTRA_MAIL_WORK_PASSWORD": "x"},
     "ASTRA_MAIL_WORK_IMAP_HOST"),
])
def test_a_named_mailbox_that_is_not_fully_set_up_says_what_is_missing(tmp_path: Path, settings, message) -> None:
    with pytest.raises(MailConfigurationError, match=message):
        load_mail_accounts(tmp_path, environ={"ASTRA_MAIL_ADDRESS": "me@gmail.com", "ASTRA_MAIL_PASSWORD": "x",
                                              **settings})
    assert not (tmp_path / ".astra").exists()


def test_default_ignores_incomplete_later_mailbox_but_not_missing_first_password(tmp_path: Path) -> None:
    environ = {"ASTRA_MAIL_ACCOUNTS": "work,other", "ASTRA_MAIL_WORK_ADDRESS": "me@qq.com",
               "ASTRA_MAIL_WORK_PASSWORD": "work-code", "ASTRA_MAIL_OTHER_ADDRESS": "other@163.com"}
    assert load_mail_config(tmp_path, environ=environ).name == "work"
    environ["ASTRA_MAIL_ACCOUNTS"] = "other,work"
    with pytest.raises(MailConfigurationError, match="ASTRA_MAIL_OTHER_PASSWORD"):
        load_mail_config(tmp_path, environ=environ)
    assert load_mail_config(tmp_path, environ=environ, account="work").name == "work"
    assert load_mail_config(tmp_path, environ=environ, require_credentials=False).name == "other"


def test_without_main_the_first_named_mailbox_comes_first(tmp_path: Path) -> None:
    configs = load_mail_accounts(tmp_path, environ={
        "ASTRA_MAIL_ACCOUNTS": "qq,work", "ASTRA_MAIL_QQ_ADDRESS": "1@qq.com", "ASTRA_MAIL_QQ_PASSWORD": "x",
        "ASTRA_MAIL_WORK_ADDRESS": "w@163.com", "ASTRA_MAIL_WORK_PASSWORD": "y"})
    assert [config.name for config in configs] == ["qq", "work"]
    assert configs[1].host == "imap.163.com" and configs[1].send_id
    assert load_mail_config(tmp_path, environ={"ASTRA_MAIL_ACCOUNTS": "qq", "ASTRA_MAIL_QQ_ADDRESS": "1@qq.com",
                                               "ASTRA_MAIL_QQ_PASSWORD": "x"}).name == "qq"


def test_an_offline_look_finds_each_mailboxs_cache_by_its_address(tmp_path: Path) -> None:
    """Offline needs no password, but the cache still follows the address: a Gmail mailbox's, not 163's."""
    configs = load_mail_accounts(tmp_path, require_credentials=False, environ={
        "ASTRA_MAIL_ADDRESS": "me@gmail.com", "ASTRA_MAIL_ACCOUNTS": "qq", "ASTRA_MAIL_QQ_ADDRESS": "1@qq.com"})
    mail_dir = tmp_path / ".astra" / "mail"
    assert [(c.name, c.account, c.auth_code, c.database_path) for c in configs] == [
        ("main", "", "", mail_dir / "imap.gmail.com.sqlite3"),
        ("qq", "", "", mail_dir / "accounts" / "qq" / "mail.sqlite3"),
    ]
    # The address is still known, so a mailbox can be picked by it offline.
    assert [config.address for config in configs] == ["me@gmail.com", "1@qq.com"]
    # With nothing set up, an offline look still means the first 163 cache.
    assert load_mail_config(tmp_path, environ={}, require_credentials=False).database_path == mail_dir / "163.sqlite3"
