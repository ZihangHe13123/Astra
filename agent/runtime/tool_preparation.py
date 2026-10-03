"""Bounded, display-only tool argument progress. Never supplies execution inputs."""

from __future__ import annotations

import functools
import re
import time
import uuid

_PATH_TOOLS = frozenset({"read_file", "stat_file", "write_file", "begin_file_write", "edit_file", "search_files"})
_SHELL_TOOLS = frozenset({"execute_shell", "bash"})
_FIELDS = frozenset({"path", "file_path", "command"})
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class _Fields:
    """One lexical pass; retain only bounded top-level string prefixes.

    This deliberately does not repair or validate JSON. A preview can be partial;
    the provider and runtime still validate the original complete arguments.
    """

    def __init__(self):
        self.depth = 0
        self.in_string = False
        self.key_string = False
        self.expect_key = False
        self.key = ""
        self.target = ""
        self.value = ""
        self.escape = False
        self.unicode: str | None = None
        self.surrogate = ""
        self.fields: dict[str, str] = {}

    def _character(self, char: str) -> None:
        if self.surrogate:
            if 0xDC00 <= ord(char) <= 0xDFFF:
                char = chr(0x10000 + ((ord(self.surrogate) - 0xD800) << 10) + ord(char) - 0xDC00)
            self.surrogate = ""
        if 0xD800 <= ord(char) <= 0xDBFF:
            self.surrogate = char
            return
        if 0xDC00 <= ord(char) <= 0xDFFF:
            return
        if (self.key_string or self.target) and len(self.value) < 160:
            self.value += char

    def feed(self, delta: str) -> None:
        for char in delta:
            if self.in_string:
                if self.unicode is not None:
                    if char not in "0123456789abcdefABCDEF":
                        self.unicode = None
                        continue
                    self.unicode += char
                    if len(self.unicode) == 4:
                        self._character(chr(int(self.unicode, 16)))
                        self.unicode = None
                    continue
                if self.escape:
                    self.escape = False
                    if char == "u":
                        self.unicode = ""
                    elif char in _ESCAPES:
                        self._character(_ESCAPES[char])
                    continue
                if char == "\\":
                    self.escape = True
                elif char == '"':
                    self.in_string = False
                    if self.key_string:
                        self.key = self.value
                        self.expect_key = False
                    elif self.target:
                        self.fields[self.target] = self.value
                    self.target = ""
                else:
                    self._character(char)
                continue
            if char == '"':
                self.in_string = True
                self.key_string = self.depth == 1 and self.expect_key
                self.target = self.key if self.depth == 1 and not self.key_string and self.key in _FIELDS else ""
                self.value = self.surrogate = ""
            elif char in "{[":
                self.depth += 1
                if self.depth == 1:
                    self.expect_key = char == "{"
            elif char in "}]":
                self.depth = max(0, self.depth - 1)
            elif char == "," and self.depth == 1:
                self.expect_key = True
                self.key = ""
        if self.in_string and self.target:
            self.fields[self.target] = self.value


class ToolPreparation:
    """Coalesces display snapshots without accumulating another argument copy."""

    def __init__(self, *, interval: float = 0.1, clock=time.monotonic):
        self.attempt_id = uuid.uuid4().hex
        self.interval, self.clock = interval, clock
        self.calls: dict[int, dict] = {}
        self.fields: dict[int, _Fields] = {}
        self.last_emit: float | None = None
        self.shown = False

    def observe(self, event: dict) -> dict | None:
        index = event.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return None
        if index not in self.calls:
            if len(self.calls) >= 8:
                return None
            self.calls[index] = {"index": index, "call_id": "", "name": "", "argument_chars": 0, "summary": ""}
            self.fields[index] = _Fields()
        call = self.calls[index]
        for key in ("call_id", "name"):
            if isinstance(event.get(key), str) and event[key]:
                call[key] = "".join(char for char in event[key][:200] if char.isprintable())
        delta = event.get("delta")
        if isinstance(event.get("arguments"), str):
            # A provider may report a final suffix, or repeat an already seen
            # snapshot. Identity/prefix correctness stays with that provider.
            delta = event["arguments"][call["argument_chars"]:]
        if isinstance(delta, str) and delta:
            call["argument_chars"] += len(delta)
            self.fields[index].feed(delta)
        fields = self.fields[index].fields
        summary = ""
        if call["name"] in _PATH_TOOLS:
            summary = fields.get("path") or fields.get("file_path") or ""
        elif call["name"] in _SHELL_TOOLS:
            # Do not surface arguments, environment assignments or shell
            # credentials. Only the received executable name is a preview.
            match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_.+-]{0,63})(?:\s|$)", fields.get("command", ""))
            summary = match[1] + " …" if match else ""
        call["summary"] = "".join(char for char in " ".join(summary.split()) if char.isprintable())[:160]
        now = self.clock()
        if self.last_emit is not None and now - self.last_emit < self.interval:
            return None
        self.last_emit = now
        self.shown = True
        return {"type": "tool_preparing", "attempt_id": self.attempt_id, "state": "preparing",
                "calls": [dict(self.calls[index]) for index in sorted(self.calls)]}

    def end(self, state: str) -> dict | None:
        if not self.shown:
            return None
        self.shown = False
        return {"type": "tool_preparing", "attempt_id": self.attempt_id, "state": state, "calls": []}


def with_tool_preparation(function):
    """Consume private delta events, forwarding only bounded public snapshots."""

    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        preview = ToolPreparation()
        stream = function(*args, **kwargs)
        try:
            async for event in stream:
                kind = event.get("type")
                if kind == "_tool_preparation_reset":
                    ended = preview.end("discarded")
                    if ended:
                        yield ended
                    preview = ToolPreparation()
                elif kind == "_tool_preparation":
                    update = preview.observe(event)
                    if update:
                        yield update
                else:
                    if kind == "tool_calls" and not preview.calls:
                        # Final-only providers still report only information
                        # actually received, with no fabricated progress.
                        for index, call in enumerate(event.get("calls") or []):
                            update = preview.observe({"index": index, "call_id": call.get("id"),
                                "name": call.get("name"), "arguments": call.get("arguments")})
                            if update:
                                yield update
                    if kind in {"done", "tool_calls"}:
                        ended = preview.end("discarded" if event.get("tool_call_state") == "incomplete" else "finished")
                        if ended:
                            yield ended
                    yield event
        except Exception:
            ended = preview.end("discarded")
            if ended:
                yield ended
            raise
        finally:
            # CancelledError/GeneratorExit cannot yield safely. The consumer's
            # cancellation/disconnect boundary removes any remaining previews.
            await stream.aclose()

    return wrapped
