"""Turn a streamed Markdown reply into short units of text worth saying aloud."""
from __future__ import annotations

import re

# CJK marks end a clause wherever they stand. ASCII marks do so only before a
# space or a line end, so "3.14", "main.py" and URLs stay whole.
_CJK_HARD, _CJK_SOFT = "。！？；…", "，、："
_ASCII_HARD, _ASCII_SOFT = ".!?;", ",:"
_BLOCK_PREFIX = re.compile(r"\s*(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)+")
_RULE = re.compile(r"\s*(?:[-*_]\s*){3,}")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"(?:https?|file)://\S+")
_CODE = re.compile(r"`([^`]*)`")
_UNSAYABLE_CODE = re.compile(r"[/\\=(){}\[\]<>|$#]|[0-9a-f]{7,}|\w+\.\w{1,4}\b")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")
_EMPHASIS = re.compile(r"\*+|~~|(?<!\w)_+|_+(?!\w)")
_PARENTHESISED = re.compile(r"[（(][^（()）]{1,24}[)）]")
_WORDLIKE = re.compile(r"[㐀-鿿]|[A-Za-z0-9]{2}")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣]")
_LEADING = re.compile(r"^[\s，。、；：！？,.;:!?…—\-~～]+")


def speakable(text: str) -> str:
    """Strip what a reader sees but a listener should not hear."""
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _CODE.sub(lambda match: "" if len(match[1]) > 24 or _UNSAYABLE_CODE.search(match[1]) else match[1], text)
    text = _TAG.sub("", text).replace("`", "")
    text = _EMPHASIS.sub("", text)
    # A bracketed run with no word in it is a kaomoji, not an aside.
    text = _PARENTHESISED.sub(lambda match: match[0] if _WORDLIKE.search(match[0]) else "", text)
    text = _EMOJI.sub("", text)
    return _LEADING.sub("", re.sub(r"\s+", " ", text)).strip()


def _weight(text: str) -> int:
    return sum(character.isalnum() for character in text)


def spoken_seconds(text: str) -> float:
    """Roughly how long a unit takes to say: a CJK character is a syllable, a Latin letter a fraction of one."""
    return sum(0.23 if ord(character) > 0x2E7F else 0.07 if character.isalnum() else 0.05 for character in text)


class SpeechSegmenter:
    """Cut a reply into speakable units while it is still arriving.

    The first unit ends at the first clause mark so speech can start early;
    later units run to a sentence end, or to a clause mark once long enough.
    Code blocks, tables and rules are skipped.
    """

    def __init__(self, *, first_chars: int = 4, soft_chars: int = 36, max_chars: int = 90) -> None:
        self.first_chars, self.soft_chars, self.max_chars = first_chars, soft_chars, max_chars
        self._raw = ""
        self._pending = ""
        self._line_start = True
        self._skip_line = False
        self._in_fence = False
        self._in_code = False
        self._in_target = False  # between "](" and ")" of a link
        self._started = False

    def feed(self, text: str) -> list[str]:
        self._raw += text
        return self._drain(final=False)

    def flush(self) -> list[str]:
        units = self._drain(final=True)
        self._cut(units, hard=True, final=True)
        return units

    def _drain(self, *, final: bool) -> list[str]:
        units: list[str] = []
        raw, index = self._raw, 0
        while index < len(raw):
            if self._line_start:
                end = raw.find("\n", index)
                head = raw[index:] if end < 0 else raw[index:end]
                # A line's kind shows in its first characters; a rule needs the whole line.
                if end < 0 and not final and (len(head) < 4 or _RULE.fullmatch(head)):
                    break
                self._line_start = False
                if head.lstrip().startswith(("```", "~~~")):
                    self._in_fence = not self._in_fence
                    self._skip_line = True
                elif self._in_fence or head.lstrip().startswith("|") or _RULE.fullmatch(head):
                    self._skip_line = True
                else:
                    prefix = _BLOCK_PREFIX.match(head)
                    index += prefix.end() if prefix else 0
                continue
            character = raw[index]
            if character == "\n":
                self._line_start, self._skip_line, self._in_code, self._in_target = True, False, False, False
                self._pending += " "
                self._cut(units, hard=True)
            elif not self._skip_line:
                marks = not self._in_code and not self._in_target
                if marks and character in _ASCII_HARD + _ASCII_SOFT:
                    if index + 1 == len(raw) and not final:
                        break  # the next character decides whether this ends a clause
                    marks = index + 1 == len(raw) or raw[index + 1].isspace()
                self._pending += character
                if character == "`":
                    self._in_code = not self._in_code
                elif character == "(" and self._pending.endswith("]("):
                    self._in_target = True
                elif character == ")":
                    self._in_target = False
                elif marks and character in _CJK_HARD + _ASCII_HARD:
                    self._cut(units, hard=True)
                elif marks and character in _CJK_SOFT + _ASCII_SOFT:
                    self._cut(units, hard=False)
                elif len(self._pending) >= self.max_chars and (character.isspace() or ord(character) > 0x2E7F):
                    self._cut(units, hard=True)
            index += 1
        self._raw = raw[index:]
        return units

    def _cut(self, units: list[str], *, hard: bool, final: bool = False) -> None:
        text = speakable(self._pending)
        weight = _weight(text)
        # A one-character sentence joins the next one; a clause must be worth a breath.
        need = (1 if final else 2) if hard else self.soft_chars if self._started else self.first_chars
        if weight >= need:
            units.append(text)
            self._pending = ""
            self._started = True
        elif hard and not weight:
            self._pending = ""
