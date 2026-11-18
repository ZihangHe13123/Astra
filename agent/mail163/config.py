import base64
import binascii
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from dotenv import dotenv_values

from .models import MailConfig

_DEFAULT_MAX_BODY_BYTES = 5 * 1024 * 1024
_DIRECT_MAILBOX_CHARACTERS = set(range(0x20, 0x7F)) - {ord("&")}
_FOLDER_ALIASES = {
    "inbox": "INBOX",
    "received": "INBOX",
    "收件箱": "INBOX",
    "sent": "已发送",
    "sent items": "已发送",
    "已发送": "已发送",
}


# Known providers by address domain: IMAP host, sent folder, IMAP ID before login.
_PROVIDERS = {
    **dict.fromkeys(("163.com",), ("imap.163.com", "已发送", True)),
    **dict.fromkeys(("126.com",), ("imap.126.com", "已发送", True)),
    **dict.fromkeys(("yeah.net",), ("imap.yeah.net", "已发送", True)),
    **dict.fromkeys(("qq.com", "foxmail.com"), ("imap.qq.com", "Sent Messages", False)),
    **dict.fromkeys(("gmail.com", "googlemail.com"), ("imap.gmail.com", "[Gmail]/Sent Mail", False)),
    **dict.fromkeys(("icloud.com", "me.com", "mac.com"), ("imap.mail.me.com", "Sent Messages", False)),
}


class MailConfigurationError(ValueError):
    """Raised when required mail configuration is missing or invalid."""


class MailNotSetUpError(MailConfigurationError):
    """Raised when no mailbox is set up at all."""


AccountProblems = dict[tuple[str, str], MailConfigurationError]


def check_account_problems(problems: AccountProblems, account: str) -> None:
    wanted = account.strip().casefold()
    for (name, address), problem in problems.items():
        if wanted and wanted in (name, address):
            raise problem


# The mailbox of ASTRA_MAIL_ADDRESS (or the 163 names); every other one is named in ASTRA_MAIL_ACCOUNTS.
MAIN_ACCOUNT = "main"
_ACCOUNT_NAME = re.compile(r"[a-z0-9]{1,32}")


def load_mail_accounts(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    require_credentials: bool = True,
    problems: AccountProblems | None = None,
) -> list[MailConfig]:
    """Every configured mailbox, without creating local state: "main" first when it is set up.

    ``ASTRA_MAIL_ADDRESS`` and ``ASTRA_MAIL_PASSWORD`` (an app password or authorization code) name
    any IMAP account, "main"; the host and sent folder of known providers follow from the address, and
    ``ASTRA_MAIL_IMAP_HOST``, ``ASTRA_MAIL_IMAP_PORT`` and ``ASTRA_MAIL_SENT_FOLDER`` set them for
    others. The older ``ASTRA_163_EMAIL`` and ``ASTRA_163_AUTH_CODE`` still work. More mailboxes are
    named in ``ASTRA_MAIL_ACCOUNTS`` (comma-separated letters and digits), each with the same settings
    under ``ASTRA_MAIL_<NAME>_``, and their own cache and attachments under ``.astra/mail/accounts/<name>``.
    """
    values = _settings(project_root, environ)
    names = _account_names(values)
    has_main = bool(values.get("ASTRA_MAIL_ADDRESS") or values.get("ASTRA_163_EMAIL"))
    configured = ([MAIN_ACCOUNT] if has_main or not names else []) + names
    configs = []
    for name in configured:
        try:
            config = (_main_config(project_root, values, require_credentials) if name == MAIN_ACCOUNT
                      else _named_config(project_root, values, name, require_credentials))
        except MailConfigurationError as error:
            if problems is None:
                raise
            address = (values.get("ASTRA_MAIL_ADDRESS") or values.get("ASTRA_163_EMAIL", "")
                       if name == MAIN_ACCOUNT else values.get(f"ASTRA_MAIL_{name.upper()}_ADDRESS", ""))
            problems[name, address.strip().casefold()] = error
        else:
            configs.append(config)
    return configs


def load_mail_config(
    project_root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    require_credentials: bool = True,
    account: str = "",
) -> MailConfig:
    """One mailbox: the one ``account`` names (its name or address), else the first configured."""
    problems: AccountProblems = {}
    configs = load_mail_accounts(project_root, environ=environ, require_credentials=require_credentials,
                                 problems=problems)
    check_account_problems(problems, account)
    if not configs:
        raise next(iter(problems.values()))
    if not account.strip():
        return configs[0]
    return find_account(configs, account)


def find_account(configs: list[MailConfig], account: str) -> MailConfig:
    """The mailbox a name or an address names."""
    wanted = account.strip().casefold()
    for config in configs:
        if wanted in {config.name, config.account.casefold(), config.address.casefold()}:
            return config
    raise MailConfigurationError(
        f"No mailbox {account.strip()!r}; the configured ones are {', '.join(config.name for config in configs)}")


def _settings(project_root: Path, environ: Mapping[str, str] | None) -> dict[str, str]:
    dotenv = {key: value for key, value in dotenv_values(project_root / ".env").items() if value is not None}
    environment = os.environ if environ is None else environ
    return {**dotenv, **environment}


def _account_names(values: Mapping[str, str]) -> list[str]:
    names: list[str] = []
    for part in values.get("ASTRA_MAIL_ACCOUNTS", "").split(","):
        name = part.strip().casefold()
        if not name:
            continue
        if not _ACCOUNT_NAME.fullmatch(name):
            raise MailConfigurationError(
                f"ASTRA_MAIL_ACCOUNTS: {part.strip()!r} is not a mailbox name (letters and digits, at most 32)")
        if name == MAIN_ACCOUNT:
            raise MailConfigurationError("ASTRA_MAIL_ACCOUNTS: main is the mailbox of ASTRA_MAIL_ADDRESS")
        if name in names:
            raise MailConfigurationError(f"ASTRA_MAIL_ACCOUNTS names {name} twice")
        names.append(name)
    return names


def _main_config(project_root: Path, values: Mapping[str, str], require_credentials: bool) -> MailConfig:
    address = values.get("ASTRA_MAIL_ADDRESS") or values.get("ASTRA_163_EMAIL", "")
    auth_code = values.get("ASTRA_MAIL_PASSWORD") or values.get("ASTRA_163_AUTH_CODE", "")
    if require_credentials and not address:
        raise MailNotSetUpError("ASTRA_MAIL_ADDRESS (or ASTRA_163_EMAIL) is required")
    if require_credentials and not auth_code:
        raise MailConfigurationError("ASTRA_MAIL_PASSWORD (or ASTRA_163_AUTH_CODE) is required")
    mail_dir = project_root / ".astra" / "mail"

    def cache(host: str) -> Path:
        # 163 keeps its first cache; other hosts get their own.
        return mail_dir / ("163.sqlite3" if host == "imap.163.com" else f"{host}.sqlite3")

    return _config(project_root, values, MAIN_ACCOUNT, "ASTRA_MAIL_", address, auth_code, require_credentials,
                   cache, mail_dir / "attachments")


def _named_config(project_root: Path, values: Mapping[str, str], name: str, require_credentials: bool) -> MailConfig:
    prefix = f"ASTRA_MAIL_{name.upper()}_"
    address = values.get(f"{prefix}ADDRESS", "").strip()
    auth_code = values.get(f"{prefix}PASSWORD", "")
    if not address:
        raise MailConfigurationError(f"{prefix}ADDRESS is required for the mailbox {name} in ASTRA_MAIL_ACCOUNTS")
    if require_credentials and not auth_code:
        raise MailConfigurationError(f"{prefix}PASSWORD is required for the mailbox {name} in ASTRA_MAIL_ACCOUNTS")
    account_dir = project_root / ".astra" / "mail" / "accounts" / name
    return _config(project_root, values, name, prefix, address, auth_code, require_credentials,
                   lambda _host: account_dir / "mail.sqlite3", account_dir / "attachments")


def _config(
    project_root: Path,
    values: Mapping[str, str],
    name: str,
    prefix: str,
    address: str,
    auth_code: str,
    require_credentials: bool,
    cache: Callable[[str], Path],
    attachments_dir: Path,
) -> MailConfig:
    # The address is not a secret: an offline look finds the right cache by it, and reads
    # whichever account that cache holds.
    domain = address.rpartition("@")[2].strip().casefold() or "163.com"
    host, sent_folder, send_id = _PROVIDERS.get(domain, ("", "Sent", False))
    host = values.get(f"{prefix}IMAP_HOST", "").strip() or host
    if not host:
        raise MailConfigurationError(f"Set {prefix}IMAP_HOST: {domain} is not a known mail provider")
    port = _read_positive_int(values.get(f"{prefix}IMAP_PORT"), f"{prefix}IMAP_PORT", default=993)
    sent_folder = values.get(f"{prefix}SENT_FOLDER", "").strip() or sent_folder
    max_body_bytes = _read_positive_int(values.get("ASTRA_163_MAX_BODY_BYTES"), "ASTRA_163_MAX_BODY_BYTES")
    return MailConfig(
        project_root=project_root,
        account=address if require_credentials else "",
        auth_code=auth_code if require_credentials else "",
        host=host,
        port=port,
        database_path=cache(host),
        attachments_dir=attachments_dir,
        max_body_bytes=max_body_bytes,
        default_folders=("INBOX", sent_folder),
        sent_folder=sent_folder,
        send_id=send_id,
        sync_limit=_read_positive_int(values.get("ASTRA_MAIL_SYNC_LIMIT"), "ASTRA_MAIL_SYNC_LIMIT", default=100),
        name=name,
        address=address,
    )


def resolve_folder(value: str, sent_folder: str = "已发送") -> str:
    """Return the canonical mailbox name for a supported folder alias."""
    canonical = _FOLDER_ALIASES.get(value.strip().casefold(), value)
    return sent_folder if canonical == "已发送" else canonical


def encode_mailbox_name(value: str) -> bytes:
    """Encode a Unicode mailbox name using RFC 3501 modified UTF-7."""
    encoded = bytearray()
    non_direct: list[str] = []

    def flush_non_direct() -> None:
        if not non_direct:
            return
        utf16 = "".join(non_direct).encode("utf-16-be")
        shifted = base64.b64encode(utf16).rstrip(b"=").replace(b"/", b",")
        encoded.extend(b"&" + shifted + b"-")
        non_direct.clear()

    for character in value:
        if ord(character) in _DIRECT_MAILBOX_CHARACTERS:
            flush_non_direct()
            encoded.extend(character.encode("ascii"))
        elif character == "&":
            flush_non_direct()
            encoded.extend(b"&-")
        else:
            non_direct.append(character)
    flush_non_direct()
    return bytes(encoded)


def decode_mailbox_name(value: bytes) -> str:
    """Decode an RFC 3501 modified UTF-7 mailbox name."""
    decoded: list[str] = []
    index = 0
    while index < len(value):
        current = value[index]
        if current == ord("&"):
            end = value.find(b"-", index + 1)
            if end == -1:
                raise ValueError("unterminated modified UTF-7 sequence")
            shifted = value[index + 1 : end]
            if not shifted:
                decoded.append("&")
            else:
                padding = b"=" * (-len(shifted) % 4)
                try:
                    utf16 = base64.b64decode(shifted.replace(b",", b"/") + padding, validate=True)
                    decoded.append(utf16.decode("utf-16-be"))
                except (UnicodeDecodeError, binascii.Error) as error:
                    raise ValueError("invalid modified UTF-7 sequence") from error
            index = end + 1
            continue
        if current not in _DIRECT_MAILBOX_CHARACTERS:
            raise ValueError("invalid direct modified UTF-7 byte")
        decoded.append(chr(current))
        index += 1
    return "".join(decoded)


def _read_positive_int(value: str | None, name: str, *, default: int = _DEFAULT_MAX_BODY_BYTES) -> int:
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise MailConfigurationError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise MailConfigurationError(f"{name} must be a positive integer")
    return parsed
