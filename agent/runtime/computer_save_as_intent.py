"""Approval-time proof for creating a new local copy through a native save panel."""

from dataclasses import dataclass
import ctypes
import os
import struct
import sys
from pathlib import Path

from . import approval_time


SAVE_AS_BOUNDARY = (
    "After approval, steps inside this Save As panel and the final Save to exactly this path are "
    "not asked again one by one. The Save still checks the folder and name and never overwrites. "
    "Ends when the panel closes, after 5 minutes, on any mismatch, or on an error."
)
SAVE_AS_COVERED_REASONS = frozenset({"keyword_window_title", "unreferenced_enter", "save_new_copy_to"})


@dataclass(eq=False)
class _SaveAsIntent:
    session: str
    grant_space: str
    source_window: str
    source: str
    destination: Path
    deadline_ms: int
    existing_windows: frozenset[str]
    panel: str = ""

    def expired(self) -> bool:
        return approval_time.continuous_now() >= self.deadline_ms / 1000


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


def resolve_save_as_destination(path: str, source: str) -> tuple[Path | None, str]:
    requested = Path(path)
    if (
        not requested.is_absolute() or not requested.suffix or path.startswith("//") or path.endswith("/")
        or any(part in {".", ".."} for part in path.split("/")) or not all(char.isprintable() for char in path)
        or len(os.fsencode(path)) > 4096 or len(os.fsencode(requested.name)) > 255
        or any(char in requested.name for char in (":", "\\"))
    ):
        return None, "invalid_arguments"
    if not source or not Path(source).is_absolute() or not all(char.isprintable() for char in source):
        return None, "save_destination_unverified"
    if Path(source).suffix.casefold() != requested.suffix.casefold():
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
