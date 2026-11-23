"""Read back an explicitly approved short native document after Save."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
import stat
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

from .computer_text import contains_credentials

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MAX_PACKAGE = 8 * 1024 * 1024
_MAX_XML = 512 * 1024
_MAX_TEXT = 4096


def _xml_part(package: ZipFile, name: str) -> ET.Element:
    parts = [entry for entry in package.infolist() if entry.filename == name]
    if len(parts) != 1:
        raise ValueError("ambiguous or absent document part")
    part = parts[0]
    if (part.file_size > _MAX_XML or part.flag_bits & 1
            or part.compress_type not in (ZIP_STORED, ZIP_DEFLATED)):
        raise ValueError("unsupported document part")
    with package.open(part) as document:
        xml = document.read(_MAX_XML + 1)
    if len(xml) > _MAX_XML or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)\b", xml.replace(b"\0", b""), re.I):
        raise ValueError("unsupported XML")
    return ET.fromstring(xml)


def _body_paragraphs(data: bytes) -> list[str]:
    with ZipFile(io.BytesIO(data)) as package:
        if len(package.infolist()) > 2048:
            raise ValueError("package limit")
        # Read only the local main part designated by this package. No relationship
        # target supplied by a document can select another member or resource to read.
        rels = _xml_part(package, "_rels/.rels")
        relationships = "{http://schemas.openxmlformats.org/package/2006/relationships}"
        main = [r for r in rels.findall(relationships + "Relationship")
                if r.get("Type") == "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"]
        if (rels.tag != relationships + "Relationships" or len(main) != 1
                or main[0].get("TargetMode", "Internal") != "Internal"
                or main[0].get("Target") not in {"word/document.xml", "/word/document.xml"}):
            raise ValueError("unsupported main part")
        root = _xml_part(package, "word/document.xml")
    body = root.find(_W + "body")
    if root.tag != _W + "document" or body is None:
        raise ValueError("not a Word main body")
    paragraphs: list[str] = []
    length = 0
    for paragraph in body:
        if paragraph.tag == _W + "sectPr":
            continue
        if paragraph.tag != _W + "p" or len(paragraphs) >= 64:
            raise ValueError("not a short, plain paragraph body")
        words: list[str] = []
        for run in paragraph:
            if run.tag in (_W + "pPr", _W + "bookmarkStart", _W + "bookmarkEnd", _W + "proofErr"):
                continue
            if run.tag != _W + "r":
                raise ValueError("complex paragraph")
            for child in run:
                if child.tag == _W + "rPr":
                    if child.find(_W + "vanish") is not None or child.find(_W + "webHidden") is not None:
                        raise ValueError("hidden text")
                    continue
                if child.tag == _W + "t":
                    words.append(child.text or "")
                elif child.tag in {_W + "tab", _W + "br", _W + "cr"}:
                    words.append("\t" if child.tag == _W + "tab" else "\n")
                else:
                    raise ValueError("unsupported inline content")
        text = "".join(words)
        length += len(text)
        if length > _MAX_TEXT:
            raise ValueError("body exceeds readback limit")
        paragraphs.append(text)
    if contains_credentials("\n".join(paragraphs)):
        raise ValueError("protected text")
    return paragraphs


def _version(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


@dataclass(frozen=True)
class ApprovedCopy:
    path: Path
    session: str
    grant_space: str
    parent_identity: tuple[int, int]
    origin_window: str
    origin_title: str

    @classmethod
    def bind(cls, path: str, session: str, grant_space: str, origin: tuple[str, str], *,
             plain_text: bool = False) -> ApprovedCopy | None:
        destination = Path(path)
        if (os.name != "posix" or not destination.is_absolute() or destination.suffix.lower() != (".txt" if plain_text else ".docx")):
            return None
        try:
            parent = destination.parent
            if parent.resolve(strict=True) != parent:
                return None
            directory = parent.lstat()
            if not stat.S_ISDIR(directory.st_mode):
                return None
        except (OSError, ValueError, RuntimeError):
            return None
        return cls(destination, session, grant_space, (directory.st_dev, directory.st_ino), *origin)

    def matches(self, window: str, title: str, catalog_path: str) -> bool:
        if not window or title != self.path.name:
            return False
        if catalog_path:
            try:
                native = Path(catalog_path)
                return native.is_absolute() and native.parent.resolve(strict=True) / native.name == self.path
            except (OSError, ValueError, RuntimeError):
                return False
        if self.path.suffix.lower() == ".txt":
            return False
        # Some WPS releases omit AXDocument. The same native AX + WindowServer
        # identity must have changed from a different document name to this copy.
        return window == self.origin_window and bool(self.origin_title) and self.origin_title != title

    @contextlib.contextmanager
    def _directory(self):
        if sys.platform == "win32":
            raise NotImplementedError("native saved-document readback requires POSIX directory descriptors")
        parent = self.path.parent
        fd = os.open(parent, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        try:
            identity = os.fstat(fd)
            if (identity.st_dev, identity.st_ino) != self.parent_identity or parent.resolve(strict=True) != parent:
                raise ValueError("directory changed")
            yield fd
            if not os.path.samestat(parent.lstat(), identity):
                raise ValueError("directory changed")
        finally:
            os.close(fd)

    def _read(self) -> tuple[tuple[int, int, int, int, int], bytes]:
        if sys.platform == "win32":
            raise NotImplementedError("native saved-document readback requires POSIX file flags")
        # Anchor all leaf lookups in the directory approved before creation.
        with contextlib.ExitStack() as stack:
            directory = stack.enter_context(self._directory())
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(self.path.name, flags, dir_fd=directory)
            source = stack.enter_context(os.fdopen(fd, "rb"))
            before = os.fstat(source.fileno())
            plain_text = self.path.suffix.lower() == ".txt"
            limit = 4 * _MAX_TEXT if plain_text else _MAX_PACKAGE
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_size > limit or (not plain_text and not before.st_size)):
                raise ValueError("unsupported document file")
            data = source.read(limit + 1)
            named = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            if (len(data) != before.st_size or _version(named) != _version(before)
                    or _version(os.fstat(source.fileno())) != _version(before)):
                raise ValueError("document changed during readback")
        return _version(before), data

    def version(self) -> tuple[int, int, int, int, int] | None:
        try:
            with self._directory() as directory:
                st = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                    return _version(st)
        except (OSError, ValueError, RuntimeError):
            pass
        return None

    def after_save(self, before: tuple[int, int, int, int, int] | None, *, wait: bool = True) -> dict:
        # Save can return before Word finishes its atomic replacement. Poll only
        # the authorized file, never the app or a directory listing.
        proof = {"path": str(self.path), "status": "unverified", "missing": "stable_saved_file",
                 "next_step": "Only the Save input was confirmed. Check the outstanding document evidence without repeating Save."}
        if before is None:
            return proof
        try:
            deadline = time.monotonic() + (0.6 if wait else 0)
            current = self.version()
            while current == before and time.monotonic() < deadline:
                time.sleep(0.05)
                current = self.version()
            if current == before:
                return self._waiting_for_update()
            current, data = self._read()
            if current == before:
                return self._waiting_for_update()
            if self.path.suffix.lower() == ".txt":
                proof["missing"] = "complete_short_plain_text"
                # Decode bytes directly to preserve the BOM and the exact CR/LF sequences.
                text = data.decode("utf-8")
                if (len(text) > _MAX_TEXT or contains_credentials(text)
                        or any(ord(char) < 32 and char not in "\r\n\t" for char in text)):
                    return proof
                return {"path": str(self.path), "status": "read_back", "evidence": "saved_plain_text",
                        "version_changed_since_save_started": True, "sha256": hashlib.sha256(data).hexdigest(),
                        "encoding": "utf-8", "byte_count": len(data), "line_count": len(text.splitlines()),
                        "text": text, "next_step": "Compare these exact on-disk bytes and line breaks with the requested edit. "
                        "This read proves the file at this point only. If you interact with it again, verify it again before reporting."}
            proof["missing"] = "complete_short_main_body"
            paragraphs = _body_paragraphs(data)
        except (BadZipFile, EOFError, ET.ParseError, zlib.error):
            # The file can have a new stat version before an in-place ZIP/XML write is complete.
            return {**proof, "missing": "saved_file_update", "reason": "incomplete_package",
                    "next_step": "The updated document is not readable yet. Observe it again with "
                                 "computer_snapshot(settle_ms=300). Do not repeat the edit or Save."}
        except (OSError, ValueError, RuntimeError, NotImplementedError):
            return proof
        return {"path": str(self.path), "status": "read_back", "evidence": "saved_docx_main_body",
                "version_changed_since_save_started": True,
                "sha256": hashlib.sha256(data).hexdigest(), "paragraphs": paragraphs,
                "next_step": "Compare these on-disk paragraphs and path with the user's request. "
                             "If they satisfy the requested edit and no other work remains, reply now. "
                             "If they differ, report or correct only that difference."}

    def _waiting_for_update(self) -> dict:
        return {"path": str(self.path), "status": "unverified", "missing": "saved_file_update",
                "next_step": "Save was acknowledged. A newer file version has not appeared. "
                             "Observe this document with computer_snapshot(settle_ms=300) to check again. "
                             "If it is still unchanged, report the pending save. Do not repeat the edit or Save."}


def is_document_save(actions, context, tree) -> bool:
    """Recognize a single Save command on the observed document or its File menu."""
    if len(actions) != 1 or not isinstance(tree, Mapping):
        return False
    action = actions[0]
    document = tree.get("role") == "AXWindow" and tree.get("subrole") == "AXStandardWindow"
    if action.type == "keypress":
        return document and action.key == "s" and action.modifiers == ("command",)
    file_menu = (tree.get("role") in {"AXMenuBarItem", "AXMenu"}
                 and str(tree.get("title") or tree.get("label") or "").casefold() in {"file", "文件", "檔案", "档案"})
    if not (document or file_menu) or action.type != "click" or not action.element_ref or action.modifiers:
        return False
    element = context.get("elements", {}).get(action.element_ref) or {}
    return (element.get("role") in {"AXButton", "AXMenuItem"}
            and str(element.get("title") or element.get("label") or "").casefold() in {"save", "保存", "儲存", "储存"})
