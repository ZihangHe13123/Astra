from __future__ import annotations

import base64
import imaplib
import ssl
from dataclasses import replace
from pathlib import Path

import pytest

from agent.mail163.imap_client import Imap163Client
from agent.mail163.mime import MailProtocolError, decode_text_part, parse_bodystructure, sanitize_filename
from agent.mail163.models import BodyPart, MailConfig

MIXED_STRUCTURE = (
    b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "QUOTED-PRINTABLE" 120 5 NIL NIL NIL) '
    b'("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 400 NIL '
    b'("ATTACHMENT" ("FILENAME" "report.pdf")) NIL NIL) "MIXED" ("BOUNDARY" "x") NIL NIL)'
)
ALTERNATIVE_STRUCTURE = (
    b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1 NIL NIL NIL) '
    b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 30 1 NIL NIL NIL) '
    b'"ALTERNATIVE" ("BOUNDARY" "x") NIL NIL)'
)
HTML_STRUCTURE = b'("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 60 1 NIL NIL NIL)'
BAD_CHARSET_STRUCTURE = b'("TEXT" "PLAIN" ("CHARSET" "X-NOT-A-CHARSET") NIL NIL "8BIT" 4 1 NIL NIL NIL)'
HEADER_SECTION = "HEADER.FIELDS (MESSAGE-ID FROM TO CC SUBJECT DATE)"


@pytest.fixture
def config(tmp_path: Path) -> MailConfig:
    mail_dir = tmp_path / ".astra" / "mail"
    return MailConfig(
        project_root=tmp_path,
        account="user@163.com",
        auth_code="super-secret-auth-code",
        host="imap.163.com",
        port=993,
        database_path=mail_dir / "163.sqlite3",
        attachments_dir=mail_dir / "attachments",
        max_body_bytes=64,
        default_folders=("INBOX", "已发送"),
    )


class FakeImap:
    def __init__(
        self,
        *,
        structure: bytes = MIXED_STRUCTURE,
        bodies: dict[str, bytes] | None = None,
        search_status: str = "OK",
        id_error: Exception | None = None,
        login_error: Exception | None = None,
        uidvalidity: int = 77,
        search_data: list[object] | None = None,
    ) -> None:
        self.commands: list[tuple[object, ...]] = []
        self.structure = structure
        self.bodies = {"1": b"Hello=20world", "2": b"UERGIGJ5dGVz"} if bodies is None else bodies
        self.search_status = search_status
        self.id_error = id_error
        self.login_error = login_error
        self.uidvalidity = uidvalidity
        self.search_data = [b"40 41"] if search_data is None else search_data
        self.selected = b"INBOX"
        self.logged_out = False

    def _simple_command(self, name: str, *args: object) -> tuple[str, list[bytes]]:
        self.commands.append((name, *args))
        if name != "ID" or args != ('("name" "Astra")',):
            raise AssertionError(f"unplanned simple command: {(name, *args)!r}")
        if self.id_error is not None:
            raise self.id_error
        return "OK", [b"ID completed"]

    def login(self, account: str, auth_code: str) -> tuple[str, list[bytes]]:
        self.commands.append(("login", account, auth_code))
        if self.login_error is not None:
            raise self.login_error
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox: bytes, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.commands.append(("select", mailbox, readonly))
        if not readonly:
            raise AssertionError("mailbox must be selected read-only")
        self.selected = mailbox
        return "OK", [b"2"]

    def response(self, code: str) -> tuple[str, list[object]]:
        self.commands.append(("response", code))
        if code == "ESEARCH":
            return "ESEARCH", [None]
        if code != "UIDVALIDITY":
            raise AssertionError(f"unplanned response: {code!r}")
        return "UIDVALIDITY", [str(self.uidvalidity).encode()]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        self.commands.append(("uid", command, *args))
        if (command, *args) == ("SEARCH", None, "ALL"):
            if self.search_status != "OK":
                return self.search_status, [b"server details must not leak"]
            return "OK", self.search_data

        if command != "FETCH" or len(args) != 2:
            raise AssertionError(f"unplanned UID command: {(command, *args)!r}")
        uid, query = args
        if isinstance(uid, str) and query == "(UID FLAGS)":
            requested = [int(value) for value in uid.split(",")]
            responses: list[object] = []
            for index, value in enumerate(requested, start=1):
                flags = "\\Seen" if value == 40 else ""
                responses.append(f"{index} (UID {value} FLAGS ({flags}))".encode())
            return "OK", responses
        if uid == "41" and isinstance(query, str) and "HEADER.FIELDS" in query:
            headers = (
                b"Message-ID: <41@example>\r\n"
                b"From: =?UTF-8?Q?Alice_=E2=9C=93?= <alice@example.com>\r\n"
                b"To: user@163.com\r\n"
                b"Cc: team@example.com\r\n"
                b"Subject: =?UTF-8?Q?Status_=E2=9C=93?=\r\n"
                b"Date: Thu, 21 Aug 2026 10:00:00 +0800\r\n\r\n"
            )
            metadata = (
                b'2 (UID 41 INTERNALDATE "21-Aug-2026 10:00:00 +0800" '
                b"RFC822.SIZE 520 FLAGS () BODYSTRUCTURE "
                + self.structure
                + b" BODY[HEADER.FIELDS (MESSAGE-ID FROM TO CC SUBJECT DATE)] {205}"
            )
            return "OK", [(metadata, headers), b")"]
        if uid == "41" and query == "(UID BODYSTRUCTURE)":
            return "OK", [b"2 (UID 41 BODYSTRUCTURE " + self.structure + b")"]
        if uid == "41" and isinstance(query, str) and query.startswith("(BODY.PEEK["):
            part_id = query.removeprefix("(BODY.PEEK[").split("]", 1)[0]
            if part_id not in self.bodies:
                raise AssertionError(f"attachment/body part {part_id!r} was not scripted")
            body = self.bodies[part_id]
            response_header = f"2 (UID 41 BODY[{part_id}] {{{len(body)}}}".encode()
            return "OK", [(response_header, body), b")"]
        raise AssertionError(f"unplanned UID FETCH: {(uid, query)!r}")

    def list(self) -> tuple[str, list[bytes]]:
        self.commands.append(("list",))
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "A&-B"']

    def logout(self) -> tuple[str, list[bytes]]:
        self.commands.append(("logout",))
        self.logged_out = True
        return "BYE", [b"LOGOUT completed"]


class RewrittenFetchImap(FakeImap):
    def __init__(self, rewrite: str) -> None:
        super().__init__()
        self.rewrite = rewrite

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        status, data = super().uid(command, *args)
        if command != "FETCH" or len(args) != 2:
            return status, data
        _uid, query = args
        replacements: tuple[bytes, bytes] | None = None
        if isinstance(query, str) and "HEADER.FIELDS" in query:
            if self.rewrite == "metadata_uid":
                replacements = (b"UID 41", b"UID 40")
            elif self.rewrite == "header_section":
                replacements = (f"BODY[{HEADER_SECTION}]".encode(), b"BODY[2]")
        elif query == "(UID BODYSTRUCTURE)" and self.rewrite == "attachment_structure_uid":
            replacements = (b"UID 41", b"UID 40")
        elif isinstance(query, str) and query.startswith("(BODY.PEEK["):
            if self.rewrite in {"body_uid", "attachment_body_uid"}:
                replacements = (b"UID 41", b"UID 40")
            elif self.rewrite == "body_section":
                replacements = (b"BODY[1]", b"BODY[2]")
            elif self.rewrite == "attachment_body_section":
                replacements = (b"BODY[2]", b"BODY[1]")
        if replacements is None:
            return status, data
        old, new = replacements
        rewritten: list[object] = []
        for item in data:
            if isinstance(item, bytes):
                rewritten.append(item.replace(old, new, 1))
            elif isinstance(item, tuple):
                rewritten.append((item[0].replace(old, new, 1), item[1]))
            else:
                rewritten.append(item)
        return status, rewritten


class IncompleteFlagsImap(FakeImap):
    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "FETCH" and len(args) == 2 and args[1] == "(UID FLAGS)":
            self.commands.append(("uid", command, *args))
            return "OK", [b"1 (UID 40 FLAGS (\\Seen))"]
        return super().uid(command, *args)


class ExtraFlagsImap(FakeImap):
    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "FETCH" and len(args) == 2 and args[1] == "(UID FLAGS)":
            self.commands.append(("uid", command, *args))
            return (
                "OK",
                [
                    b"1 (UID 40 FLAGS (\\Seen))",
                    b"9 (UID 99 FLAGS (\\Flagged))",
                    b"2 (UID 41 FLAGS ())",
                ],
            )
        return super().uid(command, *args)


class DuplicateFlagsImap(FakeImap):
    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "FETCH" and len(args) == 2 and args[1] == "(UID FLAGS)":
            self.commands.append(("uid", command, *args))
            return (
                "OK",
                [
                    b"1 (UID 40 FLAGS (\\Seen))",
                    b"2 (UID 41 FLAGS ())",
                    b"3 (UID 41 FLAGS (\\Flagged))",
                ],
            )
        return super().uid(command, *args)


class MalformedFlagsImap(FakeImap):
    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "FETCH" and len(args) == 2 and args[1] == "(UID FLAGS)":
            self.commands.append(("uid", command, *args))
            return (
                "OK",
                [
                    b"1 (UID 40 FLAGS (\\Seen))",
                    b"2 (UID 41 FLAGS ())",
                    b"3 (UID invalid FLAGS ())",
                ],
            )
        return super().uid(command, *args)


class FlagResponseImap(FakeImap):
    def __init__(self, flag_data: list[object]) -> None:
        super().__init__()
        self.flag_data = flag_data

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "FETCH" and len(args) == 2 and args[1] == "(UID FLAGS)":
            self.commands.append(("uid", command, *args))
            return "OK", self.flag_data
        return super().uid(command, *args)


class EsearchBucketImap(FakeImap):
    def __init__(self, esearch_data: list[object]) -> None:
        super().__init__(search_data=[None])
        self.esearch_data = esearch_data

    def response(self, code: str) -> tuple[str, list[object]]:
        if code == "ESEARCH":
            self.commands.append(("response", code))
            return "ESEARCH", self.esearch_data
        return super().response(code)


class InterleavedFetchImap(FakeImap):
    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        status, data = super().uid(command, *args)
        if command != "FETCH" or len(args) != 2 or args[0] != "41" or args[1] == "(UID FLAGS)":
            return status, data
        return (
            status,
            [
                b"8 (UID 98 FLAGS (\\Seen))",
                b"2 (UID 41 FLAGS (\\Flagged))",
                *data,
                b"9 (UID 99 FLAGS ())",
            ],
        )


class DuplicateRequestedFetchImap(FakeImap):
    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        status, data = super().uid(command, *args)
        if command == "FETCH" and len(args) == 2 and args[0] == "41" and args[1] == "(UID BODYSTRUCTURE)":
            return status, [*data, *data]
        return status, data


class LiteralFilenameImap(FakeImap):
    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "FETCH" and len(args) == 2:
            uid, query = args
            if uid == "41" and isinstance(query, str) and "HEADER.FIELDS" in query:
                self.commands.append(("uid", command, *args))
                headers = (
                    b"Message-ID: <41@example>\r\n"
                    b"From: alice@example.com\r\n"
                    b"To: user@163.com\r\n"
                    b"Subject: Literal filename\r\n"
                    b"Date: Thu, 21 Aug 2026 10:00:00 +0800\r\n\r\n"
                )
                prefix = (
                    b'2 (UID 41 INTERNALDATE "21-Aug-2026 10:00:00 +0800" RFC822.SIZE 520 FLAGS () '
                    b'BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "QUOTED-PRINTABLE" 13 1 NIL NIL NIL) '
                    b'("APPLICATION" "PDF" ("NAME" {10}'
                )
                continuation = (
                    b') NIL NIL "BASE64" 12 NIL ("ATTACHMENT" ("FILENAME" "report.pdf")) NIL NIL) '
                    b'"MIXED" ("BOUNDARY" "x") NIL NIL) ' + f"BODY[{HEADER_SECTION}] {{{len(headers)}}}".encode()
                )
                return "OK", [(prefix, b"report.pdf"), (continuation, headers), b")"]
            if uid == "41" and query == "(UID BODYSTRUCTURE)":
                self.commands.append(("uid", command, *args))
                prefix = (
                    b'2 (UID 41 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "QUOTED-PRINTABLE" 13 1) '
                    b'("APPLICATION" "PDF" ("NAME" {10}'
                )
                continuation = b') NIL NIL "BASE64" 12 NIL ("ATTACHMENT" ("FILENAME" "report.pdf"))) "MIXED"))'
                return "OK", [(prefix, b"report.pdf"), continuation]
        return super().uid(command, *args)


class MalformedMessageImap(FakeImap):
    def __init__(self, mode: str) -> None:
        structure = (
            b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "BASE64" 20 1 NIL NIL NIL)'
            if mode == "bad_transfer"
            else b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 20 1 NIL NIL NIL)'
        )
        super().__init__(structure=structure, bodies={"1": b"good"})
        self.mode = mode

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        if command == "FETCH" and len(args) == 2 and args[0] == "40":
            self.commands.append(("uid", command, *args))
            _uid, query = args
            if isinstance(query, str) and "HEADER.FIELDS" in query:
                metadata = (
                    b'1 (UID 40 INTERNALDATE "21-Aug-2026 09:00:00 +0800" '
                    b"RFC822.SIZE 220 FLAGS () BODYSTRUCTURE " + self.structure + f" BODY[{HEADER_SECTION}]".encode()
                )
                if self.mode == "missing_size":
                    metadata = metadata.replace(b"RFC822.SIZE 220 ", b"")
                elif self.mode == "missing_flags":
                    metadata = metadata.replace(b"FLAGS () ", b"")
                if self.mode == "missing_header":
                    return "OK", [metadata + b" NIL)"]
                headers = (
                    b"Message-ID: <40@example>\r\n"
                    b"From: broken@example.com\r\n"
                    b"To: user@163.com\r\n"
                    b"Subject: Broken message\r\n"
                    b"Date: Thu, 21 Aug 2026 09:00:00 +0800\r\n\r\n"
                )
                if self.mode == "malformed_header":
                    headers = b"not a header\r\n\r\n"
                return "OK", [(metadata + f" {{{len(headers)}}}".encode(), headers), b")"]
            if isinstance(query, str) and query.startswith("(BODY.PEEK[1]"):
                if self.mode == "missing_body":
                    return "OK", [b"1 (UID 40 BODY[1] NIL)"]
                payload = b"%%%not-base64%%%" if self.mode == "bad_transfer" else b"good"
                return "OK", [(f"1 (UID 40 BODY[1] {{{len(payload)}}}".encode(), payload), b")"]
        return super().uid(command, *args)


@pytest.fixture
def fake_imap() -> FakeImap:
    return FakeImap()


def test_bodystructure_selects_text_and_attachment_parts() -> None:
    parts = parse_bodystructure(MIXED_STRUCTURE)

    assert [(part.part_id, part.content_type, part.disposition, part.filename) for part in parts] == [
        ("1", "text/plain", "", ""),
        ("2", "application/pdf", "attachment", "report.pdf"),
    ]
    assert sanitize_filename("../../report.pdf") == "report.pdf"
    assert sanitize_filename(r"..\..\report.pdf") == "report.pdf"


def test_bodystructure_handles_single_and_nested_parts_and_rejects_malformed_input() -> None:
    single = parse_bodystructure(HTML_STRUCTURE)
    nested = parse_bodystructure(
        b'((("TEXT" "PLAIN" NIL NIL NIL "7BIT" 1 1) '
        b'("TEXT" "HTML" NIL NIL NIL "7BIT" 1 1) "ALTERNATIVE") '
        b'("IMAGE" "PNG" ("NAME" "x.png") NIL NIL "BASE64" 8 NIL '
        b'("INLINE" ("FILENAME" "x.png"))) "MIXED")'
    )

    assert single[0].part_id == "1"
    assert [part.part_id for part in nested] == ["1.1", "1.2", "2"]
    assert nested[-1].disposition == "inline"
    assert nested[-1].filename == "x.png"
    with pytest.raises(MailProtocolError, match="BODYSTRUCTURE"):
        parse_bodystructure(b'("TEXT" "PLAIN"')
    with pytest.raises(MailProtocolError, match="nesting"):
        parse_bodystructure(b"(" * 102 + b"NIL" + b")" * 102)


def test_bodystructure_decodes_rfc2231_filename_continuations() -> None:
    parts = parse_bodystructure(
        b'("APPLICATION" "PDF" NIL NIL NIL "BASE64" 20 NIL '
        b'("ATTACHMENT" ("FILENAME*0*" "utf-8\'\'caf%C3%A9%20" '
        b'"FILENAME*1*" "report.pdf")) NIL NIL)'
    )

    assert parts[0].filename == "café report.pdf"


@pytest.mark.parametrize(
    ("encoding", "payload", "expected"),
    [
        ("quoted-printable", b"Hello=2C=20world=21", "Hello, world!"),
        ("base64", b"SGVsbG8sIHdvcmxkIQ==", "Hello, world!"),
    ],
)
def test_decode_text_part_applies_transfer_and_charset_decoding(
    encoding: str,
    payload: bytes,
    expected: str,
) -> None:
    part = BodyPart("1", "text/plain", "utf-8", encoding, len(payload), "", "")

    assert decode_text_part(payload, part) == expected


def test_decode_text_part_converts_html_and_removes_active_markup() -> None:
    part = BodyPart("1", "text/html", "utf-8", "8bit", 100, "", "")
    payload = b"<style>.bad{}</style><p>Hello&nbsp; <b>world</b></p><script>alert(1)</script>"

    assert decode_text_part(payload, part) == "Hello world"


def test_client_uses_id_uid_and_peek_without_fetching_attachment(config: MailConfig, fake_imap: FakeImap) -> None:
    with Imap163Client(config, connection_factory=lambda host, port: fake_imap) as client:
        snapshot = client.snapshot("INBOX")
        messages = client.fetch_messages("INBOX", snapshot, {41})

    assert snapshot.uidvalidity == 77
    assert snapshot.uids == frozenset({40, 41})
    assert snapshot.flags == {40: ("\\Seen",), 41: ()}
    assert messages[0].uid == 41
    assert messages[0].subject == "Status ✓"
    assert messages[0].sender == "Alice ✓ <alice@example.com>"
    assert messages[0].recipients == "user@163.com, team@example.com"
    assert messages[0].body_text == "Hello world"
    assert messages[0].attachments[0].part_id == "2"
    commands = fake_imap.commands
    assert commands[0][0] == "ID"
    assert ("select", b"INBOX", True) in commands
    assert any(command[:3] == ("uid", "FETCH", "41") for command in commands)
    assert any("BODY.PEEK[1]" in str(command) for command in commands)
    assert not any("BODY.PEEK[2]" in str(command) for command in commands)
    assert not any("BODY[]" in str(command) for command in commands)
    assert fake_imap.logged_out


def test_snapshot_rejects_incomplete_flags_coverage(config: MailConfig) -> None:
    fake = IncompleteFlagsImap()

    with (
        Imap163Client(config, connection_factory=lambda host, port: fake) as client,
        pytest.raises(MailProtocolError, match="FLAGS response was incomplete"),
    ):
        client.snapshot("INBOX")


def test_snapshot_filters_legal_unsolicited_flags_updates(config: MailConfig) -> None:
    fake = ExtraFlagsImap()

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")

    assert snapshot.flags == {40: ("\\Seen",), 41: ()}


def test_snapshot_rejects_duplicate_flags_uid(config: MailConfig) -> None:
    fake = DuplicateFlagsImap()

    with (
        Imap163Client(config, connection_factory=lambda host, port: fake) as client,
        pytest.raises(MailProtocolError, match="FLAGS response was malformed"),
    ):
        client.snapshot("INBOX")


def test_snapshot_rejects_malformed_flags_entry(config: MailConfig) -> None:
    fake = MalformedFlagsImap()

    with (
        Imap163Client(config, connection_factory=lambda host, port: fake) as client,
        pytest.raises(MailProtocolError, match="FLAGS response was malformed"),
    ):
        client.snapshot("INBOX")


@pytest.mark.parametrize(
    "entry",
    [
        b"1 (UID 40 FLAGS (\\Seen)",
        b"1 (UID 40 FLAGS (\\Seen)) garbage",
    ],
)
def test_snapshot_rejects_incomplete_or_trailing_flags_envelope(
    config: MailConfig,
    entry: bytes,
) -> None:
    fake = FlagResponseImap([entry, b"2 (UID 41 FLAGS ())"])

    with (
        Imap163Client(config, connection_factory=lambda host, port: fake) as client,
        pytest.raises(MailProtocolError, match="FLAGS response"),
    ):
        client.snapshot("INBOX")


@pytest.mark.parametrize(
    "fragment",
    [
        (b"1 (UID 40 FLAGS (\\Seen))",),
        (b"1 (UID 40 FLAGS (\\Seen))", b"literal", b"extra"),
        (b"1 (UID 40 FLAGS (\\Seen))", "not-bytes"),
        ("not-bytes", b"literal"),
    ],
)
def test_snapshot_rejects_malformed_flags_tuple_shapes(
    config: MailConfig,
    fragment: tuple[object, ...],
) -> None:
    fake = FlagResponseImap([fragment, b"2 (UID 41 FLAGS ())"])

    with (
        Imap163Client(config, connection_factory=lambda host, port: fake) as client,
        pytest.raises(MailProtocolError, match="FLAGS response"),
    ):
        client.snapshot("INBOX")


def test_snapshot_accepts_legal_literal_fragments_in_unsolicited_flags_update(config: MailConfig) -> None:
    fake = FlagResponseImap(
        [
            b"1 (UID 40 FLAGS (\\Seen))",
            (b"9 (UID 99 FLAGS (\\Flagged) BODY[1] {3}", b"abc"),
            b")",
            b"2 (UID 41 FLAGS ())",
        ]
    )

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")

    assert snapshot.flags == {40: ("\\Seen",), 41: ()}


def test_multipart_alternative_prefers_plain_text(config: MailConfig) -> None:
    fake = FakeImap(structure=ALTERNATIVE_STRUCTURE, bodies={"1": b"plain", "2": b"<b>html</b>"})

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        message = client.fetch_messages("INBOX", snapshot, {41})[0]

    assert message.body_text == "plain"
    assert any("BODY.PEEK[1]" in str(command) for command in fake.commands)
    assert not any("BODY.PEEK[2]" in str(command) for command in fake.commands)


def test_html_only_message_is_converted_to_text(config: MailConfig) -> None:
    fake = FakeImap(
        structure=HTML_STRUCTURE,
        bodies={"1": b"<p>Hello&nbsp;world</p><script>secret()</script>"},
    )

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        message = client.fetch_messages("INBOX", snapshot, {41})[0]

    assert message.body_text == "Hello world"


def test_unknown_charset_adds_warning_without_aborting_message(config: MailConfig) -> None:
    fake = FakeImap(structure=BAD_CHARSET_STRUCTURE, bodies={"1": b"caf\xe9"})

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        message = client.fetch_messages("INBOX", snapshot, {41})[0]

    assert message.body_text
    assert any("charset" in warning for warning in message.warnings)


def test_unparseable_bodystructure_adds_warning_and_empty_body(config: MailConfig) -> None:
    fake = FakeImap(structure=b'("TEXT" "PLAIN"')

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        message = client.fetch_messages("INBOX", snapshot, {41})[0]

    assert message.body_text == ""
    assert message.attachments == ()
    assert any("BODYSTRUCTURE" in warning for warning in message.warnings)
    assert not any("BODY.PEEK[1]" in str(command) for command in fake.commands)


def test_list_folders_decodes_modified_utf7_ampersand(config: MailConfig, fake_imap: FakeImap) -> None:
    with Imap163Client(config, connection_factory=lambda host, port: fake_imap) as client:
        folders = client.list_folders()

    assert folders == ("INBOX", "A&B")


def test_uid_search_failure_is_sanitized(config: MailConfig) -> None:
    fake = FakeImap(search_status="NO")

    with (
        Imap163Client(config, connection_factory=lambda host, port: fake) as client,
        pytest.raises(MailProtocolError, match="UID SEARCH failed") as caught,
    ):
        client.snapshot("INBOX")

    assert "server details" not in str(caught.value)


def test_login_error_is_sanitized_and_connection_is_closed(config: MailConfig) -> None:
    fake = FakeImap(login_error=imaplib.IMAP4.error(f"bad credentials: {config.auth_code}"))

    with (
        pytest.raises(MailProtocolError, match="login failed") as caught,
        Imap163Client(config, connection_factory=lambda host, port: fake),
    ):
        pass

    assert config.auth_code not in str(caught.value)
    assert fake.logged_out


def test_tls_error_has_stable_sanitized_type(config: MailConfig) -> None:
    def fail_tls(host: str, port: int) -> FakeImap:
        del host, port
        raise ssl.SSLCertVerificationError(f"certificate detail: {config.auth_code}")

    with pytest.raises(MailProtocolError) as caught, Imap163Client(config, connection_factory=fail_tls):
        pass

    assert type(caught.value).__name__ == "MailTlsError"
    assert str(caught.value) == "IMAP TLS connection failed"
    assert config.auth_code not in str(caught.value)


def test_id_failure_is_protocol_not_authentication(config: MailConfig) -> None:
    fake = FakeImap(id_error=imaplib.IMAP4.error(f"ID rejected: {config.auth_code}"))

    with pytest.raises(MailProtocolError) as caught, Imap163Client(
        config,
        connection_factory=lambda host, port: fake,
    ):
        pass

    assert type(caught.value).__name__ == "MailProtocolError"
    assert str(caught.value) == "IMAP ID failed"
    assert config.auth_code not in str(caught.value)
    assert fake.logged_out


def test_authentication_error_has_stable_sanitized_type(config: MailConfig) -> None:
    fake = FakeImap(login_error=imaplib.IMAP4.error(f"bad credentials: {config.auth_code}"))

    with pytest.raises(MailProtocolError) as caught, Imap163Client(
        config,
        connection_factory=lambda host, port: fake,
    ):
        pass

    assert type(caught.value).__name__ == "MailAuthenticationError"
    assert str(caught.value) == "IMAP login failed"
    assert config.auth_code not in str(caught.value)


def test_unsafe_login_error_has_stable_sanitized_type(config: MailConfig) -> None:
    fake = FakeImap(
        login_error=imaplib.IMAP4.error(f"Unsafe Login. Please contact service: {config.auth_code}")
    )

    with pytest.raises(MailProtocolError) as caught, Imap163Client(
        config,
        connection_factory=lambda host, port: fake,
    ):
        pass

    assert type(caught.value).__name__ == "MailUnsafeLoginError"
    assert str(caught.value) == "IMAP unsafe login rejected"
    assert config.auth_code not in str(caught.value)


def test_partial_body_fetch_caps_saved_text_and_marks_truncation(config: MailConfig) -> None:
    limited = replace(config, max_body_bytes=7)
    fake = FakeImap(
        structure=b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 20 1 NIL NIL NIL)',
        bodies={"1": b"12345678"},
    )

    with Imap163Client(limited, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        message = client.fetch_messages("INBOX", snapshot, {41})[0]

    assert message.body_text == "1234567"
    assert message.body_truncated is True
    assert any("BODY.PEEK[1]<0.8>" in str(command) for command in fake.commands)


def test_truncated_base64_body_preserves_prefix_and_caps_decoded_text(config: MailConfig) -> None:
    limited = replace(config, max_body_bytes=10)
    original = b"0123456789abcdefghijklmnopqrstuvwxyz"
    partial_quantum = base64.b64encode(original)[:-1]
    fake = FakeImap(
        structure=(
            b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "BASE64" '
            + str(len(base64.b64encode(original))).encode()
            + b" 1 NIL NIL NIL)"
        ),
        bodies={"1": partial_quantum},
    )

    with Imap163Client(limited, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        message = client.fetch_messages("INBOX", snapshot, {41})[0]

    assert message.body_text == original[:10].decode()
    assert len(message.body_text.encode()) <= limited.max_body_bytes
    assert message.body_truncated is True


def test_download_attachment_fetches_and_decodes_only_validated_part(
    config: MailConfig,
    fake_imap: FakeImap,
) -> None:
    with Imap163Client(config, connection_factory=lambda host, port: fake_imap) as client:
        payload = client.download_attachment("INBOX", 77, 41, "2")

    assert payload == b"PDF bytes"
    body_fetches = [command for command in fake_imap.commands if "BODY.PEEK" in str(command)]
    assert len(body_fetches) == 1
    assert "BODY.PEEK[2]" in str(body_fetches[0])
    assert "<0." not in str(body_fetches[0])
    assert not any("BODY[]" in str(command) for command in fake_imap.commands)


@pytest.mark.parametrize("part_id", ["1", "2 BODY[]", "0", "1..2"])
def test_download_attachment_rejects_non_attachment_or_invalid_part_id(
    config: MailConfig,
    fake_imap: FakeImap,
    part_id: str,
) -> None:
    with (
        Imap163Client(config, connection_factory=lambda host, port: fake_imap) as client,
        pytest.raises(MailProtocolError, match="attachment"),
    ):
        client.download_attachment("INBOX", 77, 41, part_id)

    assert not any(f"BODY.PEEK[{part_id}]" in str(command) for command in fake_imap.commands)


@pytest.mark.parametrize("bad_uid", ["41", "41\r\nBODY[]", "BODY[]", 41.0, True, 0, 2**32])
def test_download_attachment_rejects_invalid_uid_before_interpolation(
    config: MailConfig,
    fake_imap: FakeImap,
    bad_uid: object,
) -> None:
    with (
        Imap163Client(config, connection_factory=lambda host, port: fake_imap) as client,
        pytest.raises(MailProtocolError, match="UID"),
    ):
        client.download_attachment("INBOX", 77, bad_uid, "2")  # type: ignore[arg-type]

    assert not any(command[:2] == ("uid", "FETCH") for command in fake_imap.commands)


@pytest.mark.parametrize("bad_uidvalidity", ["77", "77\r\nBODY[]", True, 0, 2**32])
def test_download_attachment_rejects_invalid_uidvalidity_before_select(
    config: MailConfig,
    fake_imap: FakeImap,
    bad_uidvalidity: object,
) -> None:
    with (
        Imap163Client(config, connection_factory=lambda host, port: fake_imap) as client,
        pytest.raises(MailProtocolError, match="UIDVALIDITY"),
    ):
        client.download_attachment("INBOX", bad_uidvalidity, 41, "2")  # type: ignore[arg-type]

    assert not any(command[0] == "select" for command in fake_imap.commands)


def test_fetch_messages_rejects_non_integer_uid_equal_to_snapshot_uid(config: MailConfig, fake_imap: FakeImap) -> None:
    with Imap163Client(config, connection_factory=lambda host, port: fake_imap) as client:
        snapshot = client.snapshot("INBOX")
        with pytest.raises(MailProtocolError, match="UID"):
            client.fetch_messages("INBOX", snapshot, [41.0])  # type: ignore[list-item]

    assert not any(command[:3] == ("uid", "FETCH", "41.0") for command in fake_imap.commands)


def test_download_attachment_forces_fresh_uidvalidity_read(config: MailConfig, fake_imap: FakeImap) -> None:
    with Imap163Client(config, connection_factory=lambda host, port: fake_imap) as client:
        client.snapshot("INBOX")
        fake_imap.uidvalidity = 78
        with pytest.raises(MailProtocolError, match="generation changed"):
            client.download_attachment("INBOX", 77, 41, "2")

    assert sum(command[0] == "select" for command in fake_imap.commands) == 2
    assert not any(
        command[:2] == ("uid", "FETCH") and command[3] == "(UID BODYSTRUCTURE)" for command in fake_imap.commands
    )


def test_select_quotes_and_escapes_unsafe_mailbox_astring(config: MailConfig, fake_imap: FakeImap) -> None:
    with Imap163Client(config, connection_factory=lambda host, port: fake_imap) as client:
        client.snapshot('Project "A" \\ Inbox')

    assert ("select", b'"Project \\"A\\" \\\\ Inbox"', True) in fake_imap.commands


def test_literal_bodystructure_filename_is_not_confused_with_header_or_body(config: MailConfig) -> None:
    fake = LiteralFilenameImap()

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        message = client.fetch_messages("INBOX", snapshot, {41})[0]
        attachment = client.download_attachment("INBOX", 77, 41, "2")

    assert message.subject == "Literal filename"
    assert message.body_text == "Hello world"
    assert message.attachments[0].filename == "report.pdf"
    assert attachment == b"PDF bytes"


def test_interleaved_unsolicited_fetch_updates_do_not_hide_requested_message(config: MailConfig) -> None:
    fake = InterleavedFetchImap()

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        message = client.fetch_messages("INBOX", snapshot, {41})[0]

    assert message.uid == 41
    assert message.subject == "Status ✓"
    assert message.body_text == "Hello world"
    assert message.warnings == ()


def test_interleaved_unsolicited_fetch_updates_do_not_hide_requested_attachment(config: MailConfig) -> None:
    fake = InterleavedFetchImap()

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        attachment = client.download_attachment("INBOX", 77, 41, "2")

    assert attachment == b"PDF bytes"


def test_duplicate_requested_fetch_payload_is_rejected(config: MailConfig) -> None:
    fake = DuplicateRequestedFetchImap()

    with (
        Imap163Client(config, connection_factory=lambda host, port: fake) as client,
        pytest.raises(MailProtocolError, match="response"),
    ):
        client.download_attachment("INBOX", 77, 41, "2")


@pytest.mark.parametrize("rewrite", ["metadata_uid", "header_section", "body_uid", "body_section"])
def test_mismatched_message_uid_or_body_section_becomes_message_warning(config: MailConfig, rewrite: str) -> None:
    fake = RewrittenFetchImap(rewrite)

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        message = client.fetch_messages("INBOX", snapshot, {41})[0]

    assert message.uid == 41
    assert message.body_text == ""
    assert any("response" in warning.lower() for warning in message.warnings)


@pytest.mark.parametrize(
    "rewrite",
    ["attachment_structure_uid", "attachment_body_uid", "attachment_body_section"],
)
def test_download_attachment_rejects_mismatched_response_uid_or_section(config: MailConfig, rewrite: str) -> None:
    fake = RewrittenFetchImap(rewrite)

    with (
        Imap163Client(config, connection_factory=lambda host, port: fake) as client,
        pytest.raises(MailProtocolError, match="response"),
    ):
        client.download_attachment("INBOX", 77, 41, "2")


@pytest.mark.parametrize(
    ("search_data", "expected"),
    [
        ([b"40 41"], frozenset({40, 41})),
        ([b'(TAG "A1") UID', b"ALL 42:40,50"], frozenset({40, 41, 42, 50})),
        ([b'(TAG "A1") UID'], frozenset()),
        ([b'(TAG "A1") UID COUNT 0'], frozenset()),
    ],
)
def test_snapshot_parses_legacy_search_and_esearch_uid_sequence_sets(
    config: MailConfig,
    search_data: list[object],
    expected: frozenset[int],
) -> None:
    fake = FakeImap(search_data=search_data)

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")

    assert snapshot.uids == expected
    assert ("uid", "SEARCH", None, "ALL") in fake.commands


@pytest.mark.parametrize(
    ("esearch_data", "expected"),
    [
        ([b'(TAG "A1") UID ALL 40:42,50'], frozenset({40, 41, 42, 50})),
        ([None], frozenset()),
        ([b'(TAG "A1") UID COUNT 0'], frozenset()),
    ],
)
def test_snapshot_reads_real_imaplib_esearch_response_bucket(
    config: MailConfig,
    esearch_data: list[object],
    expected: frozenset[int],
) -> None:
    fake = EsearchBucketImap(esearch_data)

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")

    assert snapshot.uids == expected
    assert ("uid", "SEARCH", None, "ALL") in fake.commands
    assert ("response", "ESEARCH") in fake.commands


@pytest.mark.parametrize(
    "mode",
    ["missing_header", "malformed_header", "missing_size", "missing_flags", "missing_body", "bad_transfer"],
)
def test_message_data_failure_yields_warning_and_empty_body(config: MailConfig, mode: str) -> None:
    fake = MalformedMessageImap(mode)

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        message = client.fetch_messages("INBOX", snapshot, {40})[0]

    assert message.uid == 40
    assert message.body_text == ""
    assert message.warnings


def test_one_malformed_message_does_not_abort_other_uids(config: MailConfig) -> None:
    fake = MalformedMessageImap("missing_header")

    with Imap163Client(config, connection_factory=lambda host, port: fake) as client:
        snapshot = client.snapshot("INBOX")
        messages = client.fetch_messages("INBOX", snapshot, {40, 41})

    assert [message.uid for message in messages] == [40, 41]
    assert messages[0].body_text == ""
    assert messages[0].warnings
    assert messages[1].body_text == "good"
    assert messages[1].warnings == ()


def test_a_server_that_does_not_need_id_gets_none(config: MailConfig, fake_imap: FakeImap) -> None:
    from dataclasses import replace

    gmail = replace(config, account="user@gmail.com", host="imap.gmail.com", send_id=False)
    with Imap163Client(gmail, connection_factory=lambda host, port: fake_imap) as client:
        client.snapshot("INBOX")
    assert fake_imap.commands[0][0] == "login" and not any(command[0] == "ID" for command in fake_imap.commands)
