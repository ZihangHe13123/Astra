import imaplib
import re
import ssl
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.header import decode_header
from email.parser import BytesHeaderParser
from typing import Any, Self

from .config import decode_mailbox_name, encode_mailbox_name
from .mime import MailProtocolError, _decode_text_part_with_warnings, _decode_transfer, parse_bodystructure
from .models import AttachmentMeta, BodyPart, FolderSnapshot, MailConfig, RemoteMessage

imaplib.Commands["ID"] = ("NONAUTH", "AUTH", "SELECTED")

_PART_ID = re.compile(r"[1-9][0-9]*(?:\.[1-9][0-9]*)*\Z")
_LIST_MAILBOX = re.compile(rb'(?:"((?:\\.|[^"\\])*)"|(\S+))\s*\Z')
_UID = re.compile(rb"\bUID\s+(\d+)\b", re.IGNORECASE)
_FLAGS = re.compile(rb"\bFLAGS\s*\(([^)]*)\)", re.IGNORECASE | re.DOTALL)
_SIZE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)\b", re.IGNORECASE)
_INTERNALDATE = re.compile(rb'\bINTERNALDATE\s+"([^"]+)"', re.IGNORECASE)
_LITERAL_MARKER = re.compile(rb"\{\d+\+?\}\s*\Z")
_BODY_SECTION = re.compile(rb"\bBODY\[([^]]*)\](?:<\d+>)?\s*\Z", re.IGNORECASE)
_FETCH_RESPONSE_START = re.compile(rb"^\s*\d+\s+\(")
_ESEARCH_CORRELATOR = re.compile(rb'^\(TAG\s+(?:"(?:\\.|[^"\\])*"|\S+)\)\s*', re.IGNORECASE)
_MAX_UID = 2**32 - 1
_MAX_SEARCH_RESULTS = 1_000_000
_IMAP_ATOM_UNSAFE = frozenset(b'(){ %*]"\\')
_HEADER_SECTION = "HEADER.FIELDS (MESSAGE-ID FROM TO CC SUBJECT DATE)"


class _MessageDataError(MailProtocolError):
    """Raised for one malformed successful FETCH response."""


class MailTlsError(MailProtocolError):
    """Raised when the secure IMAP transport cannot be established or maintained."""


class MailNetworkError(MailProtocolError):
    """Raised when the IMAP server cannot be reached."""


class MailAuthenticationError(MailProtocolError):
    """Raised when 163 rejects the configured account credentials."""


class MailUnsafeLoginError(MailProtocolError):
    """Raised when 163 rejects a login under its unsafe-login policy."""


@dataclass(frozen=True)
class _FetchData:
    metadata: bytes
    sections: dict[str, tuple[bytes, ...]]


class Imap163Client:
    """Small read-only IMAP adapter that fetches only selected MIME leaves."""

    def __init__(
        self,
        config: MailConfig,
        connection_factory: Callable[[str, int], Any] = imaplib.IMAP4_SSL,
    ) -> None:
        self.config = config
        self._connection_factory = connection_factory
        self._connection: Any | None = None
        self._selected_folder = ""
        self._selected_uidvalidity = 0

    def __enter__(self) -> Self:
        try:
            connection = self._connection_factory(self.config.host, self.config.port)
        except ssl.SSLError:
            raise MailTlsError("IMAP TLS connection failed") from None
        except (imaplib.IMAP4.error, OSError):
            raise MailNetworkError("IMAP connection failed") from None
        self._connection = connection
        try:
            if self.config.send_id:
                status, _data = self._protocol_call("ID", connection._simple_command, "ID", '("name" "Astra")')
                self._require_ok(status, "ID")
        except MailProtocolError:
            self._safe_logout()
            raise
        try:
            status, _data = connection.login(self.config.account, self.config.auth_code)
        except imaplib.IMAP4.error as error:
            self._safe_logout()
            if "unsafe login" in str(error).casefold():
                raise MailUnsafeLoginError("IMAP unsafe login rejected") from None
            raise MailAuthenticationError("IMAP login failed") from None
        except ssl.SSLError:
            self._safe_logout()
            raise MailTlsError("IMAP TLS connection failed") from None
        except OSError:
            self._safe_logout()
            raise MailNetworkError("IMAP connection failed") from None
        try:
            self._require_ok(status, "login")
        except MailProtocolError:
            self._safe_logout()
            raise MailAuthenticationError("IMAP login failed") from None
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        del exc_type, exc_value, traceback
        self._safe_logout()
        return False

    def list_folders(self) -> tuple[str, ...]:
        status, data = self._protocol_call("LIST", self._require_connection().list)
        self._require_ok(status, "LIST")
        folders: list[str] = []
        for item in data or []:
            if not isinstance(item, bytes):
                continue
            match = _LIST_MAILBOX.search(item)
            if match is None:
                raise MailProtocolError("IMAP LIST response was malformed")
            encoded = match.group(1) if match.group(1) is not None else match.group(2)
            if match.group(1) is not None:
                encoded = re.sub(rb"\\(.)", rb"\1", encoded)
            try:
                folders.append(decode_mailbox_name(encoded))
            except ValueError:
                raise MailProtocolError("IMAP LIST response contained an invalid mailbox name") from None
        return tuple(folders)

    def snapshot(self, folder: str) -> FolderSnapshot:
        uidvalidity = self._select_read_only(folder)
        connection = self._require_connection()
        status, data = self._protocol_call("UID SEARCH", connection.uid, "SEARCH", None, "ALL")
        self._require_ok(status, "UID SEARCH")
        esearch_status, esearch_data = self._protocol_call("ESEARCH", connection.response, "ESEARCH")
        if esearch_data and any(item is not None for item in esearch_data):
            if str(esearch_status).upper() != "ESEARCH":
                raise MailProtocolError("IMAP ESEARCH response was malformed")
            data = esearch_data
        uids = self._parse_search_uids(data)
        flags: dict[int, tuple[str, ...]] = {}
        if uids:
            uid_set = ",".join(str(uid) for uid in sorted(uids))
            status, flag_data = self._protocol_call("UID FETCH FLAGS", connection.uid, "FETCH", uid_set, "(UID FLAGS)")
            self._require_ok(status, "UID FETCH FLAGS")
            parsed_flags = self._parse_flags(flag_data)
            if not uids.issubset(parsed_flags):
                raise MailProtocolError("IMAP UID FETCH FLAGS response was incomplete")
            flags = {uid: parsed_flags[uid] for uid in uids}
        return FolderSnapshot(folder=folder, uidvalidity=uidvalidity, uids=frozenset(uids), flags=flags)

    def fetch_messages(
        self,
        folder: str,
        snapshot: FolderSnapshot,
        uids: Iterable[int],
    ) -> tuple[RemoteMessage, ...]:
        if snapshot.folder != folder:
            raise MailProtocolError("IMAP snapshot folder does not match the requested folder")
        uidvalidity = self._validate_uid_value(snapshot.uidvalidity, "UIDVALIDITY")
        self._ensure_selected_generation(folder, uidvalidity)
        snapshot_uids = {self._validate_uid_value(value, "UID") for value in snapshot.uids}
        requested = sorted({self._validate_uid_value(value, "UID") for value in uids})
        if not set(requested).issubset(snapshot_uids):
            raise MailProtocolError("IMAP message UID was not present in the folder snapshot")
        return tuple(self._fetch_message(folder, uidvalidity, uid) for uid in requested)

    def download_attachment(self, folder: str, uidvalidity: int, uid: int, part_id: str) -> bytes:
        uidvalidity = self._validate_uid_value(uidvalidity, "UIDVALIDITY")
        uid = self._validate_uid_value(uid, "UID")
        if not _PART_ID.fullmatch(part_id):
            raise MailProtocolError("invalid attachment part identifier")
        current_uidvalidity = self._select_read_only(folder)
        if current_uidvalidity != uidvalidity:
            raise MailProtocolError("IMAP mailbox generation changed")
        connection = self._require_connection()
        status, data = self._protocol_call(
            "UID FETCH BODYSTRUCTURE",
            connection.uid,
            "FETCH",
            str(uid),
            "(UID BODYSTRUCTURE)",
        )
        self._require_ok(status, "UID FETCH BODYSTRUCTURE")
        parsed = self._select_fetch_data(
            data,
            uid,
            operation="BODYSTRUCTURE",
            require_bodystructure=True,
        )
        structure = parse_bodystructure(self._extract_bodystructure(parsed.metadata))
        part = next((candidate for candidate in structure if candidate.part_id == part_id), None)
        if part is None or not self._is_attachment(part):
            raise MailProtocolError("requested attachment part was not present")
        status, body_data = self._protocol_call(
            "UID FETCH attachment",
            connection.uid,
            "FETCH",
            str(uid),
            f"(BODY.PEEK[{part_id}])",
        )
        self._require_ok(status, "UID FETCH attachment")
        parsed_body = self._select_fetch_data(
            body_data,
            uid,
            operation="attachment",
            expected_section=part_id,
        )
        payload = self._required_section_literal(parsed_body, part_id, operation="attachment")
        decoded, _warnings = _decode_transfer(payload, part.transfer_encoding, strict=True)
        return decoded

    def _fetch_message(self, folder: str, uidvalidity: int, uid: int) -> RemoteMessage:
        connection = self._require_connection()
        query = (
            "(UID INTERNALDATE RFC822.SIZE FLAGS BODYSTRUCTURE "
            "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM TO CC SUBJECT DATE)])"
        )
        status, data = self._protocol_call("UID FETCH metadata", connection.uid, "FETCH", str(uid), query)
        self._require_ok(status, "UID FETCH metadata")
        try:
            return self._parse_message(folder, uidvalidity, uid, data)
        except _MessageDataError:
            return self._empty_message(folder, uidvalidity, uid, "message FETCH response could not be parsed")

    def _parse_message(
        self,
        folder: str,
        uidvalidity: int,
        uid: int,
        data: Sequence[object] | None,
    ) -> RemoteMessage:
        connection = self._require_connection()
        parsed = self._select_fetch_data(
            data,
            uid,
            operation="message metadata",
            expected_section=_HEADER_SECTION,
        )
        metadata = parsed.metadata
        header_payload = self._required_section_literal(parsed, _HEADER_SECTION, operation="message headers")
        size = self._required_integer(metadata, _SIZE, "RFC822.SIZE")
        flags = self._flags_from_metadata(metadata)
        received_at, date_warnings = self._parse_internaldate(metadata)
        headers, header_warnings = self._parse_headers(header_payload)

        warnings = [*date_warnings, *header_warnings]
        parts: tuple[BodyPart, ...] = ()
        try:
            parts = parse_bodystructure(self._extract_bodystructure(metadata))
        except MailProtocolError:
            warnings.append("BODYSTRUCTURE could not be parsed")

        attachments = tuple(
            AttachmentMeta(
                part_id=part.part_id,
                filename=part.filename or f"attachment-{part.part_id}",
                content_type=part.content_type,
                size=part.encoded_size,
            )
            for part in parts
            if self._is_attachment(part)
        )
        body_text = ""
        body_truncated = False
        body_part = self._preferred_body_part(parts)
        if body_part is not None:
            fetch_limit = self._body_fetch_limit(body_part)
            status, body_data = self._protocol_call(
                "UID FETCH body part",
                connection.uid,
                "FETCH",
                str(uid),
                f"(BODY.PEEK[{body_part.part_id}]<0.{fetch_limit}>)",
            )
            self._require_ok(status, "UID FETCH body part")
            parsed_body = self._select_fetch_data(
                body_data,
                uid,
                operation="message body",
                expected_section=body_part.part_id,
            )
            raw_payload = self._required_section_literal(parsed_body, body_part.part_id, operation="message body")
            payload = raw_payload[:fetch_limit]
            transfer_truncated = len(raw_payload) >= fetch_limit
            body_text, body_warnings, decoded_truncated = _decode_text_part_with_warnings(
                payload,
                body_part,
                max_decoded_bytes=self.config.max_body_bytes,
                allow_truncated_transfer=transfer_truncated,
            )
            body_truncated = transfer_truncated or decoded_truncated
            warnings.extend(body_warnings)

        return RemoteMessage(
            folder=folder,
            uidvalidity=uidvalidity,
            uid=uid,
            message_id=headers["message-id"],
            sender=headers["from"],
            recipients=", ".join(value for value in (headers["to"], headers["cc"]) if value),
            subject=headers["subject"],
            sent_at=headers["date"],
            received_at=received_at,
            flags=flags,
            size=size,
            body_text=body_text,
            body_truncated=body_truncated,
            attachments=attachments,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _empty_message(folder: str, uidvalidity: int, uid: int, warning: str) -> RemoteMessage:
        return RemoteMessage(
            folder=folder,
            uidvalidity=uidvalidity,
            uid=uid,
            message_id="",
            sender="",
            recipients="",
            subject="",
            sent_at="",
            received_at=0.0,
            flags=(),
            size=0,
            body_text="",
            body_truncated=False,
            attachments=(),
            warnings=(warning,),
        )

    def _select_read_only(self, folder: str) -> int:
        connection = self._require_connection()
        encoded_folder = self._mailbox_astring(folder)
        status, _data = self._protocol_call("SELECT", connection.select, encoded_folder, readonly=True)
        self._require_ok(status, "SELECT")
        status, data = self._protocol_call("UIDVALIDITY", connection.response, "UIDVALIDITY")
        if str(status).upper() != "UIDVALIDITY" or not data:
            raise MailProtocolError("IMAP UIDVALIDITY response was missing")
        raw = data[0]
        if not isinstance(raw, bytes):
            raise MailProtocolError("IMAP UIDVALIDITY response was malformed")
        numbers = re.findall(rb"\d+", raw)
        if not numbers:
            raise MailProtocolError("IMAP UIDVALIDITY response was malformed")
        uidvalidity = self._validate_uid_value(int(numbers[-1]), "UIDVALIDITY")
        self._selected_folder = folder
        self._selected_uidvalidity = uidvalidity
        return uidvalidity

    def _ensure_selected_generation(self, folder: str, uidvalidity: int) -> None:
        uidvalidity = self._validate_uid_value(uidvalidity, "UIDVALIDITY")
        current = self._selected_uidvalidity
        if self._selected_folder != folder or not current:
            current = self._select_read_only(folder)
        if current != uidvalidity:
            raise MailProtocolError("IMAP mailbox generation changed")

    def _safe_logout(self) -> None:
        connection = self._connection
        self._connection = None
        self._selected_folder = ""
        self._selected_uidvalidity = 0
        if connection is None:
            return
        try:
            connection.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    def _require_connection(self) -> Any:
        if self._connection is None:
            raise MailProtocolError("IMAP client is not connected")
        return self._connection

    @staticmethod
    def _validate_uid_value(value: object, name: str) -> int:
        if type(value) is not int or not 1 <= value <= _MAX_UID:
            raise MailProtocolError(f"IMAP {name} must be an integer between 1 and {_MAX_UID}")
        return value

    @staticmethod
    def _mailbox_astring(folder: str) -> bytes:
        encoded = encode_mailbox_name(folder)
        if encoded and all(0x21 <= byte <= 0x7E and byte not in _IMAP_ATOM_UNSAFE for byte in encoded):
            return encoded
        escaped = encoded.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
        return b'"' + escaped + b'"'

    @staticmethod
    def _protocol_call(operation: str, function: Callable[..., Any], *args: object, **kwargs: object) -> Any:
        try:
            return function(*args, **kwargs)
        except ssl.SSLError:
            raise MailTlsError("IMAP TLS connection failed") from None
        except OSError:
            raise MailNetworkError("IMAP connection failed") from None
        except imaplib.IMAP4.error:
            raise MailProtocolError(f"IMAP {operation} failed") from None

    @staticmethod
    def _require_ok(status: object, operation: str) -> None:
        value = status.decode("ascii", errors="replace") if isinstance(status, bytes) else str(status)
        if value.upper() != "OK":
            raise MailProtocolError(f"IMAP {operation} failed")

    @staticmethod
    def _parse_search_uids(data: Sequence[object] | None) -> set[int]:
        if not data or data[0] is None:
            return set()
        if not all(isinstance(item, bytes) for item in data if item is not None):
            raise MailProtocolError("IMAP UID SEARCH response was malformed")
        payload = b" ".join(item for item in data if isinstance(item, bytes)).strip()
        if not payload:
            return set()
        if re.fullmatch(rb"\d+(?:\s+\d+)*", payload):
            return {Imap163Client._validate_uid_value(int(token), "UID") for token in payload.split()}

        payload = re.sub(rb"^ESEARCH\s+", b"", payload, flags=re.IGNORECASE)
        payload = _ESEARCH_CORRELATOR.sub(b"", payload)
        payload = re.sub(rb"^UID(?:\s+|$)", b"", payload, count=1, flags=re.IGNORECASE).strip()
        if not payload:
            return set()
        if re.fullmatch(rb"COUNT\s+0", payload, re.IGNORECASE):
            return set()
        match = re.search(rb"(?:^|\s)ALL(?:\s+([0-9,:]+))?(?=\s|$)", payload, re.IGNORECASE)
        if match is None or match.group(1) is None:
            if re.fullmatch(rb"ALL", payload, re.IGNORECASE):
                return set()
            raise MailProtocolError("IMAP UID SEARCH response was malformed")
        return Imap163Client._expand_uid_sequence_set(match.group(1))

    @staticmethod
    def _expand_uid_sequence_set(value: bytes) -> set[int]:
        result: set[int] = set()
        for member in value.split(b","):
            endpoints = member.split(b":")
            if not endpoints or len(endpoints) > 2 or not all(endpoint.isdigit() for endpoint in endpoints):
                raise MailProtocolError("IMAP ESEARCH UID sequence set was malformed")
            start = Imap163Client._validate_uid_value(int(endpoints[0]), "UID")
            end = start if len(endpoints) == 1 else Imap163Client._validate_uid_value(int(endpoints[1]), "UID")
            if len(result) + abs(end - start) + 1 > _MAX_SEARCH_RESULTS:
                raise MailProtocolError("IMAP ESEARCH UID sequence set exceeded the safety limit")
            result.update(range(min(start, end), max(start, end) + 1))
        return result

    @classmethod
    def _parse_flags(cls, data: Sequence[object] | None) -> dict[int, tuple[str, ...]]:
        result: dict[int, tuple[str, ...]] = {}
        groups = cls._fetch_response_groups(data, operation="UID FETCH FLAGS")
        for group in groups:
            metadata = cls._normalize_fetch_group(group, operation="UID FETCH FLAGS").metadata
            uid, flags = cls._parse_flag_fetch_entry(metadata)
            if uid in result:
                raise MailProtocolError("IMAP UID FETCH FLAGS response was malformed")
            result[uid] = flags
        return result

    @classmethod
    def _parse_flag_fetch_entry(cls, metadata: bytes) -> tuple[int, tuple[str, ...]]:
        cls._require_complete_fetch_envelope(metadata)
        projection = cls._top_level_projection(metadata)
        uid_matches = _UID.findall(projection)
        flags_matches = [
            match
            for match in _FLAGS.finditer(metadata)
            if projection[match.start() : match.start() + len(b"FLAGS")].upper() == b"FLAGS"
        ]
        if len(uid_matches) != 1 or len(flags_matches) != 1:
            raise MailProtocolError("IMAP UID FETCH FLAGS response was malformed")
        raw_flags = flags_matches[0].group(1)
        flag_tokens = raw_flags.split()
        if any(
            not token or any(current < 0x21 or current > 0x7E or current in b'(){%*]"' for current in token)
            for token in flag_tokens
        ):
            raise MailProtocolError("IMAP UID FETCH FLAGS response was malformed")
        uid = cls._validate_uid_value(int(uid_matches[0]), "UID")
        return uid, tuple(token.decode("ascii") for token in flag_tokens)

    @staticmethod
    def _require_complete_fetch_envelope(metadata: bytes) -> None:
        start = re.match(rb"^\s*[1-9]\d*\s+\(", metadata)
        if start is None:
            raise MailProtocolError("IMAP UID FETCH FLAGS response was malformed")
        depth = 0
        quoted = False
        escaped = False
        for position in range(start.end() - 1, len(metadata)):
            current = metadata[position]
            if quoted:
                if escaped:
                    escaped = False
                elif current == ord("\\"):
                    escaped = True
                elif current == ord('"'):
                    quoted = False
                continue
            if current == ord('"'):
                quoted = True
            elif current == ord("("):
                depth += 1
            elif current == ord(")"):
                depth -= 1
                if depth < 0:
                    break
                if depth == 0:
                    if metadata[position + 1 :].strip():
                        break
                    return
        raise MailProtocolError("IMAP UID FETCH FLAGS response was malformed")

    @classmethod
    def _select_fetch_data(
        cls,
        data: Sequence[object] | None,
        expected_uid: int,
        *,
        operation: str,
        expected_section: str | None = None,
        require_bodystructure: bool = False,
    ) -> _FetchData:
        groups = tuple(
            cls._normalize_fetch_group(group, operation=operation)
            for group in cls._fetch_response_groups(data, operation=operation)
        )
        if expected_section is not None:
            section = cls._canonical_section(expected_section)
            candidates = tuple(group for group in groups if section in group.sections)
        elif require_bodystructure:
            candidates = tuple(
                group
                for group in groups
                if re.search(rb"\bBODYSTRUCTURE\b", cls._top_level_projection(group.metadata), re.IGNORECASE)
            )
        else:
            raise AssertionError("FETCH response selection requires a requested attribute")
        if len(candidates) != 1:
            raise _MessageDataError(f"IMAP {operation} response did not contain one requested payload")
        selected = candidates[0]
        cls._verify_fetch_uid(selected, expected_uid, operation=operation)
        return selected

    @classmethod
    def _fetch_response_groups(
        cls,
        data: Sequence[object] | None,
        *,
        operation: str,
    ) -> tuple[tuple[object, ...], ...]:
        groups: list[tuple[object, ...]] = []
        current_group: list[object] = []
        for item in data or []:
            if isinstance(item, bytes):
                fragment = item
            elif (
                isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], bytes) and isinstance(item[1], bytes)
            ):
                fragment = item[0]
            else:
                raise _MessageDataError(f"IMAP {operation} response contained an invalid fragment")
            starts_response = _FETCH_RESPONSE_START.match(fragment) is not None
            if starts_response and current_group:
                groups.append(tuple(current_group))
                current_group = []
            elif not current_group and not starts_response:
                raise _MessageDataError(f"IMAP {operation} response group was malformed")
            current_group.append(item)
        if current_group:
            groups.append(tuple(current_group))
        if not groups:
            raise _MessageDataError(f"IMAP {operation} response group was malformed")
        return tuple(groups)

    @classmethod
    def _normalize_fetch_group(cls, data: Sequence[object], *, operation: str) -> _FetchData:
        metadata: list[bytes] = []
        sections: dict[str, list[bytes]] = {}
        for item in data:
            if isinstance(item, bytes):
                metadata.append(item)
                continue
            if not (
                isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], bytes) and isinstance(item[1], bytes)
            ):
                raise _MessageDataError(f"IMAP {operation} response contained an invalid fragment")
            prefix, literal = item
            if _LITERAL_MARKER.search(prefix) is None:
                raise _MessageDataError(f"IMAP {operation} response literal marker was missing")
            prefix = _LITERAL_MARKER.sub(b"", prefix)
            metadata.append(prefix)
            section_match = _BODY_SECTION.search(prefix)
            if section_match is None:
                escaped = literal.replace(b"\\", b"\\\\").replace(b'"', b'\\"')
                metadata.append(b'"' + escaped + b'"')
                continue
            section = cls._canonical_section(section_match.group(1))
            sections.setdefault(section, []).append(literal)
            metadata.append(b"NIL")
        return _FetchData(
            metadata=b" ".join(metadata),
            sections={name: tuple(values) for name, values in sections.items()},
        )

    @classmethod
    def _required_section_literal(cls, parsed: _FetchData, expected: str, *, operation: str) -> bytes:
        values = parsed.sections.get(cls._canonical_section(expected), ())
        if len(values) != 1:
            raise _MessageDataError(f"IMAP {operation} response body section did not match the request")
        return values[0]

    @classmethod
    def _verify_fetch_uid(cls, parsed: _FetchData, expected: int, *, operation: str) -> None:
        projection = cls._top_level_projection(parsed.metadata)
        returned = [int(value) for value in _UID.findall(projection)]
        if returned != [expected]:
            raise _MessageDataError(f"IMAP {operation} response UID did not match the request")

    @staticmethod
    def _canonical_section(value: bytes | str) -> str:
        decoded = value.decode("ascii", errors="replace") if isinstance(value, bytes) else value
        return " ".join(decoded.upper().split())

    @staticmethod
    def _top_level_projection(metadata: bytes) -> bytes:
        projection = bytearray(b" " * len(metadata))
        depth = 0
        quoted = False
        escaped = False
        for position, current in enumerate(metadata):
            if quoted:
                if escaped:
                    escaped = False
                elif current == ord("\\"):
                    escaped = True
                elif current == ord('"'):
                    quoted = False
                continue
            if current == ord('"'):
                quoted = True
            elif current == ord("("):
                depth += 1
            elif current == ord(")"):
                depth = max(0, depth - 1)
            elif depth == 1:
                projection[position] = current
        return bytes(projection)

    @staticmethod
    def _extract_bodystructure(metadata: bytes) -> bytes:
        match = re.search(rb"\bBODYSTRUCTURE\s+", metadata, re.IGNORECASE)
        if match is None:
            raise MailProtocolError("IMAP BODYSTRUCTURE response was missing")
        start = match.end()
        while start < len(metadata) and metadata[start] in b" \t\r\n":
            start += 1
        if start >= len(metadata) or metadata[start] != ord("("):
            raise MailProtocolError("IMAP BODYSTRUCTURE response was malformed")
        depth = 0
        quoted = False
        escaped = False
        for position in range(start, len(metadata)):
            current = metadata[position]
            if quoted:
                if escaped:
                    escaped = False
                elif current == ord("\\"):
                    escaped = True
                elif current == ord('"'):
                    quoted = False
                continue
            if current == ord('"'):
                quoted = True
            elif current == ord("("):
                depth += 1
                if depth > 100:
                    raise MailProtocolError("IMAP BODYSTRUCTURE nesting exceeds the safety limit")
            elif current == ord(")"):
                depth -= 1
                if depth == 0:
                    return metadata[start : position + 1]
                if depth < 0:
                    break
        raise MailProtocolError("IMAP BODYSTRUCTURE response was unbalanced")

    @staticmethod
    def _required_integer(metadata: bytes, pattern: re.Pattern[bytes], name: str) -> int:
        match = pattern.search(metadata)
        if match is None:
            raise _MessageDataError(f"IMAP {name} response was missing")
        return int(match.group(1))

    @staticmethod
    def _flags_from_metadata(metadata: bytes) -> tuple[str, ...]:
        match = _FLAGS.search(metadata)
        if match is None:
            raise _MessageDataError("IMAP FLAGS response was missing")
        return tuple(token.decode("ascii", errors="replace") for token in match.group(1).split())

    @staticmethod
    def _parse_internaldate(metadata: bytes) -> tuple[float, tuple[str, ...]]:
        match = _INTERNALDATE.search(metadata)
        if match is None:
            return 0.0, ("INTERNALDATE was missing",)
        try:
            parsed = datetime.strptime(match.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
        except (UnicodeDecodeError, ValueError):
            return 0.0, ("INTERNALDATE could not be parsed",)
        return parsed.timestamp(), ()

    @classmethod
    def _parse_headers(cls, payload: bytes) -> tuple[dict[str, str], tuple[str, ...]]:
        cls._validate_header_literal(payload)
        parsed = BytesHeaderParser(policy=policy.compat32).parsebytes(payload)
        result: dict[str, str] = {}
        warnings: list[str] = []
        for name in ("message-id", "from", "to", "cc", "subject", "date"):
            value, value_warnings = cls._decode_header_value(str(parsed.get(name, "")))
            result[name] = value
            warnings.extend(value_warnings)
        return result, tuple(warnings)

    @staticmethod
    def _validate_header_literal(payload: bytes) -> None:
        if b"\x00" in payload:
            raise _MessageDataError("IMAP message header response was malformed")
        lines = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
        try:
            end = lines.index(b"")
        except ValueError:
            raise _MessageDataError("IMAP message header response was malformed") from None
        saw_field = False
        for line in lines[:end]:
            if line.startswith((b" ", b"\t")):
                if not saw_field:
                    raise _MessageDataError("IMAP message header response was malformed")
                continue
            name, separator, _value = line.partition(b":")
            if not separator or not name or any(byte < 33 or byte > 126 or byte == ord(":") for byte in name):
                raise _MessageDataError("IMAP message header response was malformed")
            saw_field = True

    @staticmethod
    def _decode_header_value(value: str) -> tuple[str, tuple[str, ...]]:
        fragments: list[str] = []
        warnings: list[str] = []
        try:
            decoded = decode_header(value)
        except (LookupError, ValueError):
            return value, ("MIME header could not be decoded",)
        for fragment, charset in decoded:
            if isinstance(fragment, str):
                fragments.append(fragment)
                continue
            codec = charset or "ascii"
            try:
                fragments.append(fragment.decode(codec, errors="replace"))
            except LookupError:
                fragments.append(fragment.decode("utf-8", errors="replace"))
                warnings.append("MIME header used an unknown charset")
        return "".join(fragments), tuple(warnings)

    @staticmethod
    def _preferred_body_part(parts: Sequence[BodyPart]) -> BodyPart | None:
        selectable = [part for part in parts if not Imap163Client._is_attachment(part)]
        return next(
            (
                part
                for content_type in ("text/plain", "text/html")
                for part in selectable
                if part.content_type == content_type
            ),
            None,
        )

    def _body_fetch_limit(self, part: BodyPart) -> int:
        decoded_probe = self.config.max_body_bytes + 1
        if part.transfer_encoding.strip().lower() != "base64":
            return decoded_probe
        encoded_characters = 4 * ((decoded_probe + 2) // 3)
        line_break_bytes = 2 * ((encoded_characters - 1) // 76)
        return encoded_characters + line_break_bytes + 1

    @staticmethod
    def _is_attachment(part: BodyPart) -> bool:
        return part.disposition == "attachment" or bool(part.filename)
