"""Markdown documents that the agent writes section by section.

A document is an ordinary Markdown file that people can keep editing in any
editor. Sections are ATX headings; the first level-1 heading is the title.
The agent keeps a stable section id in an invisible HTML comment directly
above a heading, and a section that is still to be written carries a visible
placeholder line::

    <!-- astra:section id="background" status="pending" intent="Why rules fail" -->
    ## 1 Background
    *Pending: Why rules fail*

Reviewers leave instructions as ``<!-- @astra: shorten this paragraph -->``.
``doc_comments`` lists them and ``doc_resolve_comment`` removes one once it has
been handled. Nothing inside fenced code blocks is treated as a heading,
marker or comment.
"""

from __future__ import annotations

import bisect
import hashlib
import html
import json
import logging
import os
import re
import tempfile
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..file_checkpoints import FileCheckpointStore, PendingCheckpoint
from ..tool_failure import ToolFailure
from .files import FilesystemGrant, FilesystemPolicy, _note_turn_capture
from .registry import ToolDef, ToolRegistry, approval_justification_schema

logger = logging.getLogger(__name__)

DOCUMENT_SUFFIXES = (".md", ".markdown")
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
LEAD_ID = "_lead"
_MAX_HEADING_CHARS = 200
_MAX_INTENT_CHARS = 300
_CONTEXT_CHARS = 160

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_MARKER = re.compile(r"^[ \t]*<!--[ \t]*astra:section\b(?P<attrs>[^\n]*?)-->[ \t]*$")
_ATTR = re.compile(r'([a-z_]+)="([^"]*)"')
_COMMENT = re.compile(r"<!--[ \t]*@astra\b[ \t]*:?(?P<text>.*?)-->", re.DOTALL)
_ID_PATTERN = re.compile(r"^\w[\w.-]{0,63}$")
_WORDS = re.compile(r"[㐀-鿿豈-﫿]|[^\W_]+(?:['’-][^\W_]+)*")


class DocumentError(ValueError):
    """A request the document cannot satisfy; ``code`` names the reason."""

    def __init__(self, code: str, message: str, hint: str = "", **details: Any):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.details = details


@dataclass(frozen=True)
class Section:
    id: str
    heading: str
    level: int
    marked: bool
    marker_status: str
    intent: str
    start: int
    heading_line: int
    body_end: int
    end: int
    hash: str
    words: int
    pristine_pending: bool

    @property
    def status(self) -> str:
        return "pending" if self.pristine_pending else "written"


@dataclass(frozen=True)
class Comment:
    id: str
    text: str
    start: int
    end: int
    line: int
    section_id: str | None
    context: str


@dataclass(frozen=True)
class Document:
    lines: list[str]
    newline: str
    title: str | None
    title_line: int | None
    lead_start: int
    lead_end: int
    sections: list[Section]
    comments: list[Comment]
    warnings: list[str]

    @property
    def text(self) -> str:
        return "".join(self.lines)

    def section(self, section_id: str) -> Section:
        for item in self.sections:
            if item.id == section_id:
                return item
        known = ", ".join(item.id for item in self.sections) or "none"
        raise DocumentError(
            "section_not_found",
            f"No section with id '{section_id}'. Known ids: {known}",
            "Call doc_outline to see the current section ids.",
        )

    @property
    def lead_hash(self) -> str:
        return _hash(self.lines[self.lead_start:self.lead_end])

    @property
    def lead_words(self) -> int:
        return _count_words(self.lines[self.lead_start:self.lead_end])


def _hash(lines: list[str]) -> str:
    """Hash section text; trailing blank lines are spacing, not content."""
    end = len(lines)
    while end and _is_blank(lines[end - 1]):
        end -= 1
    text = "".join(lines[:end]).replace("\r\n", "\n").rstrip("\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _count_words(lines: list[str]) -> int:
    count = 0
    for line in lines:
        if _MARKER.match(line):
            continue
        count += len(_WORDS.findall(_COMMENT.sub("", line)))
    return count


def _strip_newline(line: str) -> str:
    return line.rstrip("\r\n")


def _is_blank(line: str) -> bool:
    return not line.strip()


def _fence_close(line: str, fence: str) -> bool:
    body = _strip_newline(line)
    indent = len(body) - len(body.lstrip(" "))
    stripped = body.strip()
    return (
        indent <= 3
        and len(stripped) >= len(fence)
        and set(stripped) == {fence[0]}
    )


def fenced_lines(lines: list[str]) -> list[bool]:
    """Mark lines that belong to fenced code blocks, fences included."""
    inside: list[bool] = []
    fence = ""
    for line in lines:
        if fence:
            inside.append(True)
            if _fence_close(line, fence):
                fence = ""
            continue
        match = _FENCE_OPEN.match(line)
        if match:
            fence = match.group(1)
            inside.append(True)
            continue
        inside.append(False)
    return inside


def heading_of(line: str) -> tuple[int, str] | None:
    match = _HEADING.match(_strip_newline(line))
    if not match:
        return None
    return len(match.group(1)), (match.group(2) or "").strip()


def _marker_attrs(line: str) -> dict[str, str] | None:
    match = _MARKER.match(_strip_newline(line))
    if not match:
        return None
    return {key: html.unescape(value) for key, value in _ATTR.findall(match.group("attrs"))}


def _escape_attr(value: str) -> str:
    # ``--`` may not appear inside an HTML comment.
    return html.escape(value, quote=True).replace("--", "&#45;&#45;")


def marker_line(section_id: str, *, pending: bool = False, intent: str = "") -> str:
    attrs = [f'id="{_escape_attr(section_id)}"']
    if pending:
        attrs.append('status="pending"')
        if intent:
            attrs.append(f'intent="{_escape_attr(intent)}"')
    return f"<!-- astra:section {' '.join(attrs)} -->"


def placeholder(intent: str) -> str:
    cleaned = intent.replace("*", "").replace("\n", " ").strip()
    return f"*Pending: {cleaned}*" if cleaned else "*Pending*"


def slugify(text: str) -> str:
    slug = re.sub(r"[^\w]+", "-", text.lower()).strip("-_")
    return slug[:64].strip("-") or "section"


def _unique(candidate: str, used: set[str]) -> str:
    base = candidate
    counter = 2
    while candidate in used:
        suffix = f"-{counter}"
        candidate = base[: 64 - len(suffix)] + suffix
        counter += 1
    used.add(candidate)
    return candidate


def _detect_newline(text: str) -> str:
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"


def _line_starts(lines: list[str]) -> list[int]:
    starts: list[int] = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    return starts


def parse_document(text: str) -> Document:
    """Parse Markdown into title, lead, sections and review comments."""
    lines = text.splitlines(keepends=True)
    newline = _detect_newline(text)
    fenced = fenced_lines(lines)
    warnings: list[str] = []

    headings: list[tuple[int, int, str, dict[str, str] | None, int | None]] = []
    pending_marker: tuple[int, dict[str, str]] | None = None
    for index, line in enumerate(lines):
        if fenced[index]:
            pending_marker = None
            continue
        attrs = _marker_attrs(line)
        if attrs is not None:
            if pending_marker is not None:
                warnings.append(f"Line {pending_marker[0] + 1}: section marker is not followed by a heading.")
            pending_marker = (index, attrs)
            continue
        found = heading_of(line)
        if found is not None:
            level, heading = found
            marker_index = pending_marker[0] if pending_marker else None
            headings.append((index, level, heading, pending_marker[1] if pending_marker else None, marker_index))
            pending_marker = None
            continue
        if not _is_blank(line) and pending_marker is not None:
            warnings.append(f"Line {pending_marker[0] + 1}: section marker is not followed by a heading.")
            pending_marker = None
    if pending_marker is not None:
        warnings.append(f"Line {pending_marker[0] + 1}: section marker is not followed by a heading.")

    title: str | None = None
    title_line: int | None = None
    if headings and headings[0][1] == 1 and headings[0][3] is None:
        title_line, title = headings[0][0], headings[0][2]
        headings = headings[1:]

    used: set[str] = {LEAD_ID}
    ids: list[str] = []
    for line_index, _level, heading, attrs, _marker in headings:
        requested = (attrs or {}).get("id", "").strip()
        if requested and _ID_PATTERN.match(requested):
            if requested in used:
                warnings.append(f"Line {line_index + 1}: duplicate section id '{requested}' was renamed.")
            ids.append(_unique(requested, used))
        else:
            if requested:
                warnings.append(f"Line {line_index + 1}: invalid section id '{requested}' was replaced.")
            ids.append(_unique(slugify(heading), used))

    starts = [marker if marker is not None else line for line, _, _, _, marker in headings]
    sections: list[Section] = []
    for position, (line_index, level, heading, attrs, _marker) in enumerate(headings):
        end = len(lines)
        body_end = len(lines)
        for later in range(position + 1, len(headings)):
            if body_end == len(lines):
                body_end = starts[later]
            if headings[later][1] <= level:
                end = starts[later]
                break
        # Pending means the section's own text is still the placeholder; its
        # sub-sections are separate sections with their own status.
        body = lines[line_index + 1:body_end]
        marker_status = (attrs or {}).get("status", "")
        intent = (attrs or {}).get("intent", "")
        meaningful = [_strip_newline(item).strip() for item in body if not _is_blank(item)]
        pristine = marker_status == "pending" and (
            not meaningful or meaningful == [placeholder(intent)]
        )
        sections.append(Section(
            id=ids[position],
            heading=heading,
            level=level,
            marked=attrs is not None,
            marker_status=marker_status,
            intent=intent,
            start=starts[position],
            heading_line=line_index,
            body_end=body_end,
            end=end,
            hash=_hash(lines[starts[position]:end]),
            words=_count_words(lines[line_index + 1:body_end]),
            pristine_pending=pristine,
        ))

    lead_start = title_line + 1 if title_line is not None else 0
    lead_end = sections[0].start if sections else len(lines)
    comments = _parse_comments(lines, fenced, sections)
    return Document(
        lines=lines,
        newline=newline,
        title=title,
        title_line=title_line,
        lead_start=lead_start,
        lead_end=lead_end,
        sections=sections,
        comments=comments,
        warnings=warnings,
    )


def _parse_comments(lines: list[str], fenced: list[bool], sections: list[Section]) -> list[Comment]:
    masked = "".join(
        re.sub(r"[^\r\n]", " ", line) if fenced[index] else line
        for index, line in enumerate(lines)
    )
    matches = list(_COMMENT.finditer(masked))
    # Blank every comment (keeping line breaks) so context never quotes comment syntax.
    blanked = list(masked)
    for match in matches:
        for position in range(match.start(), match.end()):
            if blanked[position] not in "\r\n":
                blanked[position] = " "
    blanked_lines = "".join(blanked).splitlines(keepends=True)
    starts = _line_starts(lines)
    seen: dict[str, int] = {}
    comments: list[Comment] = []
    for match in matches:
        text = " ".join(match.group("text").split())
        occurrence = seen.get(text, 0)
        seen[text] = occurrence + 1
        digest = hashlib.sha256(f"{text}#{occurrence}".encode()).hexdigest()[:8]
        first = max(0, bisect.bisect_right(starts, match.start()) - 1)
        last = max(first, bisect.bisect_right(starts, match.end() - 1) - 1)
        owner = None
        for section in sections:
            if section.start <= first < section.end:
                owner = section.id  # later sections nest inside earlier ones, so the innermost wins
        comments.append(Comment(
            id=f"c{digest}",
            text=text,
            start=match.start(),
            end=match.end(),
            line=first + 1,
            section_id=owner,
            context=_comment_context(lines, blanked_lines, first, last),
        ))
    return comments


def _comment_context(lines: list[str], blanked_lines: list[str], first: int, last: int) -> str:
    own = " ".join("".join(blanked_lines[first:last + 1]).split())
    if own:
        return own[:_CONTEXT_CHARS]
    for index in range(first - 1, -1, -1):
        if _MARKER.match(lines[index]):
            continue
        candidate = " ".join(blanked_lines[index].split())
        if candidate:
            return candidate[:_CONTEXT_CHARS]
    return ""


def _normalize_heading(value: str) -> str:
    heading = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    heading = re.sub(r"^#+\s*", "", heading).strip()
    if not heading:
        raise DocumentError("invalid_heading", "Section heading must not be empty.")
    if len(heading) > _MAX_HEADING_CHARS:
        raise DocumentError("invalid_heading", f"Section heading is longer than {_MAX_HEADING_CHARS} characters.")
    return heading


def _normalize_intent(value: str) -> str:
    intent = " ".join(str(value or "").split())
    return intent[:_MAX_INTENT_CHARS]


def _check_body(content: str, level: int, *, lead: bool = False) -> tuple[str | None, str]:
    """Validate body text; return (heading taken from its first line, body).

    A section body may repeat its own heading on the first line (it is taken as
    the heading) and may contain deeper headings. A heading at the section's
    level or above would silently end the section, so it is refused. The lead
    under the title may not contain headings at all.
    """
    body_lines = content.replace("\r\n", "\n").strip("\n").split("\n") if content.strip() else []
    fenced = fenced_lines([item + "\n" for item in body_lines])
    taken: str | None = None
    for index, line in enumerate(body_lines):
        if fenced[index]:
            continue
        if _marker_attrs(line) is not None:
            raise DocumentError(
                "invalid_content",
                "Content must not contain astra:section markers; the document tools manage them.",
            )
        found = heading_of(line)
        if found is None:
            continue
        found_level, found_text = found
        if lead:
            raise DocumentError(
                "invalid_content",
                f"The lead may not contain headings (line {index + 1}).",
                "Put headed text in a new section with doc_edit action=add.",
            )
        leading = all(_is_blank(item) for item in body_lines[:index])
        if found_level == level and leading and taken is None:
            taken = found_text
            continue
        if found_level <= level:
            raise DocumentError(
                "invalid_content",
                (
                    f"Content line {index + 1} is a level-{found_level} heading, which would "
                    f"end this level-{level} section."
                ),
                "Use deeper headings inside a section, or doc_edit action=add for a new section.",
            )
    if taken is not None:
        first = next(index for index, line in enumerate(body_lines) if not _is_blank(line))
        body_lines = body_lines[first + 1:]
    return taken, "\n".join(body_lines).strip("\n")


def _render_section(
    section_id: str,
    level: int,
    heading: str,
    body: str,
    *,
    pending: bool,
    intent: str,
    newline: str,
) -> list[str]:
    parts = [marker_line(section_id, pending=pending, intent=intent), f"{'#' * level} {heading}", ""]
    text = placeholder(intent) if pending else body
    if text:
        parts.extend(text.split("\n"))
    return [part + newline for part in parts]


def _ends_with_newline(line: str) -> bool:
    return line.endswith(("\n", "\r"))


def _finish(lines: list[str], newline: str) -> list[str]:
    """End the file with exactly one newline."""
    trimmed = list(lines)
    while trimmed and _is_blank(trimmed[-1]):
        trimmed.pop()
    if trimmed and not _ends_with_newline(trimmed[-1]):
        trimmed[-1] += newline
    return trimmed


def _splice(head: list[str], block: list[str], tail: list[str], newline: str) -> list[str]:
    """Join runs of lines with exactly one blank line at each seam.

    Only the seams change, so every untouched section keeps its text (and its
    hash, which ignores trailing blank lines).
    """
    joined: list[str] = []
    for part in (head, block, tail):
        if not part:
            continue
        if not joined:
            joined.extend(part)
            continue
        while joined and _is_blank(joined[-1]):
            joined.pop()
        if joined and not _ends_with_newline(joined[-1]):
            joined[-1] += newline
        start = 0
        while start < len(part) and _is_blank(part[start]):
            start += 1
        if joined:
            joined.append(newline)
        joined.extend(part[start:])
    return _finish(joined, newline)


def _section_block(item: dict[str, Any], used: set[str]) -> list[str]:
    section_heading = _normalize_heading(str(item.get("heading", "")))
    level = int(item.get("level") or 2)
    if not 2 <= level <= 6:
        raise DocumentError("invalid_level", "Section level must be between 2 and 6.")
    requested = str(item.get("id") or "").strip()
    if requested and not _ID_PATTERN.match(requested):
        raise DocumentError("invalid_id", f"Invalid section id '{requested}'.", "Use letters, digits, '-', '_' or '.'.")
    if requested in used:
        raise DocumentError("invalid_id", f"Section id '{requested}' is used twice.")
    section_id = _unique(requested or slugify(section_heading), used)
    intent = _normalize_intent(str(item.get("intent", "")))
    content = str(item.get("content") or "")
    taken, body = _check_body(content, level) if content.strip() else (None, "")
    if taken and taken != section_heading:
        raise DocumentError("invalid_content", f"Content for '{section_heading}' starts with a different heading.")
    return _render_section(section_id, level, section_heading, body, pending=not body, intent=intent, newline="\n")


def build_document(title: str, lead: str, sections: list[dict[str, Any]]) -> str:
    """Create the Markdown for a new document."""
    lines: list[str] = [f"# {_normalize_heading(title)}\n"]
    if lead.strip():
        _, lead_body = _check_body(lead, 1, lead=True)
        lines = _splice(lines, [line + "\n" for line in lead_body.split("\n")], [], "\n")
    used: set[str] = {LEAD_ID}
    for item in sections:
        lines = _splice(lines, _section_block(item, used), [], "\n")
    return "".join(_finish(lines, "\n"))


def replace_section(document: Document, section_id: str, content: str, heading: str | None = None) -> list[str]:
    """Return new lines with one section's heading and body replaced."""
    newline = document.newline
    if section_id == LEAD_ID:
        _, body = _check_body(content, 1, lead=True)
        block = [line + newline for line in body.split("\n")] if body else []
        return _splice(document.lines[:document.lead_start], block, document.lines[document.lead_end:], newline)
    section = document.section(section_id)
    taken, body = _check_body(content, section.level)
    new_heading = _normalize_heading(heading) if heading else None
    if taken and new_heading and taken != new_heading:
        raise DocumentError(
            "invalid_content",
            "The content starts with a heading that differs from the heading argument.",
            "Pass the new heading only once.",
        )
    if not body:
        raise DocumentError(
            "invalid_content",
            "Section content is empty.",
            "Write the section text with doc_edit action=write, or delete it with action=remove.",
        )
    if section.body_end < section.end and _contains_heading(body):
        raise DocumentError(
            "invalid_content",
            f"Section '{section.id}' has sub-sections ({', '.join(subsection_ids(document, section))}); "
            "its own text cannot add headings.",
            "Write each sub-section with its own section_id, or add one with doc_edit action=add.",
        )
    block = _render_section(
        section.id, section.level, new_heading or taken or section.heading, body,
        pending=False, intent="", newline=newline,
    )
    # Only the section's own text is replaced; its sub-sections stay in place.
    return _splice(document.lines[:section.start], block, document.lines[section.body_end:], newline)


def _contains_heading(text: str) -> bool:
    lines = [item + "\n" for item in text.split("\n")]
    fenced = fenced_lines(lines)
    return any(not fenced[index] and heading_of(line.rstrip("\n")) is not None for index, line in enumerate(lines))


def insert_section(
    document: Document,
    heading: str,
    *,
    content: str = "",
    intent: str = "",
    level: int | None = None,
    after: str | None = None,
    before: str | None = None,
    section_id: str | None = None,
) -> tuple[list[str], str]:
    """Return new lines with a section inserted, and the new section id."""
    if after and before:
        raise DocumentError("invalid_position", "Pass either after or before, not both.")
    anchor = document.section(after or before or "") if (after or before) else None
    new_level = level or (anchor.level if anchor else 2)
    if not 2 <= new_level <= 6:
        raise DocumentError("invalid_level", "Section level must be between 2 and 6.")
    new_heading = _normalize_heading(heading)
    used = {item.id for item in document.sections} | {LEAD_ID}
    requested = (section_id or "").strip()
    if requested:
        if not _ID_PATTERN.match(requested):
            raise DocumentError("invalid_id", f"Invalid section id '{requested}'.", "Use letters, digits, '-', '_' or '.'.")
        if requested in used:
            raise DocumentError("invalid_id", f"Section id '{requested}' already exists.")
    new_id = _unique(requested or slugify(new_heading), used)
    taken, body = _check_body(content, new_level) if content.strip() else (None, "")
    if taken and taken != new_heading:
        raise DocumentError("invalid_content", "The content starts with a heading that differs from the heading argument.")
    block = _render_section(
        new_id, new_level, new_heading, body,
        pending=not body, intent=_normalize_intent(intent), newline=document.newline,
    )
    if anchor is None:
        index = len(document.lines)
    elif after:
        index = anchor.end
    else:
        index = anchor.start
    return _splice(document.lines[:index], block, document.lines[index:], document.newline), new_id


def remove_section(document: Document, section_id: str) -> list[str]:
    section = document.section(section_id)
    return _splice(document.lines[:section.start], [], document.lines[section.end:], document.newline)


def remove_comment(document: Document, comment_id: str) -> list[str]:
    comment = next((item for item in document.comments if item.id == comment_id), None)
    if comment is None:
        known = ", ".join(item.id for item in document.comments) or "none"
        raise DocumentError(
            "comment_not_found",
            f"No open comment with id '{comment_id}'. Open comments: {known}",
            "Call doc_outline to list the current comments.",
        )
    text = document.text
    starts = _line_starts(document.lines)
    first = comment.line - 1
    last = max(0, bisect.bisect_right(starts, comment.end - 1) - 1)
    line_start = starts[first]
    line_end = starts[last] + len(document.lines[last])
    if not text[line_start:comment.start].strip() and not text[comment.end:line_end].strip():
        head = text[:line_start].splitlines(keepends=True)
        tail = text[line_end:].splitlines(keepends=True)
        if head and tail and _is_blank(head[-1]) and _is_blank(tail[0]):
            tail = tail[1:]
        return _finish(head + tail, document.newline)
    left = text[:comment.start].rstrip(" \t")
    right = text[comment.end:].lstrip(" \t")
    gap = " " if left and not _ends_with_newline(left) and right and not right.startswith(("\n", "\r")) else ""
    return _finish((left + gap + right).splitlines(keepends=True), document.newline)


def section_view(section: Section) -> dict[str, Any]:
    view: dict[str, Any] = {
        "id": section.id,
        "heading": section.heading,
        "level": section.level,
        "status": section.status,
        "words": section.words,
        "hash": section.hash,
    }
    if not section.marked:
        view["stable_id"] = False
    if section.pristine_pending and section.intent:
        view["intent"] = section.intent
    return view


def outline_view(path: Path, document: Document, sha256: str) -> dict[str, Any]:
    view: dict[str, Any] = {
        "path": str(path),
        "title": document.title,
        "sha256": sha256,
        "lead": {"id": LEAD_ID, "words": document.lead_words, "hash": document.lead_hash},
        "sections": [section_view(item) for item in document.sections],
        "pending_sections": sum(item.pristine_pending for item in document.sections),
        "open_comments": len(document.comments),
    }
    if document.warnings:
        view["warnings"] = document.warnings
    return view


def section_markdown(document: Document, section: Section) -> str:
    """The section's heading and own text, i.e. what a write replaces; sub-sections excluded."""
    return "".join(document.lines[section.heading_line:section.body_end]).replace("\r\n", "\n").rstrip("\n")


def subsection_ids(document: Document, section: Section) -> list[str]:
    return [item.id for item in document.sections if section.start < item.start < section.end]


def comment_views(document: Document) -> list[dict[str, Any]]:
    return [
        {"id": item.id, "text": item.text, "section_id": item.section_id, "line": item.line, "context": item.context}
        for item in document.comments
    ]


def register_document_tools(
    registry: ToolRegistry,
    *,
    policy: FilesystemPolicy | None = None,
    workdir: str = ".",
) -> None:
    """Register the ``documents`` tool group; file tools should be registered first."""
    access = policy or registry.filesystem_policy or FilesystemPolicy.load(workdir)
    if not isinstance(access, FilesystemPolicy):
        raise TypeError("Document tools need a FilesystemPolicy")
    checkpoints = FileCheckpointStore(access.workspace)
    session_grants: list[FilesystemGrant] = []

    def permission_check(operation: str, *, write: bool) -> Callable[[dict], dict | None]:
        def check(args: dict) -> dict | None:
            return access.permission_request(str(args.get("path") or ""), write=write, operation=operation)
        return check

    def permission_grant(args: dict, request: dict, decision: str):
        del args
        mode = "rw" if request.get("access") == "write" else "ro"
        grant = access.grant(str(request["target"]), mode)
        if decision == "once":
            return lambda: access.revoke(grant)
        session_grants.append(grant)
        return None

    def cleanup_session_grants(session_id: str, reason: str) -> None:
        del session_id, reason
        for grant in reversed(session_grants):
            access.revoke(grant)
        session_grants.clear()

    registry.hooks.on_session_end(cleanup_session_grants)

    def failure(code: str, message: str, hint: str = "", *, retryable: bool = True, **details: Any) -> ToolFailure:
        return ToolFailure(code=code, message=message, retryable=retryable, recovery_hint=hint, details=details)

    def resolve(path: str, *, write: bool, must_exist: bool = True) -> Path:
        target = access.resolve(path, write=write)
        if target.suffix.lower() not in DOCUMENT_SUFFIXES:
            raise DocumentError(
                "not_markdown",
                f"Document tools work on Markdown files (.md or .markdown): {target}",
                "Use the file tools for other formats, or doc_export to produce Word, PDF or HTML.",
            )
        if must_exist and not target.is_file():
            raise DocumentError("doc_not_found", f"Document does not exist: {target}", "Create it with doc_create first.")
        return target

    def load(target: Path) -> tuple[Document, str]:
        size = target.stat().st_size
        if size > MAX_DOCUMENT_BYTES:
            raise DocumentError(
                "doc_too_large",
                f"Document is {size} bytes; document tools accept up to {MAX_DOCUMENT_BYTES} bytes.",
                "Split the document or edit it with edit_file or apply_patch.",
            )
        raw = target.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentError("not_utf8", f"Document is not UTF-8 text: {target}") from exc
        return parse_document(text), hashlib.sha256(raw).hexdigest()

    def capture(target: Path, operation: str, task_id: str) -> PendingCheckpoint | None:
        try:
            pending = checkpoints.capture([target], operation=operation, task_id=task_id)
        except Exception:
            logger.exception("Failed to capture document checkpoint for %s", operation)
            return None
        _note_turn_capture(pending, "")
        return pending

    def finalize(pending: PendingCheckpoint | None) -> str:
        try:
            checkpoint_id = checkpoints.finalize(pending)
        except Exception:
            logger.exception("Failed to finalize document checkpoint")
            return ""
        if checkpoint_id:
            _note_turn_capture(pending, checkpoint_id)
        return checkpoint_id

    def commit(target: Path, lines: list[str], *, expected_sha256: str | None, operation: str, task_id: str) -> tuple[str, str]:
        """Atomically replace the document if nobody changed it since it was read."""
        content = "".join(lines)
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_DOCUMENT_BYTES:
            raise DocumentError("doc_too_large", f"The edited document would exceed {MAX_DOCUMENT_BYTES} bytes.")
        if expected_sha256 is not None:
            current = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else ""
            if current != expected_sha256:
                raise DocumentError(
                    "file_changed",
                    f"The document changed while this edit was prepared: {target}",
                    "Call doc_outline again and redo the edit against the current text.",
                )
        elif target.exists():
            raise DocumentError("doc_exists", f"Document already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = capture(target, operation, task_id)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent, prefix=f".{target.name}.agent-", suffix=".tmp", delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return hashlib.sha256(encoded).hexdigest(), finalize(pending)

    def run(action: Callable[[], dict[str, Any]]) -> str | ToolFailure:
        try:
            return json.dumps(action(), ensure_ascii=False)
        except DocumentError as exc:
            return failure(exc.code, str(exc), exc.hint, **exc.details)
        except PermissionError as exc:
            return failure("permission_denied", str(exc), "Choose a writable path inside the workspace.", retryable=False)
        except (OSError, ValueError) as exc:
            return failure("document_error", str(exc))

    def guard(section: Section, expected_hash: str, action: str, *, untouched: bool | None = None) -> None:
        untouched = section.pristine_pending if untouched is None else untouched
        if untouched and not expected_hash:
            return
        if not expected_hash:
            raise DocumentError(
                "hash_required",
                f"Section '{section.id}' or its sub-sections already have content; {action} needs its expected_hash."
                if section.pristine_pending else
                f"Section '{section.id}' already has content; {action} needs its expected_hash.",
                "Call doc_outline with this section_id, check its current text, then pass its hash.",
                current_hash=section.hash,
            )
        if expected_hash.strip().lower() != section.hash:
            raise DocumentError(
                "section_changed",
                f"Section '{section.id}' changed since hash {expected_hash} was read (now {section.hash}).",
                "Someone edited this section. Read it again with doc_outline and redo the edit on the current text.",
                current_hash=section.hash,
            )

    def _doc_create(
        path: str,
        title: str,
        sections: list[dict[str, Any]],
        lead: str = "",
        _task_id: str = "",
    ) -> str | ToolFailure:
        def action() -> dict[str, Any]:
            target = resolve(path, write=True, must_exist=False)
            if target.exists():
                raise DocumentError(
                    "doc_exists",
                    f"Document already exists: {target}",
                    "Continue it with doc_outline and doc_edit, or choose a new path.",
                )
            text = build_document(title, lead, sections)
            document = parse_document(text)
            sha256, checkpoint_id = commit(
                target, document.lines, expected_sha256=None, operation="doc_create", task_id=_task_id,
            )
            return {**outline_view(target, document, sha256), "status": "created", "checkpoint_id": checkpoint_id}
        return run(action)

    def _doc_outline(path: str, section_id: str = "") -> str | ToolFailure:
        def action() -> dict[str, Any]:
            target = resolve(path, write=False)
            document, sha256 = load(target)
            view = outline_view(target, document, sha256)
            view["comments"] = comment_views(document)
            if section_id == LEAD_ID:
                view["section"] = {
                    "id": LEAD_ID,
                    "hash": document.lead_hash,
                    "markdown": "".join(document.lines[document.lead_start:document.lead_end]).strip("\r\n"),
                }
            elif section_id:
                section = document.section(section_id)
                view["section"] = {**section_view(section), "markdown": section_markdown(document, section),
                                   "subsections": subsection_ids(document, section)}
            return view
        return run(action)

    def _doc_write_section(
        path: str,
        section_id: str,
        content: str,
        heading: str = "",
        expected_hash: str = "",
        _task_id: str = "",
    ) -> str | ToolFailure:
        def action() -> dict[str, Any]:
            target = resolve(path, write=True)
            document, sha256 = load(target)
            if section_id == LEAD_ID:
                if expected_hash.strip().lower() not in ("", document.lead_hash) or (
                    not expected_hash and document.lead_words
                ):
                    raise DocumentError(
                        "section_changed" if expected_hash else "hash_required",
                        f"The lead changed or needs its expected_hash (current {document.lead_hash}).",
                        "Call doc_outline with section_id '_lead' and redo the edit on the current text.",
                        current_hash=document.lead_hash,
                    )
            else:
                guard(document.section(section_id), expected_hash, "rewriting it")
            lines = replace_section(document, section_id, content, heading or None)
            new_sha, checkpoint_id = commit(
                target, lines, expected_sha256=sha256, operation="doc_write_section", task_id=_task_id,
            )
            updated = parse_document("".join(lines))
            result: dict[str, Any] = {
                "path": str(target),
                "sha256": new_sha,
                "status": "written",
                "pending_sections": sum(item.pristine_pending for item in updated.sections),
                "checkpoint_id": checkpoint_id,
            }
            if section_id == LEAD_ID:
                result["section"] = {"id": LEAD_ID, "words": updated.lead_words, "hash": updated.lead_hash}
            else:
                result["section"] = section_view(updated.section(section_id))
            return result
        return run(action)

    def _doc_add_section(
        path: str,
        heading: str,
        content: str = "",
        intent: str = "",
        level: int = 0,
        after: str = "",
        before: str = "",
        section_id: str = "",
        _task_id: str = "",
    ) -> str | ToolFailure:
        def action() -> dict[str, Any]:
            target = resolve(path, write=True)
            document, sha256 = load(target)
            lines, new_id = insert_section(
                document, heading, content=content, intent=intent, level=level or None,
                after=after or None, before=before or None, section_id=section_id or None,
            )
            new_sha, checkpoint_id = commit(
                target, lines, expected_sha256=sha256, operation="doc_add_section", task_id=_task_id,
            )
            updated = parse_document("".join(lines))
            return {
                "path": str(target),
                "sha256": new_sha,
                "status": "added",
                "section": section_view(updated.section(new_id)),
                "sections": [item.id for item in updated.sections],
                "checkpoint_id": checkpoint_id,
            }
        return run(action)

    def _doc_remove_section(
        path: str,
        section_id: str,
        expected_hash: str = "",
        _task_id: str = "",
    ) -> str | ToolFailure:
        def action() -> dict[str, Any]:
            target = resolve(path, write=True)
            document, sha256 = load(target)
            section = document.section(section_id)
            # Removing takes the sub-sections too, so any written one needs the hash.
            subtree = [item for item in document.sections if section.start <= item.start < section.end]
            guard(section, expected_hash, "removing it", untouched=all(item.pristine_pending for item in subtree))
            lines = remove_section(document, section_id)
            new_sha, checkpoint_id = commit(
                target, lines, expected_sha256=sha256, operation="doc_remove_section", task_id=_task_id,
            )
            updated = parse_document("".join(lines))
            return {
                "path": str(target),
                "sha256": new_sha,
                "status": "removed",
                "removed": {"id": section.id, "heading": section.heading},
                "sections": [item.id for item in updated.sections],
                "checkpoint_id": checkpoint_id,
            }
        return run(action)

    def _doc_comments(path: str) -> str | ToolFailure:
        def action() -> dict[str, Any]:
            target = resolve(path, write=False)
            document, sha256 = load(target)
            return {
                "path": str(target),
                "sha256": sha256,
                "open_comments": len(document.comments),
                "comments": comment_views(document),
            }
        return run(action)

    def _doc_resolve_comment(
        path: str,
        comment_id: str,
        note: str = "",
        _task_id: str = "",
    ) -> str | ToolFailure:
        def action() -> dict[str, Any]:
            target = resolve(path, write=True)
            document, sha256 = load(target)
            comment = next((item for item in document.comments if item.id == comment_id), None)
            lines = remove_comment(document, comment_id)
            new_sha, checkpoint_id = commit(
                target, lines, expected_sha256=sha256, operation="doc_resolve_comment", task_id=_task_id,
            )
            updated = parse_document("".join(lines))
            return {
                "path": str(target),
                "sha256": new_sha,
                "status": "resolved",
                "resolved": {"id": comment_id, "text": comment.text if comment else "", "note": note.strip()},
                "open_comments": len(updated.comments),
                "checkpoint_id": checkpoint_id,
            }
        return run(action)

    # One model-facing edit tool keeps the manifest small; the per-action tools
    # above stay registered as hidden aliases for old transcripts and callers.
    edit_actions: dict[str, tuple[str, tuple[str, ...]]] = {
        "write": ("Write document section", ("section_id", "content")),
        "add": ("Add document section", ("heading",)),
        "remove": ("Remove document section", ("section_id",)),
        "resolve_comment": ("Resolve document comment", ("comment_id",)),
    }

    def _doc_edit(
        path: str,
        action: str,
        section_id: str = "",
        content: str | None = None,
        heading: str = "",
        expected_hash: str = "",
        intent: str = "",
        level: int = 0,
        after: str = "",
        before: str = "",
        comment_id: str = "",
        note: str = "",
        _task_id: str = "",
    ) -> str | ToolFailure:
        if action not in edit_actions:
            return failure("invalid_arguments", f"Unknown doc_edit action: {action}",
                           "Use write, add, remove or resolve_comment.", retryable=False)
        given = {"section_id": section_id, "content": content, "heading": heading, "comment_id": comment_id}
        missing = [name for name in edit_actions[action][1]
                   if given[name] is None or (name != "content" and not given[name])]
        if missing:
            return failure("invalid_arguments", f"doc_edit action={action} needs {', '.join(missing)}",
                           "Add the missing fields and call doc_edit again.", retryable=False)
        if action == "write":
            result = _doc_write_section(path, section_id, content or "", heading, expected_hash, _task_id)
        elif action == "add":
            result = _doc_add_section(path, heading, content or "", intent, level, after, before, section_id, _task_id)
        elif action == "remove":
            result = _doc_remove_section(path, section_id, expected_hash, _task_id)
        else:
            result = _doc_resolve_comment(path, comment_id, note, _task_id)
        if isinstance(result, str):
            result = json.dumps({"action": action, **json.loads(result)}, ensure_ascii=False)
        return result

    path_schema = {
        "type": "string",
        "description": "Markdown document path (.md or .markdown), relative to the workspace or absolute",
    }
    hash_schema = {
        "type": "string",
        "description": "Section hash from the latest doc_outline or write result; guards against overwriting edits made since",
    }
    common = {
        "group": "documents",
        "sandboxed": False,
        "strict_schema": True,
        "approval_justification": True,
        "permission_grant": permission_grant,
    }

    registry.register(ToolDef(
        name="doc_create",
        description=(
            "Create a new Markdown document for section-by-section writing: a title, an optional lead and one "
            "pending placeholder per section, each with an intent saying what it will hold. Refuses an existing "
            "path. Then fill one section per doc_edit action=write call, in reading order, so the user can follow "
            "progress. Use doc_export for Word, PDF or HTML copies."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": path_schema,
                "title": {"type": "string", "description": "Document title (becomes the level-1 heading)"},
                "lead": {"type": "string", "description": "Optional opening paragraph(s) under the title; state the main point first"},
                "sections": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 60,
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "intent": {"type": "string", "description": "What this section will contain; shown as its placeholder"},
                            "id": {"type": "string", "description": "Optional stable id; default derived from the heading"},
                            "level": {"type": "integer", "minimum": 2, "maximum": 6, "default": 2},
                            "content": {"type": "string", "description": "Optional body to write immediately instead of a placeholder"},
                        },
                        "required": ["heading"],
                        "additionalProperties": False,
                    },
                },
                **approval_justification_schema(),
            },
            "required": ["path", "title", "sections"],
            "additionalProperties": False,
        },
        fn=_doc_create,
        risk="write",
        permission_check=permission_check("Create document", write=True),
        **common,
    ))
    registry.register(ToolDef(
        name="doc_outline",
        description=(
            "Read a Markdown document's structure: title, lead, and every section's id, heading, level, status "
            "(pending or written), word count and hash, plus the open review comments (id, section, nearby text) "
            "that reviewers write as <!-- @astra: instruction -->. Pass section_id (or '_lead') to also get that "
            "section's own Markdown (without its sub-sections) and its sub-section ids before rewriting it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": path_schema,
                "section_id": {"type": "string", "description": "Optional section id, or '_lead' for the text under the title"},
                **approval_justification_schema(),
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        fn=_doc_outline,
        risk="read",
        permission_check=permission_check("Read document outline", write=False),
        **common,
    ))

    def edit_permission_check(args: dict) -> dict | None:
        operation = edit_actions.get(str(args.get("action") or ""), ("Edit document",))[0]
        return permission_check(operation, write=True)(args)

    registry.register(ToolDef(
        name="doc_edit",
        description=(
            "Change a Markdown document section by section. action=write replaces one section's own text up to its "
            "first sub-section, which stays (content without the section's own heading; optional new heading; "
            "section_id '_lead' is the text under the title). action=add inserts a section after or before another, or at the end; without content it is "
            "a pending placeholder described by intent. action=remove deletes a section with its sub-sections. "
            "action=resolve_comment removes a handled review comment and records a note. Filling a pending "
            "placeholder needs no hash; rewriting a written section, or removing one whose text or sub-sections "
            "were written, needs expected_hash from the latest doc_outline or edit result, and the call is "
            "refused if the section changed since."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": path_schema,
                "action": {"type": "string", "enum": list(edit_actions)},
                "section_id": {"type": "string", "description": "write/remove: target section; add: optional id for the new section"},
                "content": {"type": "string", "description": "write: new Markdown body (required); add: optional body"},
                "heading": {"type": "string", "description": "add: heading of the new section (required); write: optional new heading"},
                "expected_hash": hash_schema,
                "intent": {"type": "string", "description": "add: what a pending section will contain"},
                "level": {"type": "integer", "minimum": 2, "maximum": 6, "description": "add: default the anchor section's level, else 2"},
                "after": {"type": "string", "description": "add: insert after this section id"},
                "before": {"type": "string", "description": "add: insert before this section id"},
                "comment_id": {"type": "string", "description": "resolve_comment: comment id from doc_outline"},
                "note": {"type": "string", "description": "resolve_comment: one sentence on how it was handled"},
                **approval_justification_schema(),
            },
            "required": ["path", "action"],
            "additionalProperties": False,
        },
        fn=_doc_edit,
        risk="write",
        permission_check=edit_permission_check,
        **common,
    ))
    registry.register(ToolDef(
        name="doc_write_section",
        description=(
            "Replace one section's own text up to its first sub-section (and optionally its heading) in a Markdown "
            "document. Content is the text without the section's own heading; deeper headings are allowed only "
            "while the section has no sub-sections. Filling a pending placeholder "
            "needs no hash. Rewriting a written section requires expected_hash from the latest doc_outline or "
            "write result; if someone edited the section since, the call is refused so their text is never lost. "
            "Use section_id '_lead' for the text under the title."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": path_schema,
                "section_id": {"type": "string"},
                "content": {"type": "string", "description": "New Markdown body for the section"},
                "heading": {"type": "string", "description": "Optional new heading text (same level)"},
                "expected_hash": hash_schema,
                **approval_justification_schema(),
            },
            "required": ["path", "section_id", "content"],
            "additionalProperties": False,
        },
        fn=_doc_write_section,
        risk="write",
        permission_check=permission_check("Write document section", write=True),
        expose_by_default=False,
        allow_hidden_execution=True,
        **common,
    ))
    registry.register(ToolDef(
        name="doc_add_section",
        description=(
            "Insert a new section before or after an existing section (after places it after that section's "
            "sub-sections), or at the end. Without content it becomes a pending placeholder described by intent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": path_schema,
                "heading": {"type": "string"},
                "content": {"type": "string", "description": "Optional body; omit to add a pending placeholder"},
                "intent": {"type": "string", "description": "What the pending section will contain"},
                "level": {"type": "integer", "minimum": 2, "maximum": 6, "description": "Default: the anchor section's level, else 2"},
                "after": {"type": "string", "description": "Insert after this section id"},
                "before": {"type": "string", "description": "Insert before this section id"},
                "section_id": {"type": "string", "description": "Optional stable id for the new section"},
                **approval_justification_schema(),
            },
            "required": ["path", "heading"],
            "additionalProperties": False,
        },
        fn=_doc_add_section,
        risk="write",
        permission_check=permission_check("Add document section", write=True),
        expose_by_default=False,
        allow_hidden_execution=True,
        **common,
    ))
    registry.register(ToolDef(
        name="doc_remove_section",
        description=(
            "Delete a section together with its sub-sections. If the section or any sub-section was written, "
            "pass expected_hash from the latest doc_outline; untouched pending placeholders need none."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": path_schema,
                "section_id": {"type": "string"},
                "expected_hash": hash_schema,
                **approval_justification_schema(),
            },
            "required": ["path", "section_id"],
            "additionalProperties": False,
        },
        fn=_doc_remove_section,
        risk="write",
        permission_check=permission_check("Remove document section", write=True),
        expose_by_default=False,
        allow_hidden_execution=True,
        **common,
    ))
    registry.register(ToolDef(
        name="doc_comments",
        description=(
            "List the open review comments in a Markdown document. Reviewers write them anywhere in the file as "
            "<!-- @astra: instruction -->. Each comment has an id, its section and nearby text. Act on a comment "
            "with doc_edit, then resolve it with doc_edit action=resolve_comment."
        ),
        parameters={
            "type": "object",
            "properties": {"path": path_schema, **approval_justification_schema()},
            "required": ["path"],
            "additionalProperties": False,
        },
        fn=_doc_comments,
        risk="read",
        permission_check=permission_check("Read document comments", write=False),
        expose_by_default=False,
        allow_hidden_execution=True,
        **common,
    ))
    registry.register(ToolDef(
        name="doc_resolve_comment",
        description=(
            "Remove one handled review comment from a Markdown document and record a short note on what was done. "
            "Resolve a comment only after acting on it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": path_schema,
                "comment_id": {"type": "string"},
                "note": {"type": "string", "description": "One sentence on how the comment was handled"},
                **approval_justification_schema(),
            },
            "required": ["path", "comment_id"],
            "additionalProperties": False,
        },
        fn=_doc_resolve_comment,
        risk="write",
        permission_check=permission_check("Resolve document comment", write=True),
        expose_by_default=False,
        allow_hidden_execution=True,
        **common,
    ))

    def export_targets(args: dict) -> tuple[str, str, str]:
        from .documents_export import SUFFIXES

        source = str(args.get("path") or "")
        fmt = str(args.get("format") or "docx")
        output = str(args.get("output") or "") or str(Path(source).with_suffix(SUFFIXES.get(fmt, ".docx")))
        return source, output, str(args.get("template") or "")

    def export_permission_check(args: dict) -> dict | None:
        source, output, template = export_targets(args)
        fmt = str(args.get("format") or "docx")
        request = access.permission_request(output, write=True, operation=f"Export document to {fmt}")
        if request is None:
            request = access.permission_request(source, write=False, operation="Read document for export")
        if request is None and template:
            request = access.permission_request(template, write=False, operation="Read Word template")
        return request

    def _doc_export(
        path: str,
        format: str = "docx",
        output: str = "",
        engine: str = "auto",
        template: str = "",
        paper: str = "a4",
        _task_id: str = "",
    ) -> str | ToolFailure:
        from .documents_export import ExportError, SUFFIXES, export_document

        def action() -> dict[str, Any]:
            source = resolve(path, write=False)
            document, _ = load(source)
            _, output_path, _ = export_targets({"path": str(source), "format": format, "output": output})
            target = access.resolve(output_path, write=True)
            suffix = SUFFIXES[format]
            if target.suffix.lower() != suffix:
                raise DocumentError("invalid_output", f"A {format} export must be written to a {suffix} file: {target}")
            if target == source:
                raise DocumentError("invalid_output", "The export would overwrite the source document.")
            template_path: Path | None = None
            if template:
                template_path = access.resolve(template, write=False)
                if template_path.suffix.lower() != ".docx" or not template_path.is_file():
                    raise DocumentError("invalid_template", f"Template must be an existing .docx file: {template_path}")

            def resolve_image(src: str) -> Path:
                cleaned = urllib.parse.unquote(src)
                if cleaned.lower().startswith("file:"):
                    cleaned = urllib.parse.unquote(urllib.parse.urlparse(cleaned).path)
                candidate = Path(cleaned).expanduser()
                if not candidate.is_absolute():
                    candidate = source.parent / candidate
                return access.resolve(str(candidate), write=False)

            pending = capture(target, "doc_export", _task_id)
            try:
                result = export_document(
                    document.text, base_dir=source.parent, output=target, fmt=format, engine=engine,
                    template=template_path, paper=paper, resolve_image=resolve_image,
                )
            except ExportError as exc:
                raise DocumentError(exc.code, str(exc), exc.hint) from exc
            checkpoint_id = finalize(pending)
            data = target.read_bytes()
            return {
                "path": str(target),
                "source": str(source),
                "format": format,
                "engine": result.engine,
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "pending_sections": sum(item.pristine_pending for item in document.sections),
                "warnings": result.warnings,
                "status": "exported",
                "checkpoint_id": checkpoint_id,
            }
        return run(action)

    registry.register(ToolDef(
        name="doc_export",
        description=(
            "Export a Markdown document to Word (docx), PDF or HTML next to it (or at output). Section markers "
            "and review comments are left out; pending placeholders stay and are reported. engine auto uses "
            "pandoc when installed, otherwise the built-in python-docx exporter; PDF converts the Word file "
            "with LibreOffice. template is an optional .docx whose styles are reused. Remote images are not "
            "downloaded. Check the returned warnings before sharing the file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": path_schema,
                "format": {"type": "string", "enum": ["docx", "pdf", "html"], "default": "docx"},
                "output": {"type": "string", "description": "Optional output path; default is the document path with the new extension"},
                "engine": {"type": "string", "enum": ["auto", "python-docx", "pandoc"], "default": "auto"},
                "template": {"type": "string", "description": "Optional .docx template for Word/PDF styles"},
                "paper": {"type": "string", "enum": ["a4", "letter"], "default": "a4", "description": "Page size without a template (python-docx engine)"},
                **approval_justification_schema(),
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        fn=_doc_export,
        risk="write",
        timeout=300,
        permission_check=export_permission_check,
        **common,
    ))
