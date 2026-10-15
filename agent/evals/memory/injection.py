"""Find what a provider request adds to canonical history, without marker strings.

The canonical side is the conversation's own provider projection of
``AgentContext.messages`` with the base (effective) system prompt and no
runtime context. Messages are aligned by exact content; request messages with
no canonical counterpart are whole injections, and canonical messages whose
text grew contribute only the inserted characters.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


@dataclass(frozen=True)
class Segment:
    index: int  # position in the request
    role: str
    text: str
    whole: bool  # an entire injected message, or text added to a canonical one


@dataclass(frozen=True)
class RequestDiff:
    segments: tuple[Segment, ...]
    dropped: int  # canonical messages absent from the request

    @property
    def texts(self) -> tuple[str, ...]:
        return tuple(segment.text for segment in self.segments)

    @property
    def injected_messages(self) -> int:
        return sum(segment.whole for segment in self.segments)

    @property
    def augmented_messages(self) -> int:
        return len({segment.index for segment in self.segments if not segment.whole})

    @property
    def injected_text(self) -> str:
        return "\n".join(self.texts)


def message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return "" if content is None else str(content)


def _key(message: dict) -> str:
    return json.dumps(message, sort_keys=True, ensure_ascii=False, default=str)


def inserted_text(original: str, changed: str) -> list[str]:
    """Return the runs of ``changed`` that are not in ``original``."""
    if original == changed:
        return []
    limit = min(len(original), len(changed))
    prefix = 0
    while prefix < limit and original[prefix] == changed[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and original[-1 - suffix] == changed[-1 - suffix]:
        suffix += 1
    old_middle = original[prefix:len(original) - suffix]
    new_middle = changed[prefix:len(changed) - suffix]
    if not old_middle:
        return [new_middle] if new_middle else []
    matcher = SequenceMatcher(None, old_middle, new_middle, autojunk=False)
    return [
        new_middle[j1:j2] for tag, _i1, _i2, j1, j2 in matcher.get_opcodes()
        if tag in {"insert", "replace"} and new_middle[j1:j2]
    ]


def diff_request(request: Sequence[dict], canonical: Sequence[dict]) -> RequestDiff:
    request_keys = [_key(message) for message in request]
    canonical_keys = [_key(message) for message in canonical]
    matcher = SequenceMatcher(None, canonical_keys, request_keys, autojunk=False)
    segments: list[Segment] = []
    dropped = 0

    def whole(index: int) -> None:
        text = message_text(request[index])
        segments.append(Segment(index, str(request[index].get("role", "")), text, True))

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            for index in range(j1, j2):
                whole(index)
            continue
        if tag == "delete":
            dropped += i2 - i1
            continue
        # A replaced block: pair messages of the same role in order, so a
        # canonical message with added text yields only the added runs.
        roles = SequenceMatcher(
            None,
            [str(message.get("role", "")) for message in canonical[i1:i2]],
            [str(message.get("role", "")) for message in request[j1:j2]],
            autojunk=False,
        )
        for role_tag, a1, a2, b1, b2 in roles.get_opcodes():
            if role_tag == "equal":
                for offset in range(a2 - a1):
                    old, new = canonical[i1 + a1 + offset], request[j1 + b1 + offset]
                    for text in inserted_text(message_text(old), message_text(new)):
                        segments.append(Segment(j1 + b1 + offset, str(new.get("role", "")), text, False))
                continue
            dropped += a2 - a1
            for index in range(j1 + b1, j1 + b2):
                whole(index)
    segments.sort(key=lambda segment: segment.index)
    return RequestDiff(tuple(segments), dropped)


def fresh_segments(current: Sequence[str], previous: Sequence[str]) -> list[str]:
    """Injected texts that were not already present in the previous request."""
    remaining = Counter(previous)
    fresh = []
    for text in current:
        if remaining[text] > 0:
            remaining[text] -= 1
        else:
            fresh.append(text)
    return fresh


def prefix_stability(previous: Sequence[dict], current: Sequence[dict]) -> dict[str, Any]:
    """Whether the previous request is a byte-identical prefix of this one."""
    previous_keys = [_key(message) for message in previous]
    current_keys = [_key(message) for message in current]
    common_messages = 0
    for old, new in zip(previous_keys, current_keys):
        if old != new:
            break
        common_messages += 1
    old_bytes = "\n".join(previous_keys).encode("utf-8")
    new_bytes = "\n".join(current_keys).encode("utf-8")
    common_bytes = 0
    for old_byte, new_byte in zip(old_bytes, new_bytes):
        if old_byte != new_byte:
            break
        common_bytes += 1
    return {
        "stable": common_messages == len(previous_keys) and len(current_keys) >= len(previous_keys),
        "common_messages": common_messages,
        "previous_messages": len(previous_keys),
        "common_prefix_ratio": round(common_bytes / len(old_bytes), 4) if old_bytes else 1.0,
        "first_divergence": None if common_messages == len(previous_keys) else common_messages,
    }


def core_occurrences(text: str, core_entries: Sequence[str]) -> tuple[list[str], str]:
    """Split ``text`` into core-entry occurrences and the remaining text."""
    found: list[str] = []
    remainder = text
    for entry in sorted(set(core_entries), key=len, reverse=True):
        for form in dict.fromkeys((entry, entry.replace("<", "&lt;").replace(">", "&gt;"))):
            count = remainder.count(form)
            if count:
                found.extend([entry] * count)
                remainder = remainder.replace(form, "")
    return found, remainder


__all__ = [
    "RequestDiff",
    "Segment",
    "core_occurrences",
    "diff_request",
    "fresh_segments",
    "inserted_text",
    "message_text",
    "prefix_stability",
]
