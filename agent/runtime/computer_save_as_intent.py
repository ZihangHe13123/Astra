"""Approval-time proof for creating a new local copy through a native save panel."""

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
import ctypes
import os
import struct
import sys
from pathlib import Path

from . import approval_time
from .computer_protocol import ComputerAction


SAVE_AS_BOUNDARY = (
    "Approval covers eligible steps in this Save As panel and its attached folder chooser for this exact path. "
    "Foreground control can still require permission. Save checks the folder and name and never overwrites. "
    "Ends when the panel closes, after 5 minutes, on any mismatch, or on an error."
)
SAVE_AS_COVERED_REASONS = frozenset({"keyword_window_title", "unreferenced_enter", "save_new_copy_to"})
SAVE_AS_SHEET_REASONS = frozenset({"target_metadata_incomplete", "unreferenced_enter", "keyword_window_title"})


_WINDOW_TITLE_NOTE = "（窗口标题，无文件路径）"


@dataclass(frozen=True)
class _SaveAsSource:
    name: str
    from_window_title: bool = False

    @property
    def extension(self) -> str:
        path = Path(self.name)
        if not self.name.isprintable() or not (self.from_window_title or path.is_absolute()):
            return ""
        return path.suffix.casefold()

    @property
    def disclosure(self) -> str:
        return self.name + _WINDOW_TITLE_NOTE if self.from_window_title else self.name


def valid_save_as_source_disclosure(source: str) -> bool:
    return source.startswith("/") or (
        source.endswith(_WINDOW_TITLE_NOTE)
        and bool(_SaveAsSource(source.removesuffix(_WINDOW_TITLE_NOTE), from_window_title=True).extension)
    )


@dataclass(eq=False)
class _SaveAsIntent:
    session: str
    grant_space: str
    source_window: str
    source: _SaveAsSource
    destination: Path
    deadline_ms: int
    existing_windows: frozenset[str]
    panel: str = ""
    sheet: str = ""

    def expired(self) -> bool:
        return approval_time.continuous_now() >= self.deadline_ms / 1000


def save_as_sheet_actions(
    actions: Sequence[ComputerAction], destination: Path, elements: Mapping[str, Mapping],
) -> bool:
    fields = [ref for ref, node in elements.items() if node.get("role") in {"AXTextField", "AXTextArea", "AXSecureTextField"}]
    field = fields[0] if len(fields) == 1 and elements[fields[0]].get("role") == "AXTextField" else None
    remaining = iter(actions)
    for action in remaining:
        if action.type == "wait" or action.type == "keypress" and action.key == "return" and not action.modifiers:
            continue
        if action.type == "click" and not action.modifiers and action.checked is None:
            candidates = [
                node for ref, node in elements.items()
                if ref == action.element_ref or action.element_ref is None and action.element_index == node.get("index")
            ] if action.element_ref or action.element_index is not None else []
            if len(candidates) != 1:
                return False
            button = candidates[0]
            if (button.get("role") != "AXButton" or str(button.get("title") or "").strip().casefold()
                    not in {"前往", "go", "取消", "cancel"} or "AXPress" not in (button.get("actions") or ())):
                return False
            continue
        selecting = (field is not None and action.type == "keypress" and action.key == "a"
                     and action.modifiers == ("command",) and action.element_ref == field)
        if selecting:
            action = next(remaining, None)
        if (action is None or field is None or action.type != "type" or action.element_ref != field
                or action.modifiers or action.target_text is not None or not (selecting or action.replace is True)
                or not action.text or not action.text.isprintable()):
            return False
        try:
            path = Path(action.text).expanduser()
            if not path.is_absolute() or path.resolve(strict=True) != destination.parent.resolve(strict=True):
                return False
        except (OSError, ValueError, RuntimeError):
            return False
    return True


def _local_directory(directory: Path) -> bool:
    if sys.platform != "darwin" or not directory.is_dir():
        return False
    # Darwin getattrlist with ATTR_VOL_INFO | ATTR_VOL_MOUNTFLAGS. The result is
    # a uint32 byte count followed by the uint32 mount flags (see sys/attr.h).
    attributes = ctypes.create_string_buffer(struct.pack("=HHIIIII", 5, 0, 0, 0x80004000, 0, 0, 0))
    result = (ctypes.c_uint32 * 2)()
    getattrlist = ctypes.CDLL(None).getattrlist
    status = getattrlist(os.fsencode(directory), attributes, result, ctypes.sizeof(result), ctypes.c_ulong(0))
    return status == 0 and result[0] == ctypes.sizeof(result) and bool(result[1] & 0x00001000)  # MNT_LOCAL


def resolve_save_as_destination(path: str, source: _SaveAsSource | None) -> tuple[Path | None, str]:
    requested = Path(path)
    if (
        not requested.is_absolute() or not requested.suffix or path.startswith("//") or path.endswith("/")
        or any(part in {".", ".."} for part in path.split("/")) or not all(char.isprintable() for char in path)
        or len(os.fsencode(path)) > 4096 or len(os.fsencode(requested.name)) > 255
        or any(char in requested.name for char in (":", "\\"))
    ):
        return None, "invalid_arguments"
    if source is None or source.extension != requested.suffix.casefold():
        return None, "save_destination_unverified"
    try:
        directory = requested.parent.resolve(strict=True)
        if not _local_directory(directory):
            return None, "save_destination_unverified"
        destination = directory / requested.name
        try:
            destination.lstat()
        except FileNotFoundError:
            return destination, ""
    except (OSError, ValueError, RuntimeError, AttributeError):
        return None, "save_destination_unverified"
    return None, "save_destination_exists"
