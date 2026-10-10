"""Behavior tests for the turn-change snapshot store (M1 · Subagent A).

Source of truth: ``docs/superpowers/plans/2026-09-18-turn-change-ledger-m1.md``
§3.2 (behavior requirements 1-10) and §4 TA (required coverage 1-16).

Every test injects a fake differ, a controllable clock and ``tmp_path``; no
real time, no network, no git, no subprocess.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

from agent.runtime import turn_change_store as store
from agent.runtime.turn_diff import (
    DEFAULT_MAX_FILE_BYTES,
    DIFF_COARSE,
    DIFF_FULL,
    DIFF_NONE,
    REASON_BINARY,
    REASON_CANCELLED,
    REASON_TIMEOUT,
    DiffStats,
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeClock:
    """Controllable ``time.monotonic`` replacement (seconds, monotonic)."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, milliseconds: float) -> None:
        self.now += milliseconds / 1000.0


@dataclass
class DifferCall:
    before: bytes | None
    after: bytes | None
    max_bytes: int
    deadline: float | None
    cancelled: Callable[[], bool] | None


def _line_count(data: bytes | None) -> int:
    if not data:
        return 0
    return len(data.split(b"\n")) - (1 if data.endswith(b"\n") else 0)


class FakeDiffer:
    """Deterministic stand-in for ``turn_diff.diff_bytes`` that records calls."""

    def __init__(
        self,
        *,
        clock: FakeClock | None = None,
        advance_ms: float = 0.0,
        result: DiffStats | None = None,
    ) -> None:
        self.clock = clock
        self.advance_ms = advance_ms
        self.result = result
        self.calls: list[DifferCall] = []

    def __call__(
        self,
        before: bytes | None,
        after: bytes | None,
        *,
        max_bytes: int = DEFAULT_MAX_FILE_BYTES,
        deadline: float | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> DiffStats:
        self.calls.append(DifferCall(before, after, max_bytes, deadline, cancelled))
        if self.clock is not None:
            self.clock.advance(self.advance_ms)
        if self.result is not None:
            return self.result
        return DiffStats(
            added=_line_count(after),
            removed=_line_count(before),
            quality=DIFF_FULL,
            reason="",
            truncated=False,
        )


def make_store(
    tmp_path: Path,
    session_id: str = "sess-1",
    *,
    differ: Callable[..., DiffStats] | None = None,
    clock: FakeClock | None = None,
    limits: store.TurnChangeLimits | None = None,
    root: Path | None = None,
) -> store.TurnChangeStore:
    return store.TurnChangeStore(
        tmp_path,
        session_id,
        limits=limits,
        root=root,
        differ=differ if differ is not None else FakeDiffer(),
        clock=clock if clock is not None else FakeClock(),
    )


def session_dir(tmp_path: Path, session_id: str = "sess-1") -> Path:
    return tmp_path / ".astra" / "turn-changes" / session_id


def entries_by_path(manifest: store.TurnChangesManifest) -> dict[str, store.FileChange]:
    return {change.path: change for change in [*manifest.files, *manifest.unknown]}


# ---------------------------------------------------------------------------
# 1. lifecycle
# ---------------------------------------------------------------------------

def test_empty_turn_seals_to_none(tmp_path: Path) -> None:
    subject = make_store(tmp_path)

    subject.begin_turn("req-1")

    assert subject.seal() is None


def test_seal_without_active_turn_is_a_no_op(tmp_path: Path) -> None:
    subject = make_store(tmp_path)

    assert subject.seal() is None


def test_repeated_begin_turn_raises(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    with pytest.raises(RuntimeError):
        subject.begin_turn("req-2")


def test_begin_turn_after_seal_is_legal(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_capture("a.txt", b"old\n")
    assert subject.seal() is not None

    subject.begin_turn("req-2")
    subject.note_capture("a.txt", b"old\n")

    assert subject.seal() is not None


@pytest.mark.parametrize(
    "note",
    [
        lambda subject: subject.note_paths(["a.txt"]),
        lambda subject: subject.note_capture("a.txt", b"x\n"),
        lambda subject: subject.note_capture("a.txt", b"x\n", checkpoint_id="cp1"),
        lambda subject: subject.note_absent("a.txt"),
        lambda subject: subject.note_tracked("a.txt", "file:1", "file:2"),
    ],
)
def test_note_without_active_turn_raises(tmp_path: Path, note) -> None:
    subject = make_store(tmp_path)

    with pytest.raises(RuntimeError):
        note(subject)


# ---------------------------------------------------------------------------
# 2. capture dedup / mutual exclusion
# ---------------------------------------------------------------------------

def test_note_capture_keeps_only_the_first_snapshot(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"final\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_capture("a.txt", b"first\n", checkpoint_id="cp1")
    subject.note_capture("a.txt", b"second\n", checkpoint_id="cp2")

    manifest = subject.seal()
    assert manifest is not None
    change = entries_by_path(manifest)["a.txt"]
    assert (change.state, change.compare) == (store.STATE_MODIFIED, store.COMPARE_FULL)
    assert change.checkpoint_ids == ["cp1", "cp2"]


def test_note_capture_wins_over_a_later_absent(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_capture("a.txt", b"old\n")
    subject.note_absent("a.txt")

    manifest = subject.seal()
    assert manifest is not None
    change = entries_by_path(manifest)["a.txt"]
    assert change.before_state == store.SIDE_CAPTURED
    assert change.state == store.STATE_MODIFIED


def test_note_absent_wins_over_a_later_capture(tmp_path: Path) -> None:
    (tmp_path / "new.txt").write_bytes(b"fresh\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_absent("new.txt", checkpoint_id="cp1")
    subject.note_capture("new.txt", b"ignored\n", checkpoint_id="cp2")

    manifest = subject.seal()
    assert manifest is not None
    change = entries_by_path(manifest)["new.txt"]
    assert (change.state, change.before_state) == (store.STATE_ADDED, store.SIDE_ABSENT)
    assert change.checkpoint_ids == ["cp1", "cp2"]


def test_candidate_path_with_a_snapshot_is_confirmed(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_paths(["a.txt"])
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal()
    assert manifest is not None
    assert [change.path for change in manifest.files] == ["a.txt"]
    assert manifest.unknown == []


# ---------------------------------------------------------------------------
# 4. five states at seal
# ---------------------------------------------------------------------------

def test_seal_reports_five_states(tmp_path: Path) -> None:
    (tmp_path / "mod.txt").write_bytes(b"new\n")
    (tmp_path / "same.txt").write_bytes(b"same\n")
    (tmp_path / "added.txt").write_bytes(b"fresh\n")
    (tmp_path / "candidate.txt").write_bytes(b"untracked\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_capture("mod.txt", b"old\n")          # → modified
    subject.note_capture("same.txt", b"same\n")        # 改了又改回 → unchanged
    subject.note_absent("added.txt")                   # → added
    subject.note_capture("gone.txt", b"bye\n")         # 快照存在、文件已删 → deleted
    subject.note_paths(["candidate.txt"])              # 仅候选 → unknown

    manifest = subject.seal()
    assert manifest is not None
    confirmed = {change.path: change for change in manifest.files}
    assert set(confirmed) == {"mod.txt", "added.txt", "gone.txt"}
    assert confirmed["mod.txt"].state == store.STATE_MODIFIED
    assert confirmed["added.txt"].state == store.STATE_ADDED
    assert confirmed["gone.txt"].state == store.STATE_DELETED
    assert "same.txt" not in entries_by_path(manifest)

    unknown = {change.path: change for change in manifest.unknown}
    assert set(unknown) == {"candidate.txt"}
    entry = unknown["candidate.txt"]
    assert entry.state == store.STATE_UNKNOWN
    assert entry.before_state == store.SIDE_UNCAPTURED
    assert entry.after_state == store.SIDE_CAPTURED
    assert (entry.compare, entry.added, entry.removed) == (store.COMPARE_NONE, None, None)

    assert manifest.totals["files"] == 3
    assert manifest.session_id == "sess-1"
    assert manifest.request_id == "req-1"


def test_added_and_deleted_use_the_absent_side_for_counts(tmp_path: Path) -> None:
    (tmp_path / "added.txt").write_bytes(b"one\ntwo\n")
    differ = FakeDiffer()
    subject = make_store(tmp_path, differ=differ)
    subject.begin_turn("req-1")

    subject.note_absent("added.txt")
    subject.note_capture("deleted.txt", b"gone\n")

    manifest = subject.seal()
    assert manifest is not None
    added = {change.path: change for change in manifest.files}["added.txt"]
    deleted = {change.path: change for change in manifest.files}["deleted.txt"]
    assert (added.added, added.removed, added.compare) == (2, 0, store.COMPARE_FULL)
    assert (deleted.added, deleted.removed, deleted.compare) == (0, 1, store.COMPARE_FULL)
    assert [(call.before, call.after) for call in differ.calls] == [
        (None, b"one\ntwo\n"),
        (b"gone\n", None),
    ]


def test_after_side_is_the_files_bytes_where_files_open_in_text_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows opens a file in text mode unless asked otherwise: the after side lost its
    carriage returns and ended at the first 0x1A byte, so a "\r\n" file that a turn left
    as it was came out as changed."""
    monkeypatch.setattr(store, "_HANDLE_IO_OK", False)  # the path-based reads Windows uses
    if os.name != "nt":
        # Give this platform the same rule, so the failure shows here too.
        binary = 0x8000
        text_mode: set[int] = set()
        real_open, real_read, real_close = os.open, os.read, os.close

        def open_as_windows(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags & ~binary, *args, **kwargs)
            (text_mode.discard if flags & binary else text_mode.add)(descriptor)
            return descriptor

        def read_as_windows(descriptor, count):
            data = real_read(descriptor, count)
            if descriptor in text_mode:
                data = data.split(b"\x1a")[0].replace(b"\r\n", b"\n")
            return data

        def close_as_windows(descriptor):
            text_mode.discard(descriptor)
            real_close(descriptor)

        monkeypatch.setattr(os, "O_BINARY", binary, raising=False)
        monkeypatch.setattr(os, "open", open_as_windows)
        monkeypatch.setattr(os, "read", read_as_windows)
        monkeypatch.setattr(os, "close", close_as_windows)
    data = b"one\r\ntwo\x1athree\r\n"
    (tmp_path / "note.txt").write_bytes(data)
    subject = make_store(tmp_path)

    subject.begin_turn("changed")
    subject.note_capture("note.txt", b"earlier\r\n")
    assert subject.seal() is not None
    assert subject.load_sides(0, "note.txt").after == data

    subject.begin_turn("left as it was")
    subject.note_capture("note.txt", data)
    assert subject.seal() is None


def test_unchanged_paths_are_never_reported(tmp_path: Path) -> None:
    (tmp_path / "same.txt").write_bytes(b"same\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_capture("same.txt", b"same\n")

    assert subject.seal() is None


def test_trailing_newline_change_is_a_byte_modification(tmp_path: Path) -> None:
    """R9：净状态由存在性和字节决定；零行差只是展示数据."""
    (tmp_path / "note.txt").write_bytes(b"hello\n")
    subject = store.TurnChangeStore(tmp_path, "sess-1")  # 真实 turn_diff.diff_bytes
    subject.begin_turn("req-1")
    subject.note_capture("note.txt", b"hello")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert change.state == store.STATE_MODIFIED
    assert change.compare == store.COMPARE_FULL
    assert (change.added, change.removed) == (0, 0)
    assert manifest.totals["files"] == 1


def test_unchanged_entries_stay_out_of_the_main_list(tmp_path: Path) -> None:
    """R9：unchanged 不进主清单也不进未知区，主清单只收净变化条目."""
    (tmp_path / "same.txt").write_bytes(b"same\n")
    (tmp_path / "mod.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_capture("same.txt", b"same\n")  # 改了又改回
    subject.note_capture("mod.txt", b"old\n")

    manifest = subject.seal()
    assert manifest is not None
    assert [change.path for change in manifest.files] == ["mod.txt"]
    assert manifest.unknown == []
    assert manifest.totals["files"] == 1


def test_uncaptured_side_lands_in_the_unknown_area(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    # 只作为"被触碰候选"登记的路径：before 侧从未取得快照 → 不得当作已确认
    subject.note_paths(["never-captured.txt"])

    manifest = subject.seal()
    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert change.path == "never-captured.txt"
    assert change.state == store.STATE_UNKNOWN
    assert change.before_state == store.SIDE_UNCAPTURED
    assert change.after_state == store.SIDE_ABSENT
    assert change.reason == store.REASON_ERROR


# ---------------------------------------------------------------------------
# 5. note_tracked decision table
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("before_fp", "after_fp", "expected"),
    [
        # 任一侧 error: 前缀 → unknown
        ("error: not a git repository", "file:1", store.STATE_UNKNOWN),
        ("file:1", "error: index.lock exists", store.STATE_UNKNOWN),
        # 字面量 <clean>（防御）→ unknown
        ("<clean>", "file:1", store.STATE_UNKNOWN),
        ("file:1", "<clean>", store.STATE_UNKNOWN),
        # 两侧均非 None
        ("file:1", "file:1", None),                      # unchanged → 不出现
        ("missing", "file:2", store.STATE_MODIFIED),     # 重建
        ("missing", "symlink:2", store.STATE_MODIFIED),  # 重建（符号链接）
        ("file:1", "missing", store.STATE_DELETED),
        ("symlink:1", "missing", store.STATE_DELETED),
        ("file:1", "file:2", store.STATE_MODIFIED),
        ("file:1", "symlink:2", store.STATE_MODIFIED),
        # 一侧 None
        (None, "missing", store.STATE_DELETED),
        (None, "file:2", store.STATE_MODIFIED),
        (None, "symlink:2", store.STATE_MODIFIED),
        ("file:1", None, store.STATE_MODIFIED),
        # 冻结表：非 None → None 一律 modified（不细分子类）
        ("missing", None, store.STATE_MODIFIED),
    ],
)
def test_note_tracked_decision_table(
    tmp_path: Path,
    before_fp: str | None,
    after_fp: str | None,
    expected: str | None,
) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_tracked("agent/runtime/x.py", before_fp, after_fp)

    manifest = subject.seal()
    if expected is None:
        assert manifest is None
        return
    assert manifest is not None
    change = entries_by_path(manifest)["agent/runtime/x.py"]
    assert change.state == expected
    assert change.compare == store.COMPARE_NONE
    assert (change.added, change.removed) == (None, None)
    assert change.reason == store.REASON_TRACKED
    if expected == store.STATE_UNKNOWN:
        assert change in manifest.unknown
        assert manifest.files == []
    else:
        assert change in manifest.files
        assert manifest.unknown == []


def test_note_tracked_deleted_marks_the_after_side_absent(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_tracked("agent/x.py", "file:1", "missing")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert change.after_state == store.SIDE_ABSENT
    assert change.before_state == store.SIDE_UNCAPTURED


def test_note_tracked_without_any_fingerprint_is_unknown(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_tracked("agent/x.py", None, None)

    manifest = subject.seal()
    assert manifest is not None
    assert manifest.files == []
    assert manifest.unknown[0].state == store.STATE_UNKNOWN
    assert manifest.unknown[0].reason == store.REASON_TRACKED


def test_note_tracked_malformed_fingerprint_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    subject = make_store(tmp_path)

    with caplog.at_level(logging.WARNING, logger=store.__name__):
        subject.begin_turn("req-1")
        subject.note_tracked("agent/x.py", "file:1", "<clean>")
        manifest = subject.seal()

    assert manifest is not None
    assert manifest.unknown[0].state == store.STATE_UNKNOWN
    assert any("tracked" in record.getMessage() for record in caplog.records)


def test_snapshot_evidence_outranks_tracked_fingerprints(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_tracked("a.txt", "file:1", "file:2")
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert change.compare == store.COMPARE_FULL
    assert change.reason == ""


def test_tracked_sequence_reverting_to_original_is_not_listed(tmp_path: Path) -> None:
    """R5：同一路径多次 tracked 取"首次 before + 末次 after"，改回原样=无净改动."""
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_tracked("sample.py", "file:original", "file:changed")
    subject.note_tracked("sample.py", "file:changed", "file:original")

    assert subject.seal() is None


def test_tracked_chain_uses_first_before_and_last_after(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_tracked("created.py", "missing", "file:1")
    subject.note_tracked("created.py", "file:1", "file:2")
    subject.note_tracked("dead.py", "file:1", "file:2")
    subject.note_tracked("dead.py", "file:2", "missing")
    subject.note_tracked("drift.py", "file:1", "file:2")
    subject.note_tracked("drift.py", "file:2", "file:3")

    manifest = subject.seal()
    assert manifest is not None
    changes = {change.path: change for change in manifest.files}
    assert changes["created.py"].state == store.STATE_MODIFIED  # missing → file:2
    assert changes["dead.py"].state == store.STATE_DELETED      # file:1 → missing
    assert changes["drift.py"].state == store.STATE_MODIFIED    # file:1 → file:3
    assert all(change.reason == store.REASON_TRACKED for change in changes.values())


def test_snapshot_evidence_outranks_a_repeated_tracked_chain(tmp_path: Path) -> None:
    """混合规则：字节快照优先于指纹；快照存在时 tracked 链不参与判定."""
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_tracked("a.txt", "file:1", "file:2")
    subject.note_tracked("a.txt", "file:2", "missing")  # 单看链=deleted
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert change.state == store.STATE_MODIFIED
    assert change.compare == store.COMPARE_FULL
    assert change.reason == ""


# ---------------------------------------------------------------------------
# path normalization and the per-turn path budget
# ---------------------------------------------------------------------------

def test_paths_are_normalized_relative_to_the_workspace(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_capture(str(tmp_path / "sub" / "a.txt"), b"old\n", checkpoint_id="cp1")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert change.path == "sub/a.txt"
    assert change.display == "sub/a.txt"


def test_relative_and_absolute_paths_deduplicate(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")

    subject.note_paths(["a.txt"])
    subject.note_capture(str((tmp_path / "a.txt").resolve()), b"old\n")

    manifest = subject.seal()
    assert manifest is not None
    assert [change.path for change in manifest.files] == ["a.txt"]


def test_capture_target_outside_the_workspace_uses_the_stable_identity(tmp_path: Path) -> None:
    """R4：display 只是展示名；before/after 以传入的稳定绝对路径为目标."""
    files = tmp_path / "files"
    sandbox = tmp_path / "sandbox"
    files.mkdir()
    sandbox.mkdir()
    (files / "note.txt").write_bytes(b"after\n")
    (sandbox / "note.txt").write_bytes(b"UNRELATED\n")
    subject = make_store(sandbox, "review")
    subject.begin_turn("req-1")

    subject.note_capture(str(files / "note.txt"), b"before\n", display="note.txt")

    manifest = subject.seal()
    assert manifest is not None
    change = manifest.files[0]
    assert (change.path, change.display) == ("note.txt", "note.txt")
    assert change.state == store.STATE_MODIFIED
    assert subject.load_sides(0, "note.txt").after == b"after\n"


def test_target_directory_swap_degrades_the_after_read(tmp_path: Path) -> None:
    """R4/R1：目标目录被换成符号链接时，after 不跟随替换路径，安全降级."""
    workspace = tmp_path / "files"
    workspace.mkdir()
    (workspace / "note.txt").write_bytes(b"before\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "note.txt").write_bytes(b"SWAPPED\n")
    subject = make_store(tmp_path)
    subject.begin_turn("req-1")
    subject.note_capture(str(workspace / "note.txt"), b"before\n", display="note.txt")

    workspace.rename(tmp_path / "files-held")
    workspace.symlink_to(elsewhere)

    manifest = subject.seal()
    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert (change.path, change.state) == ("note.txt", store.STATE_UNKNOWN)
    assert change.before_state == store.SIDE_CAPTURED
    assert change.after_state == store.SIDE_UNCAPTURED
    assert change.reason == store.REASON_ERROR
    assert subject.load_sides(0, "note.txt").after is None


def test_path_budget_truncates_candidates(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    limits = store.TurnChangeLimits(max_paths_per_turn=2)
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_bytes(b"x\n")
    subject = make_store(tmp_path, limits=limits)

    with caplog.at_level(logging.WARNING, logger=store.__name__):
        subject.begin_turn("req-1")
        subject.note_paths(["a.txt", "b.txt", "c.txt"])
        manifest = subject.seal()

    assert manifest is not None
    assert {change.path for change in manifest.unknown} == {"a.txt", "b.txt"}
    assert any("path" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------------
# 6. persistence and read-back (layout, manifest, load_sides, FIFO, close)
# ---------------------------------------------------------------------------

def seal_modified_turn(
    subject: store.TurnChangeStore,
    tmp_path: Path,
    *,
    name: str = "a.txt",
    before: bytes = b"old\n",
    after: bytes = b"new\n",
    request_id: str = "req-1",
) -> store.TurnChangesManifest:
    (tmp_path / name).write_bytes(after)
    subject.begin_turn(request_id)
    subject.note_capture(name, before, checkpoint_id=f"cp-{name}")
    manifest = subject.seal()
    assert manifest is not None
    return manifest


def test_turn_directory_layout_and_owner_marker(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")

    manifest = seal_modified_turn(subject, tmp_path)

    area = session_dir(tmp_path)
    turn = area / "turn-1"
    assert sorted(path.name for path in turn.iterdir()) == [
        "after.0.bin",
        "before.0.bin",
        "manifest.json",
    ]
    assert (turn / "before.0.bin").read_bytes() == b"old\n"
    assert (turn / "after.0.bin").read_bytes() == b"new\n"
    # owner.json 位于会话目录（且先于任何快照内容创建）
    assert sorted(path.name for path in area.iterdir()) == ["owner.json", "turn-1", "turns.json"]
    owner = json.loads((area / "owner.json").read_text(encoding="utf-8"))
    assert owner["session_id"] == "sess-1"
    assert owner["pid"] == os.getpid()
    payload = json.loads((turn / "manifest.json").read_text(encoding="utf-8"))
    assert payload["session_id"] == "sess-1"
    assert payload["request_id"] == "req-1"
    assert payload["turn_seq"] == 1
    assert payload["totals"] == manifest.totals


def test_only_captured_sides_are_persisted(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    (tmp_path / "created.txt").write_bytes(b"fresh\n")
    subject.begin_turn("req-1")
    subject.note_absent("created.txt")
    assert subject.seal() is not None

    turn = session_dir(tmp_path) / "turn-1"

    assert sorted(path.name for path in turn.iterdir()) == ["after.0.bin", "manifest.json"]


def test_manifest_and_load_sides_read_only_the_snapshot_area(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    seal_modified_turn(subject, tmp_path)
    (tmp_path / "a.txt").write_bytes(b"edited after seal\n")  # seal 后外部再编辑

    manifest = subject.manifest(0)
    assert manifest is not None
    assert [change.path for change in manifest.files] == ["a.txt"]
    assert manifest.request_id == "req-1"
    sides = subject.load_sides(0, "a.txt")
    assert (sides.before, sides.after) == (b"old\n", b"new\n")
    assert (sides.before_state, sides.after_state) == (store.SIDE_CAPTURED, store.SIDE_CAPTURED)

    (tmp_path / "a.txt").unlink()  # 快照区自足：不再读当前磁盘

    assert subject.load_sides(0, "a.txt").after == b"new\n"
    assert subject.manifest(1) is None


def test_manifest_round_trips_the_unknown_area(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    (tmp_path / "real.txt").write_bytes(b"new\n")
    subject.begin_turn("req-1")
    subject.note_capture("real.txt", b"old\n")
    subject.note_tracked("ghost.py", "file:1", "error: bad revision")

    manifest = subject.seal()
    reread = subject.manifest(0)

    assert manifest is not None
    assert reread is not None
    assert reread == manifest
    assert [change.path for change in reread.files] == ["real.txt"]
    assert [change.path for change in reread.unknown] == ["ghost.py"]
    assert reread.unknown[0].reason == store.REASON_TRACKED
    assert reread.unknown[0].state == store.STATE_UNKNOWN


def test_manifest_offsets_follow_turn_seq_order(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    for index in range(2):
        (tmp_path / "a.txt").write_bytes(f"after-{index}\n".encode())
        subject.begin_turn(f"req-{index}")
        subject.note_capture("a.txt", f"before-{index}\n".encode())
        assert subject.seal() is not None

    newest = subject.manifest(0)
    older = subject.manifest(1)

    assert newest is not None
    assert older is not None
    assert (newest.turn_seq, older.turn_seq) == (2, 1)
    assert (newest.request_id, older.request_id) == ("req-1", "req-0")
    assert subject.load_sides(1, "a.txt").after == b"after-0\n"
    assert subject.manifest(2) is None
    assert subject.manifest(-1) is None


def test_load_sides_three_states(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    (tmp_path / "created.txt").write_bytes(b"fresh\n")
    subject.begin_turn("req-1")
    subject.note_absent("created.txt")
    assert subject.seal() is not None

    created = subject.load_sides(0, "created.txt")
    assert (created.before, created.before_state) == (None, store.SIDE_ABSENT)
    assert (created.after, created.after_state) == (b"fresh\n", store.SIDE_CAPTURED)

    seal_modified_turn(subject, tmp_path, name="edited.txt", before=b"old\n", after=b"new\n", request_id="req-2")
    assert subject.load_sides(0, "edited.txt").after == b"new\n"

    (session_dir(tmp_path) / "turn-2" / "after.0.bin").unlink()  # 快照文件被删 = 读取失败

    broken = subject.load_sides(0, "edited.txt")
    assert (broken.after, broken.after_state) == (None, store.SIDE_UNCAPTURED)
    assert (broken.before, broken.before_state) == (b"old\n", store.SIDE_CAPTURED)
    assert subject.load_sides(9, "edited.txt") == store.LoadedSides(
        None, None, store.SIDE_UNCAPTURED, store.SIDE_UNCAPTURED
    )
    assert subject.load_sides(0, "no-such-path.txt").before_state == store.SIDE_UNCAPTURED


def test_fifo_retains_only_the_newest_turns(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(max_turns_retained=3)
    subject = make_store(tmp_path, "sess-1", limits=limits)
    for index in range(5):
        (tmp_path / "a.txt").write_bytes(f"after-{index}\n".encode())
        subject.begin_turn(f"req-{index}")
        subject.note_capture("a.txt", f"before-{index}\n".encode())
        assert subject.seal() is not None

    retained = sorted(path.name for path in session_dir(tmp_path).iterdir() if path.name.startswith("turn-"))

    assert retained == ["turn-3", "turn-4", "turn-5"]
    newest = subject.manifest(0)
    oldest_retained = subject.manifest(2)
    assert newest is not None
    assert oldest_retained is not None
    assert (newest.turn_seq, oldest_retained.turn_seq) == (5, 3)
    assert subject.manifest(3) is None
    assert subject.load_sides(2, "a.txt").before == b"before-2\n"


def test_close_removes_the_session_snapshot_area(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    seal_modified_turn(subject, tmp_path)

    subject.close()

    assert not session_dir(tmp_path).exists()
    assert subject.manifest(0) is None
    assert subject.load_sides(0, "a.txt").before is None


# ---------------------------------------------------------------------------
# 3. quota takes effect at capture time (never waits for seal)
# ---------------------------------------------------------------------------

def test_file_quota_degrades_at_capture_without_copying_bytes(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(max_file_bytes=16)
    big = b"x" * 64
    (tmp_path / "big.txt").write_bytes(b"small\n")
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("req-1")

    subject.note_capture("big.txt", big, checkpoint_id="cp1")
    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert (change.state, change.before_state) == (store.STATE_UNKNOWN, store.SIDE_UNCAPTURED)
    assert change.reason == store.REASON_QUOTA
    assert (change.compare, change.added, change.removed) == (store.COMPARE_NONE, None, None)
    assert change.checkpoint_ids == ["cp1"]

    turn = session_dir(tmp_path) / "turn-1"
    assert sorted(path.name for path in turn.iterdir()) == ["after.0.bin", "manifest.json"]
    assert big not in (turn / "after.0.bin").read_bytes()


def test_turn_quota_degrades_later_captures(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(max_file_bytes=1024, max_turn_bytes=11)
    (tmp_path / "a.txt").write_bytes(b"x")
    (tmp_path / "b.txt").write_bytes(b"y")
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("req-1")

    subject.note_capture("a.txt", b"01234567\n")  # 9 字节 ≤ 回合预算
    subject.note_capture("b.txt", b"01234567\n")  # 再 9 字节 → 超回合预算
    manifest = subject.seal()

    assert manifest is not None
    confirmed = {change.path: change for change in manifest.files}
    degraded = {change.path: change for change in manifest.unknown}
    assert set(confirmed) == {"a.txt"}
    assert confirmed["a.txt"].compare == store.COMPARE_FULL
    assert list(degraded) == ["b.txt"]
    assert degraded["b.txt"].before_state == store.SIDE_UNCAPTURED
    assert degraded["b.txt"].reason == store.REASON_QUOTA


def test_session_quota_evicts_the_oldest_turn_at_capture_time(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(
        max_file_bytes=1024,
        max_turn_bytes=4096,
        max_session_bytes=160,
        max_turns_retained=10,
    )
    subject = make_store(tmp_path, "sess-1", limits=limits)
    for index in (1, 2):
        seal_modified_turn(
            subject,
            tmp_path,
            before=b"A" * 32,
            after=b"B" * 32,
            request_id=f"req-{index}",
        )
    area = session_dir(tmp_path)

    (tmp_path / "a.txt").write_bytes(b"C" * 32)
    subject.begin_turn("req-3")
    subject.note_capture("a.txt", b"D" * 64)  # 会话预算不足 → 先淘汰最旧回合

    assert not (area / "turn-1").exists()
    assert (area / "turn-2").exists()

    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files[0].compare == store.COMPARE_FULL  # 淘汰后可容纳 → 不降级
    assert (area / "turn-3").exists()


def test_session_quota_degrades_when_no_room_can_be_made(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(
        max_file_bytes=1024,
        max_turn_bytes=4096,
        max_session_bytes=64,
        max_turns_retained=10,
    )
    subject = make_store(tmp_path, "sess-1", limits=limits)
    seal_modified_turn(subject, tmp_path, before=b"A" * 8, after=b"B" * 8)
    area = session_dir(tmp_path)

    (tmp_path / "big.txt").write_bytes(b"C" * 8)
    subject.begin_turn("req-2")
    subject.note_capture("big.txt", b"D" * 96)  # 淘汰最旧回合后仍超会话预算

    assert not (area / "turn-1").exists()

    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert change.path == "big.txt"
    assert change.reason == store.REASON_QUOTA
    assert change.before_state == store.SIDE_UNCAPTURED


def test_after_side_over_the_file_quota_lands_in_the_unknown_area(tmp_path: Path) -> None:
    limits = store.TurnChangeLimits(max_file_bytes=16)
    (tmp_path / "a.txt").write_bytes(b"y" * 64)
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("req-1")

    subject.note_capture("a.txt", b"x\n")
    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert change.before_state == store.SIDE_CAPTURED
    assert change.after_state == store.SIDE_UNCAPTURED
    assert change.reason == store.REASON_QUOTA
    turn = session_dir(tmp_path) / "turn-1"
    # 超限的 after 字节绝不落盘；已捕获的 before 仍保留
    assert sorted(path.name for path in turn.iterdir()) == ["before.0.bin", "manifest.json"]


# ---------------------------------------------------------------------------
# 11. failures only degrade
# ---------------------------------------------------------------------------

def test_seal_survives_a_manifest_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = make_store(tmp_path, "sess-1")
    (tmp_path / "a.txt").write_bytes(b"new\n")

    def boom(path, payload):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_write_json_atomic", boom)

    subject.begin_turn("req-1")
    subject.note_capture("a.txt", b"old\n")
    manifest = subject.seal()

    assert manifest is not None
    assert [change.path for change in manifest.files] == ["a.txt"]
    assert manifest.files[0].state == store.STATE_MODIFIED
    assert subject.manifest(0) is None  # 未落盘 → 读回为空，仅降级


def test_store_survives_an_unusable_snapshot_root(tmp_path: Path) -> None:
    root = tmp_path / "blocked"
    root.mkdir()
    (root / "sess-1").write_text("not a directory", encoding="utf-8")
    (tmp_path / "a.txt").write_bytes(b"new\n")

    subject = make_store(tmp_path, "sess-1", root=root)
    subject.begin_turn("req-1")
    subject.note_capture("a.txt", b"old\n")
    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files[0].state == store.STATE_MODIFIED
    assert subject.manifest(0) is None


def test_manifest_read_failure_returns_none(tmp_path: Path) -> None:
    subject = make_store(tmp_path, "sess-1")
    seal_modified_turn(subject, tmp_path)
    (session_dir(tmp_path) / "turn-1" / "manifest.json").write_text("{not json", encoding="utf-8")

    assert subject.manifest(0) is None
    sides = subject.load_sides(0, "a.txt")
    assert (sides.before, sides.after) == (None, None)
    assert (sides.before_state, sides.after_state) == (store.SIDE_UNCAPTURED, store.SIDE_UNCAPTURED)


# ---------------------------------------------------------------------------
# 13. turn compute budget
# ---------------------------------------------------------------------------

def test_turn_compute_budget_degrades_unfinished_entries(tmp_path: Path) -> None:
    clock = FakeClock()
    differ = FakeDiffer(clock=clock, advance_ms=90)
    limits = store.TurnChangeLimits(diff_deadline_ms=50, compute_budget_ms=100)
    subject = make_store(tmp_path, "sess-1", differ=differ, clock=clock, limits=limits)
    subject.begin_turn("req-1")
    for index in range(5):
        name = f"f{index}.txt"
        (tmp_path / name).write_bytes(f"new-{index}\n".encode())
        subject.note_capture(name, f"old-{index}\n".encode())
    started = clock.now

    wall = time.perf_counter()
    manifest = subject.seal()
    elapsed = time.perf_counter() - wall

    assert manifest is not None
    assert elapsed < 2.0  # 注入的慢 differ 不得拖住收尾
    assert [change.path for change in manifest.files] == ["f0.txt", "f1.txt"]
    assert len(differ.calls) == 2  # 超预算后不再发起新的 diff
    assert differ.calls[0].deadline == pytest.approx(started + 0.050)  # 单文件截止时间
    assert differ.calls[1].deadline == pytest.approx(started + 0.100)  # 取较早的回合截止时间
    assert all(call.max_bytes == limits.max_file_bytes for call in differ.calls)
    assert clock.now - started == pytest.approx(0.180)

    confirmed = [change for change in manifest.files if change.compare == store.COMPARE_FULL]
    assert len(confirmed) == 2
    # 未读到 after 的条目进未知区，绝不报成 modified（review F5）
    assert [change.path for change in manifest.unknown] == ["f2.txt", "f3.txt", "f4.txt"]
    assert all(change.added is None and change.removed is None for change in manifest.unknown)
    assert all(change.reason == REASON_TIMEOUT for change in manifest.unknown)
    assert all(change.state == store.STATE_UNKNOWN for change in manifest.unknown)
    assert all(change.after_state == store.SIDE_UNCAPTURED for change in manifest.unknown)


# ---------------------------------------------------------------------------
# 14. cancellation is forwarded to the differ and respected
# ---------------------------------------------------------------------------

def test_seal_forwards_the_cancel_callback_to_the_differ(tmp_path: Path) -> None:
    clock = FakeClock()
    state = {"cancelled": False}
    received: list[Callable[[], bool] | None] = []

    def differ(before, after, *, max_bytes=DEFAULT_MAX_FILE_BYTES, deadline=None, cancelled=None):
        received.append(cancelled)
        state["cancelled"] = True  # 首个文件算完后取消
        return DiffStats(added=1, removed=1, quality=DIFF_FULL, reason="", truncated=False)

    def cancel_flag() -> bool:
        return state["cancelled"]

    limits = store.TurnChangeLimits(diff_deadline_ms=60_000, compute_budget_ms=60_000)  # 长 deadline
    subject = make_store(tmp_path, "sess-1", differ=differ, clock=clock, limits=limits)
    subject.begin_turn("req-1")
    for index in range(3):
        name = f"f{index}.txt"
        (tmp_path / name).write_bytes(f"new-{index}\n".encode())
        subject.note_capture(name, f"old-{index}\n".encode())

    manifest = subject.seal(cancelled=cancel_flag)

    assert manifest is not None
    assert received and received[0] is cancel_flag  # 取消信号贯通给 differ
    assert len(received) == 1                       # 取消后不再发起新的 diff
    assert [change.path for change in manifest.files] == ["f0.txt"]
    assert manifest.files[0].compare == store.COMPARE_FULL
    # 取消时未读取 after 的条目进未知区（review F5）
    assert [change.path for change in manifest.unknown] == ["f1.txt", "f2.txt"]
    assert all(change.compare == store.COMPARE_NONE for change in manifest.unknown)
    assert all(change.reason == REASON_CANCELLED for change in manifest.unknown)
    assert all(change.state == store.STATE_UNKNOWN for change in manifest.unknown)
    assert all(change.after_state == store.SIDE_UNCAPTURED for change in manifest.unknown)


def test_cancel_before_any_diff_degrades_every_entry(tmp_path: Path) -> None:
    clock = FakeClock()
    differ = FakeDiffer(clock=clock)
    limits = store.TurnChangeLimits(diff_deadline_ms=60_000, compute_budget_ms=60_000)
    subject = make_store(tmp_path, "sess-1", differ=differ, clock=clock, limits=limits)
    subject.begin_turn("req-1")
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal(cancelled=lambda: True)

    assert manifest is not None
    assert differ.calls == []
    # 未读取 after：条目进未知区，不得报成 modified（review F5）
    assert manifest.files == []
    change = manifest.unknown[0]
    assert (change.compare, change.added, change.removed) == (store.COMPARE_NONE, None, None)
    assert change.reason == REASON_CANCELLED
    assert change.state == store.STATE_UNKNOWN
    assert change.after_state == store.SIDE_UNCAPTURED


def test_cancelled_seal_never_reads_source_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R6：预先取消时停止新的可放弃捕获——seal 不读取任何 after."""
    limits = store.TurnChangeLimits(compute_budget_ms=20)
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("req-1")
    for index in range(5):
        path = tmp_path / f"note-{index}.txt"
        path.write_bytes(b"after\n")
        subject.note_capture(path.name, b"before\n")

    reads: list[str] = []
    original = store._read_bytes_bounded

    def counting_read(path: Path, limit: int) -> bytes:
        reads.append(path.name)
        return original(path, limit)

    monkeypatch.setattr(store, "_read_bytes_bounded", counting_read)

    start = time.perf_counter()
    manifest = subject.seal(cancelled=lambda: True)
    elapsed = time.perf_counter() - start

    assert manifest is not None
    assert reads == []
    assert elapsed < 0.5
    # 未读取 after 的条目全部进未知区（review F5）
    assert manifest.files == []
    assert [change.path for change in manifest.unknown] == [f"note-{i}.txt" for i in range(5)]
    assert all(change.compare == store.COMPARE_NONE for change in manifest.unknown)
    assert all(change.added is None and change.removed is None for change in manifest.unknown)
    assert all(change.reason == REASON_CANCELLED for change in manifest.unknown)
    assert all(change.after_state == store.SIDE_UNCAPTURED for change in manifest.unknown)
    assert all(change.state == store.STATE_UNKNOWN for change in manifest.unknown)


def test_expired_budget_stops_further_source_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R6：收尾预算覆盖读取路径；过期后不再发起新的读取."""
    clock = FakeClock()
    limits = store.TurnChangeLimits(compute_budget_ms=100, diff_deadline_ms=50)
    subject = make_store(tmp_path, "sess-1", clock=clock, limits=limits)
    subject.begin_turn("req-1")
    for index in range(6):
        path = tmp_path / f"f{index}.txt"
        path.write_bytes(b"after\n")
        subject.note_capture(path.name, b"before\n")

    reads: list[str] = []
    original = store._read_bytes_bounded

    def slow_read(path: Path, limit: int) -> bytes:
        reads.append(path.name)
        clock.advance(60)  # 每次读取 60ms（模拟慢 I/O）
        return original(path, limit)

    monkeypatch.setattr(store, "_read_bytes_bounded", slow_read)

    manifest = subject.seal()

    assert manifest is not None
    assert reads == ["f0.txt", "f1.txt"]  # 预算 100ms：第二次读取后过期
    assert [change.path for change in manifest.files] == ["f0.txt", "f1.txt"]
    assert manifest.files[0].compare == store.COMPARE_FULL
    # f1 已读取但没算完计数：两侧已确定，留在主清单但降级
    assert manifest.files[1].compare == store.COMPARE_NONE
    assert manifest.files[1].reason == REASON_TIMEOUT
    # f2 起未读取 after：进未知区（review F5）
    assert [change.path for change in manifest.unknown] == [f"f{i}.txt" for i in range(2, 6)]
    assert all(change.reason == REASON_TIMEOUT for change in manifest.unknown)
    assert all(change.after_state == store.SIDE_UNCAPTURED for change in manifest.unknown)


@pytest.mark.parametrize("reason", [REASON_CANCELLED, REASON_TIMEOUT])
def test_unread_after_never_claims_a_net_change(tmp_path: Path, reason: str) -> None:
    """F5：取消/超时下"改回原样"与"创建后删除"都不得报成 modified/added.

    两者 after 均未读取（无字节、无指纹），净变化语义无从确认；只能进未知区
    并保留停止原因，绝不能把"可能写过"当成"已确认的净改动"。
    """
    clock = FakeClock()
    budget = 0 if reason == REASON_TIMEOUT else 60_000  # 0ms 预算 = 收尾前已过期
    limits = store.TurnChangeLimits(compute_budget_ms=budget, diff_deadline_ms=60_000)
    subject = make_store(tmp_path, "sess-1", clock=clock, limits=limits)
    subject.begin_turn("req-1")
    reverted = tmp_path / "reverted.txt"
    reverted.write_bytes(b"original\n")
    subject.note_capture("reverted.txt", b"original\n")
    reverted.write_bytes(b"edited\n")
    reverted.write_bytes(b"original\n")  # 改回原样
    added = tmp_path / "created-then-deleted.txt"
    subject.note_absent(added)
    added.write_bytes(b"temporary\n")
    added.unlink()  # 创建后删除

    manifest = subject.seal(cancelled=(lambda: True) if reason == REASON_CANCELLED else None)

    assert manifest is not None
    assert manifest.files == []  # 实际净变化为零：绝不出现 modified/added
    assert sorted(change.path for change in manifest.unknown) == [
        "created-then-deleted.txt",
        "reverted.txt",
    ]
    assert all(change.state == store.STATE_UNKNOWN for change in manifest.unknown)
    assert all(change.after_state == store.SIDE_UNCAPTURED for change in manifest.unknown)
    assert all(change.reason == reason for change in manifest.unknown)
    assert all(change.added is None and change.removed is None for change in manifest.unknown)
    assert subject.load_sides(0, "reverted.txt").after is None  # 未读最终字节


def test_cancelled_seal_starts_no_snapshot_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F4：已取消的收尾不启动快照写入，清单照常落盘（写入受预算门控）."""
    limits = store.TurnChangeLimits(compute_budget_ms=20)
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("req-1")
    for index in range(5):
        path = tmp_path / f"note-{index}.txt"
        path.write_bytes(b"after\n")
        subject.note_capture(path.name, b"before\n")

    writes: list[str] = []
    original = store._write_bytes

    def slow_write(path, data):
        writes.append(Path(path).name)
        time.sleep(0.04)  # 受控慢 I/O（注入延迟，不是机器基准）
        original(path, data)

    monkeypatch.setattr(store, "_write_bytes", slow_write)

    start = time.perf_counter()
    manifest = subject.seal(cancelled=lambda: True)
    elapsed = time.perf_counter() - start

    assert manifest is not None
    assert writes == []  # 取消后不启动任何快照写入
    assert elapsed < 0.15
    assert (subject.session_dir / "turn-1" / "manifest.json").exists()  # 台账仍落盘
    assert manifest.files == []
    assert all(change.reason == "cancelled" for change in manifest.unknown)


def test_cancelled_seal_skips_eviction(tmp_path: Path) -> None:
    """F4：取消的收尾不淘汰旧回合（淘汰属于可放弃工作）."""
    limits = store.TurnChangeLimits(compute_budget_ms=60_000, max_turns_retained=1)
    subject = make_store(tmp_path, "sess-1", limits=limits)
    for index in range(2):
        subject.begin_turn(f"old-{index}")
        (tmp_path / f"old-{index}.txt").write_bytes(b"new\n")
        subject.note_absent(f"old-{index}.txt")
        assert subject.seal() is not None
    assert len(subject._turn_dirs()) == 1  # 正常收尾：FIFO 只留最新一个

    subject.begin_turn("cancelled")
    (tmp_path / "cancelled.txt").write_bytes(b"new\n")
    subject.note_absent("cancelled.txt")
    assert subject.seal(cancelled=lambda: True) is not None

    # 取消的收尾不淘汰：旧回合保留，FIFO 推迟到下一次正常收尾
    assert len(subject._turn_dirs()) == 2


def test_normal_seal_still_writes_snapshots_without_delay(tmp_path: Path) -> None:
    """F4：无取消/无过期时快照照常写入（门控不得误伤正常收尾）."""
    subject = make_store(tmp_path, "sess-1")
    subject.begin_turn("req-1")
    (tmp_path / "a.txt").write_bytes(b"after\n")
    subject.note_capture("a.txt", b"before\n")

    manifest = subject.seal()

    assert manifest is not None
    turn_dir = subject.session_dir / "turn-1"
    assert sorted(path.name for path in turn_dir.iterdir()) == [
        "after.0.bin",
        "before.0.bin",
        "manifest.json",
    ]
    assert subject.load_sides(0, "a.txt").after == b"after\n"


def test_external_cancel_is_processed_during_cooperative_seal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4：事件循环上排期的取消能在合作式收尾中得到处理，读取随即停止.

    复现口径：10ms 后 `call_later` 触发 task.cancel()，每次 after 读取注入
    40ms 延迟。同步 seal 会让取消回调直到收尾结束才运行；合作式 seal_async
    在条目之间让出控制权，取消在让出点投递并停止剩余读取。
    """
    import asyncio

    limits = store.TurnChangeLimits(compute_budget_ms=60_000, diff_deadline_ms=60_000)
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("external-cancel")
    for index in range(5):
        path = tmp_path / f"note-{index}.txt"
        path.write_bytes(b"after\n")
        subject.note_capture(path.name, b"before\n")

    reads: list[str] = []
    original = store._read_bytes_bounded

    def slow_read(path, limit):
        reads.append(Path(path).name)
        time.sleep(0.04)  # 受控慢 I/O
        return original(path, limit)

    monkeypatch.setattr(store, "_read_bytes_bounded", slow_read)
    observed: dict[str, float] = {}

    async def scenario():
        task = asyncio.current_task()
        assert task is not None
        start = time.monotonic()

        def request_cancel() -> None:
            observed["cancel_callback_ms"] = (time.monotonic() - start) * 1000.0
            task.cancel()

        asyncio.get_running_loop().call_later(0.01, request_cancel)
        try:
            await subject.seal_async(cancelled=lambda: bool(task.cancelling()))
        except asyncio.CancelledError:
            # 收尾完成后取消语义继续传播（review G2）
            observed["cancel_propagated"] = True
        observed["seal_return_ms"] = (time.monotonic() - start) * 1000.0
        await asyncio.sleep(0)
        return subject.take_stopped_manifest()

    manifest = asyncio.run(scenario())

    assert observed.get("cancel_propagated") is True
    assert manifest is not None
    # 取消在让出点投递：剩余的读取全部停止（只允许在途的最后一个条目算完）
    assert 1 <= len(reads) <= 3, f"external cancel must stop new reads; reads={reads}"
    assert observed["cancel_callback_ms"] < observed["seal_return_ms"]  # 收尾期间处理
    assert subject.seal_stopped_by_cancel is True
    assert len(manifest.files) == len(reads)  # 已读条目：两侧已确定 → 主清单
    assert len(manifest.unknown) == 5 - len(reads)  # 未读条目 → 未知区
    assert all(change.reason == "cancelled" for change in manifest.unknown)
    assert all(change.after_state == store.SIDE_UNCAPTURED for change in manifest.unknown)


def test_after_read_refuses_a_parent_swap_between_check_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1：父目录锚与 lstat 检查通过后、实际 open 前被换成链接，也不得读到链外."""
    workspace = tmp_path / "workspace"
    folder = workspace / "sub"
    outside = tmp_path / "outside"
    folder.mkdir(parents=True)
    outside.mkdir()
    target = folder / "note.txt"
    target.write_bytes(b"inside\n")
    (outside / "note.txt").write_bytes(b"SYNTHETIC-OUTSIDE-DATA\n")

    subject = make_store(workspace)
    subject.begin_turn("parent-swap")
    subject.note_capture(target, b"before\n")

    original = store._read_bytes_bounded
    swapped = False

    def swap_then_open(path, limit, **kwargs):
        nonlocal swapped
        if not swapped:
            folder.rename(workspace / "sub-held")
            folder.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original(path, limit, **kwargs)

    monkeypatch.setattr(store, "_read_bytes_bounded", swap_then_open)
    manifest = subject.seal()

    assert swapped is True
    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert change.path == "sub/note.txt"
    assert change.after_state == store.SIDE_UNCAPTURED
    assert subject.load_sides(0, "sub/note.txt").after is None


def test_snapshot_write_refuses_a_storage_swap_between_check_and_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G1：存储链检查通过后、首次快照写入前换掉 session 目录，不得写到链外."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    (outside / "turn-1").mkdir(parents=True)
    target = workspace / "note.txt"
    target.write_bytes(b"after\n")

    subject = store.TurnChangeStore(workspace, "review", root=tmp_path / "ledger")
    subject.begin_turn("storage-swap")
    subject.note_capture(target, b"before\n")

    original = store._write_bytes
    swapped = False

    def swap_then_write(path, data, **kwargs):
        nonlocal swapped
        if not swapped:
            subject.session_dir.rename(subject.root / "review-held")
            subject.session_dir.symlink_to(outside, target_is_directory=True)
            swapped = True
        original(path, data, **kwargs)

    monkeypatch.setattr(store, "_write_bytes", swap_then_write)
    subject.seal()

    assert swapped is True
    created = sorted(
        str(path.relative_to(outside)) for path in outside.rglob("*") if path.is_file()
    )
    assert created == []


def test_deadline_between_snapshot_sides_skips_the_second_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3：两个快照侧之间也检查预算；过期后不再启动第二次写入."""
    clock = FakeClock()
    subject = make_store(
        tmp_path, "sess-1", clock=clock, limits=store.TurnChangeLimits(compute_budget_ms=20)
    )
    target = tmp_path / "note.txt"
    target.write_bytes(b"new\n")
    subject.begin_turn("write-deadline")
    subject.note_capture(target, b"old\n")

    writes: list[str] = []
    original = store._write_bytes

    def slow_write(path, data, **kwargs):
        writes.append(Path(path).name)
        clock.advance(40)  # 40ms 的受控慢写入（推进假时钟，与既有预算测试同口径）
        original(path, data, **kwargs)

    monkeypatch.setattr(store, "_write_bytes", slow_write)
    manifest = subject.seal()

    assert manifest is not None
    # 预算 20ms：40ms 的 before 写入之后，after 写入属于新启动的可放弃工作
    assert writes == ["before.0.bin"]
    assert subject.load_sides(0, "note.txt").after is None


def test_external_cancel_stops_persist_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G3/G2：落盘阶段的真实取消在让出点投递，停止剩余写入并传播取消."""
    import asyncio

    subject = make_store(tmp_path, "sess-1", limits=store.TurnChangeLimits(compute_budget_ms=1000))
    subject.begin_turn("cancel-during-write")
    for index in range(3):
        target = tmp_path / f"note-{index}.txt"
        target.write_bytes(b"new\n")
        subject.note_capture(target, b"old\n")

    writes: list[str] = []
    original = store._write_bytes
    observed: dict[str, float] = {}
    started = [0.0]

    def slow_write(path, data, **kwargs):
        if not writes:
            task = asyncio.current_task()

            def request_cancel() -> None:
                observed["cancel_callback_ms"] = (time.monotonic() - started[0]) * 1000.0
                task.cancel()

            asyncio.get_running_loop().call_later(0.01, request_cancel)
        writes.append(Path(path).name)
        time.sleep(0.04)  # 受控慢 I/O
        original(path, data, **kwargs)

    async def scenario():
        task = asyncio.current_task()
        assert task is not None
        started[0] = time.monotonic()
        try:
            manifest = await subject.seal_async(cancelled=lambda: bool(task.cancelling()))
            observed["seal_returned_normally"] = True
        except asyncio.CancelledError:
            # 收尾完成后的取消语义必须继续传播（review G2）
            observed["cancel_propagated"] = True
            manifest = subject.take_stopped_manifest()
        await asyncio.sleep(0)
        return manifest

    monkeypatch.setattr(store, "_write_bytes", slow_write)
    manifest = asyncio.run(scenario())

    assert observed.get("cancel_propagated") is True, observed
    assert manifest is not None
    assert len(writes) < 6, f"cancel must stop later snapshot writes; writes={writes}"
    assert (subject.session_dir / "turn-1" / "manifest.json").exists()  # 清单仍落盘


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock probe")
def test_lock_wait_respects_the_turn_budget(tmp_path: Path) -> None:
    """G3：持锁等待受回合剩余预算约束，不再固定干等."""
    import asyncio
    import subprocess as _subprocess
    import sys as _sys
    import threading

    subject = make_store(tmp_path, "sess-1", limits=store.TurnChangeLimits(compute_budget_ms=20))
    target = tmp_path / "note.txt"
    target.write_bytes(b"new\n")
    subject.begin_turn("lock-budget")
    subject.note_capture(target, b"old\n")
    script = (
        "import fcntl, sys\n"
        "f = open(sys.argv[1], 'a+b')\n"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    proc = _subprocess.Popen(
        [_sys.executable, "-c", script, str(subject._session_lock_path())],
        stdin=_subprocess.PIPE,
        stdout=_subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "locked"

    def release() -> None:
        assert proc.stdin is not None
        proc.stdin.write("release\n")
        proc.stdin.flush()

    timer = threading.Timer(0.25, release)
    try:
        timer.start()
        start = time.monotonic()
        asyncio.run(subject.seal_async())
        elapsed = time.monotonic() - start
    finally:
        timer.join(1)
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=2)

    assert elapsed < 0.15, f"lock wait must follow the turn budget; elapsed={elapsed:.3f}s"


def _hold_session_lock(path: Path):
    """Hold ``path`` from a separate process; release via ``_release_lock`` (POSIX)."""
    import subprocess as _subprocess
    import sys as _sys

    script = (
        "import fcntl, sys\n"
        "f = open(sys.argv[1], 'a+b')\n"
        "fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
        "print('locked', flush=True)\n"
        "sys.stdin.readline()\n"
    )
    child = _subprocess.Popen(
        [_sys.executable, "-c", script, str(path)],
        stdin=_subprocess.PIPE,
        stdout=_subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "locked"
    return child


def _release_lock(child) -> None:
    if child.poll() is None and child.stdin is not None:
        child.stdin.write("release\n")
        child.stdin.flush()
    child.wait(timeout=2)


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-handle semantics")
def test_close_refuses_a_replaced_session_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1：owner 核验之后、删除之前被换入同名目录 → 不得删除换入者的目录."""
    subject = make_store(tmp_path, "review")
    replacement = tmp_path / "other-session"
    replacement.mkdir()
    (replacement / "owner.json").write_text(
        json.dumps({"session_id": "other", "pid": os.getpid(), "token": "active-other-owner"}),
        encoding="utf-8",
    )
    (replacement / "keep.txt").write_bytes(b"OTHER-OWNER-DATA\n")
    held = tmp_path / "review-held"
    original = store._remove_tree_at
    swapped = False

    def swap_then_remove(parent_fd, name, **kwargs):
        nonlocal swapped
        if name == subject.session_dir.name and not swapped:
            subject.session_dir.rename(held)
            replacement.rename(subject.session_dir)
            swapped = True
        return original(parent_fd, name, **kwargs)

    monkeypatch.setattr(store, "_remove_tree_at", swap_then_remove)
    subject.close()

    assert swapped is True
    assert held.is_dir() and (held / "owner.json").is_file()  # 原目录仍在
    assert (subject.session_dir / "keep.txt").read_bytes() == b"OTHER-OWNER-DATA\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock probe")
def test_concurrent_seals_keep_their_own_session_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H2：并发 task 不得复用彼此的重入标记；外部持锁时另一个 seal 必须降级."""
    import asyncio

    first = make_store(tmp_path, "A", limits=store.TurnChangeLimits(compute_budget_ms=1000))
    second = make_store(tmp_path, "B", limits=store.TurnChangeLimits(compute_budget_ms=20))
    first.begin_turn("a")
    for index in range(5):
        path = tmp_path / f"a-{index}.txt"
        path.write_bytes(b"new\n")
        first.note_absent(path)
    second.begin_turn("b")
    target = tmp_path / "b.txt"
    target.write_bytes(b"new\n")
    second.note_absent(target)

    writes: list[str] = []
    original = store._write_bytes
    first_writing = asyncio.Event()

    def observe_write(path, data, **kwargs):
        if second.session_dir in path.parents:
            writes.append(path.name)
        if first.session_dir in path.parents:
            first_writing.set()
        original(path, data, **kwargs)

    async def scenario():
        async def seal_second() -> None:
            await first_writing.wait()
            await second.seal_async()

        await asyncio.gather(first.seal_async(), seal_second())

    holder = _hold_session_lock(second._session_lock_path())
    try:
        monkeypatch.setattr(store, "_write_bytes", observe_write)
        asyncio.run(scenario())
        assert holder.poll() is None  # 外部持有者始终在场
    finally:
        _release_lock(holder)

    assert writes == []  # B 绝不能绕过自己的文件锁写入
    assert not (second.session_dir / "turn-1" / "manifest.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock probe")
def test_quota_eviction_respects_the_turn_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3：after 捕获触发配额淘汰也要受回合预算约束；过期后不再启动删除."""
    import asyncio

    subject = make_store(
        tmp_path,
        "review",
        clock=time.monotonic,
        limits=store.TurnChangeLimits(max_session_bytes=10, compute_budget_ms=20),
    )
    target = tmp_path / "note.txt"
    target.write_bytes(b"old\n")
    subject.begin_turn("old")
    subject.note_absent(target)
    assert subject.seal() is not None
    assert subject._session_bytes() == 4
    subject.begin_turn("new")
    subject.note_capture(target, b"old\n")
    target.write_bytes(b"new\n")

    removed: list[str] = []
    original_remove = subject._remove_turn

    def observe_remove(turn_dir: Path) -> bool:
        removed.append(turn_dir.name)
        return original_remove(turn_dir)

    holder = _hold_session_lock(subject._session_lock_path())
    try:
        monkeypatch.setattr(subject, "_remove_turn", observe_remove)
        start = time.monotonic()
        manifest = asyncio.run(subject.seal_async())
        elapsed = time.monotonic() - start
        assert holder.poll() is None
    finally:
        _release_lock(holder)

    assert manifest is not None
    assert removed == []  # 预算内不启动淘汰旧回合（review H3）
    assert elapsed < 0.15, f"quota eviction must follow the turn budget; elapsed={elapsed:.3f}s"
    # after 捕获因预算降级为未捕获：条目进入 unknown 区（F5 语义）
    assert [change.after_state for change in manifest.unknown] == [store.SIDE_UNCAPTURED]
    assert manifest.files == []


def test_bounded_read_caps_the_read_size(tmp_path: Path) -> None:
    """R6：读取有字节上限（limit+1），不整份读入."""
    target = tmp_path / "big.bin"
    target.write_bytes(b"x" * 64)

    assert store._read_bytes_bounded(target, 16) == b"x" * 17


def test_after_read_growing_past_the_limit_degrades_as_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6：stat 与 read 之间文件增长越限时按配额降级（有界读取兜底）."""
    limits = store.TurnChangeLimits(max_file_bytes=16)
    (tmp_path / "grow.txt").write_bytes(b"initial")
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("req-1")
    subject.note_capture("grow.txt", b"before\n")

    monkeypatch.setattr(store, "_read_bytes_bounded", lambda path, limit: b"x" * (limit + 1))

    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files == []
    change = manifest.unknown[0]
    assert change.state == store.STATE_UNKNOWN
    assert change.after_state == store.SIDE_UNCAPTURED
    assert change.reason == store.REASON_QUOTA


def test_count_only_downgrades_by_the_differ(tmp_path: Path) -> None:
    subject = make_store(
        tmp_path,
        "sess-1",
        differ=FakeDiffer(
            result=DiffStats(
                added=None,
                removed=None,
                quality=DIFF_COARSE,
                reason=REASON_CANCELLED,
                truncated=False,
            )
        ),
    )
    subject.begin_turn("req-1")
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal()

    assert manifest is not None
    change = manifest.files[0]
    assert (change.compare, change.added, change.removed) == (store.COMPARE_COARSE, None, None)
    assert change.reason == REASON_CANCELLED


def test_binary_diff_result_stays_confirmed_without_counts(tmp_path: Path) -> None:
    subject = make_store(
        tmp_path,
        "sess-1",
        differ=FakeDiffer(
            result=DiffStats(
                added=None,
                removed=None,
                quality=DIFF_NONE,
                reason=REASON_BINARY,
                truncated=False,
            )
        ),
    )
    subject.begin_turn("req-1")
    (tmp_path / "a.bin").write_bytes(b"\x00\x01")
    subject.note_capture("a.bin", b"\x00\x02")

    manifest = subject.seal()

    assert manifest is not None
    assert manifest.unknown == []
    change = manifest.files[0]
    assert change.state == store.STATE_MODIFIED
    assert (change.compare, change.added, change.removed) == (store.COMPARE_NONE, None, None)
    assert change.reason == REASON_BINARY


def test_differ_exception_only_degrades_the_entry(tmp_path: Path) -> None:
    def boom(before, after, **kwargs):
        raise RuntimeError("differ exploded")

    subject = make_store(tmp_path, "sess-1", differ=boom)
    subject.begin_turn("req-1")
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject.note_capture("a.txt", b"old\n")

    manifest = subject.seal()

    assert manifest is not None
    change = manifest.files[0]
    assert change.state == store.STATE_MODIFIED
    assert (change.compare, change.added, change.removed) == (store.COMPARE_NONE, None, None)
    assert change.reason == store.REASON_ERROR


# ---------------------------------------------------------------------------
# 8. cleanup_orphans safety rules
# ---------------------------------------------------------------------------

def make_session_dir(root: Path, name: str, owner: str) -> Path:
    session = root / name
    (session / "turn-1").mkdir(parents=True)
    (session / "owner.json").write_text(owner, encoding="utf-8")
    return session


def test_cleanup_orphans_skips_live_owners(tmp_path: Path) -> None:
    root = tmp_path / "turn-changes"
    live = make_session_dir(root, "live", json.dumps({"session_id": "live", "pid": 4242}))

    removed = store.TurnChangeStore.cleanup_orphans(root, is_alive=lambda pid: pid == 4242)

    assert removed == []
    assert live.exists()
    assert (live / "turn-1").exists()


def test_cleanup_orphans_removes_dead_owners(tmp_path: Path) -> None:
    root = tmp_path / "turn-changes"
    dead = make_session_dir(root, "dead", json.dumps({"session_id": "dead", "pid": 99999}))
    live = make_session_dir(root, "live", json.dumps({"session_id": "live", "pid": 4242}))

    removed = store.TurnChangeStore.cleanup_orphans(root, is_alive=lambda pid: pid == 4242)

    assert removed == [str(dead)]
    assert not dead.exists()
    assert live.exists()


def test_cleanup_orphans_never_touches_unowned_directories(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "turn-changes"
    fresh = root / "just-created"          # 另一实例刚建目录、尚未写 owner
    (fresh / "turn-1").mkdir(parents=True)
    broken = root / "broken-owner"         # owner.json 不可解析
    broken.mkdir()
    (broken / "owner.json").write_text("{not json", encoding="utf-8")
    no_pid = root / "no-pid"               # owner.json 无 pid → 无法确认
    no_pid.mkdir()
    (no_pid / "owner.json").write_text(json.dumps({"session_id": "no-pid"}), encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=store.__name__):
        removed = store.TurnChangeStore.cleanup_orphans(root, is_alive=lambda pid: False)

    assert removed == []
    assert fresh.exists()
    assert (fresh / "turn-1").exists()
    assert broken.exists()
    assert no_pid.exists()
    assert "owner" in caplog.text


def test_cleanup_orphans_default_probe_keeps_a_live_session(tmp_path: Path) -> None:
    root = tmp_path / "turn-changes"
    make_session_dir(root, "self", json.dumps({"session_id": "self", "pid": os.getpid()}))

    assert store.TurnChangeStore.cleanup_orphans(root) == []
    assert (root / "self").exists()


def test_cleanup_orphans_keeps_the_session_when_liveness_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3：探活"不确定"必须保守保留（查询失败 ≠ 进程已退出）."""
    import ctypes
    from types import SimpleNamespace

    from agent.runtime import process_env

    root = tmp_path / "turn-changes"
    session = make_session_dir(
        root, "review", json.dumps({"session_id": "review", "pid": 987654321, "token": "t"})
    )

    class Kernel:
        def OpenProcess(self, *_args):
            return 123

        def GetExitCodeProcess(self, *_args):
            return 0  # 查询失败：不能据此认定已退出

        def CloseHandle(self, *_args):
            return 1

    monkeypatch.setattr(process_env, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Kernel(), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)

    removed = store.TurnChangeStore.cleanup_orphans(root)

    assert removed == []
    assert session.exists()


def test_store_takes_over_a_dead_owner_and_cleanup_keeps_it(tmp_path: Path) -> None:
    """R7：死亡 pid 的 owner 被原子接管；清理器不会把活跃目录当孤儿删除."""
    root = tmp_path / "turn-changes"
    session = root / "review"
    session.mkdir(parents=True)
    dead_pid = 987654321
    (session / "owner.json").write_text(
        json.dumps({"session_id": "review", "pid": dead_pid}), encoding="utf-8"
    )

    subject = store.TurnChangeStore(tmp_path, "review", root=root)
    subject.begin_turn("live-turn")

    owner = json.loads((session / "owner.json").read_text(encoding="utf-8"))
    assert owner["pid"] == os.getpid()
    assert owner["session_id"] == "review"

    removed = store.TurnChangeStore.cleanup_orphans(root, is_alive=lambda pid: pid != dead_pid)

    assert removed == []
    assert session.exists()


def test_second_instance_refuses_an_active_owner(tmp_path: Path) -> None:
    """R7：活跃占用须隔离或拒绝——第二实例不改 owner、不落盘、不删目录."""
    root = tmp_path / "turn-changes"
    first = store.TurnChangeStore(tmp_path, "review", root=root)
    first.begin_turn("req-1")
    (tmp_path / "a.txt").write_bytes(b"new\n")
    first.note_capture("a.txt", b"old\n")
    assert first.seal() is not None
    session = root / "review"
    owner_text = (session / "owner.json").read_text(encoding="utf-8")

    second = store.TurnChangeStore(tmp_path, "review", root=root)
    second.begin_turn("req-2")
    (tmp_path / "b.txt").write_bytes(b"new\n")
    second.note_capture("b.txt", b"old\n")
    assert second.seal() is not None  # 内存清单仍返回

    assert (session / "owner.json").read_text(encoding="utf-8") == owner_text  # 未改 owner
    assert sorted(path.name for path in session.iterdir()) == [
        "owner.json",
        "turn-1",
        "turns.json",
    ]

    second.close()
    assert session.exists()  # 非本实例所有：不删除
    assert first.manifest(0) is not None


def test_concurrent_takeover_refuses_the_late_competitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1：两个实例真正交错时，后到的接管不得覆盖活跃 owner 或删掉台账.

    探针形状：两个线程同时读到同一个"死亡 pid"旧 owner、都决定接管，先放行 A
    完成接管并写出 turn-1，再放行早已决策的 B。B 的写入必须在会话锁内被复核
    拒绝（不是串行构造两个 store），且 B.close() 不得删除 A 的 manifest。
    """
    import threading

    root = tmp_path / "ledger"
    session = root / "review"
    session.mkdir(parents=True)
    (session / "owner.json").write_text(
        json.dumps({"pid": 987654321, "session_id": "review", "token": "dead"}),
        encoding="utf-8",
    )
    ready = {key: threading.Event() for key in ("A", "B")}
    release = {key: threading.Event() for key in ("A", "B")}
    stores: dict[str, store.TurnChangeStore] = {}
    errors: list[str] = []
    original_write_owner = store.TurnChangeStore._write_owner

    def controlled_write(self, owner_path):
        key = threading.current_thread().name
        ready[key].set()
        # 两个竞争者都先"读完旧 owner、决定接管"，再按释放顺序真正写入
        assert release[key].wait(5), f"probe synchronization timeout for {key}"
        return original_write_owner(self, owner_path)

    def construct(key: str) -> None:
        try:
            stores[key] = store.TurnChangeStore(tmp_path, "review", root=root)
        except Exception as exc:  # pragma: no cover - surfaced through `errors`
            errors.append(f"{key}: {exc!r}")

    monkeypatch.setattr(store, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(store.TurnChangeStore, "_write_owner", controlled_write)

    threads = [
        threading.Thread(target=construct, args=(key,), name=key) for key in ("A", "B")
    ]
    try:
        for thread in threads:
            thread.start()
        assert all(event.wait(5) for event in ready.values()), errors

        release["A"].set()
        threads[0].join(5)
        assert "A" in stores, errors
        first = stores["A"]
        (tmp_path / "real.txt").write_bytes(b"new\n")
        first.begin_turn("A-live")
        first.note_absent("real.txt")
        assert first.seal() is not None
        manifest_path = session / "turn-1" / "manifest.json"
        assert manifest_path.exists()

        release["B"].set()
        threads[1].join(5)
        assert "B" in stores, errors
        second = stores["B"]

        assert first._owner_is_ours() is True
        assert second._owner_is_ours() is False  # 后来者不得抢走活跃所有权
        second.close()
        assert manifest_path.exists()  # 不得删除前一个实例的台账
    finally:
        for event in release.values():
            event.set()
        for thread in threads:
            thread.join(5)


# ---------------------------------------------------------------------------
# 9. turn_store_scope / current_turn_change_store
# ---------------------------------------------------------------------------

def test_turn_store_scope_sets_nests_and_resets(tmp_path: Path) -> None:
    assert store.current_turn_change_store() is None
    outer = make_store(tmp_path, "outer")
    inner = make_store(tmp_path, "inner")

    with store.turn_store_scope(outer):
        assert store.current_turn_change_store() is outer
        with store.turn_store_scope(inner):
            assert store.current_turn_change_store() is inner
        assert store.current_turn_change_store() is outer
        with store.turn_store_scope(None):
            assert store.current_turn_change_store() is None
        assert store.current_turn_change_store() is outer

    assert store.current_turn_change_store() is None


def test_turn_store_scope_resets_after_an_exception(tmp_path: Path) -> None:
    outer = make_store(tmp_path, "outer")

    with pytest.raises(ValueError), store.turn_store_scope(outer):
        raise ValueError("boom")

    assert store.current_turn_change_store() is None


def test_locked_turn_eviction_stops_instead_of_looping(tmp_path: Path) -> None:
    """A locked FIFO delete must degrade, not spin forever (review R2).

    Runs in a subprocess: the pre-fix behavior was an infinite loop, which
    would also hang the test runner itself.
    """
    import json as _json
    import subprocess as _subprocess
    import sys as _sys
    import textwrap

    script = textwrap.dedent(
        """
        import json
        import sys
        from pathlib import Path

        import agent.runtime.turn_change_store as tcs

        root = Path(sys.argv[1])
        calls = 0

        def failing_remove(parent_fd, name):
            global calls
            calls += 1
            raise PermissionError("simulated locked snapshot directory")

        tcs._remove_tree_at = failing_remove

        store = tcs.TurnChangeStore(
            root, "review", root=root / "ledger",
            limits=tcs.TurnChangeLimits(max_turns_retained=1),
        )
        second_manifest = None
        for index in range(2):
            target = root / f"file-{index}.txt"
            target.write_bytes(b"new\\n")
            store.begin_turn(f"turn-{index}")
            store.note_absent(target.name)
            second_manifest = store.seal()
        print(json.dumps({
            "done": True,
            "delete_calls": calls,
            "second_manifest_persisted": second_manifest is not None,
        }))
        """
    )
    completed = _subprocess.run(
        [_sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert completed.returncode == 0, completed.stderr
    payload = _json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["done"] is True
    # The deletion fault was exercised, yet eviction stopped after a bounded
    # number of attempts instead of looping on the same directory.
    assert 1 <= payload["delete_calls"] <= 4
    assert payload["second_manifest_persisted"] is True


def test_store_refuses_a_swapped_workspace_symlink(tmp_path: Path) -> None:
    """Constructing on a symlinked workspace refuses instead of following it.

    That is the shape of the audit path-swap attack (review R1): once the
    final component is a symbolic link, the ledger must degrade rather than
    write through it.
    """
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(OSError):
        store.TurnChangeStore(linked, "review", root=tmp_path / "ledger")


def test_store_stops_writing_after_the_workspace_is_swapped(tmp_path: Path) -> None:
    """A workspace swap after construction must not redirect writes (R1)."""
    workspace = tmp_path / "work"
    workspace.mkdir()
    ledger = store.TurnChangeStore(workspace, "review")

    ledger.begin_turn("t1")
    (workspace / "note.txt").write_bytes(b"after\n")
    ledger.note_capture("note.txt", b"before\n")

    held = tmp_path / "held"
    workspace.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace.symlink_to(outside, target_is_directory=True)

    ledger.seal()
    assert not (outside / ".astra").exists()

    ledger.close()
    assert not (outside / ".astra").exists()


def test_seal_refuses_storage_after_the_snapshot_root_is_swapped(tmp_path: Path) -> None:
    """F2：快照目录链（.astra）被换成符号链接后，封存不得在链外创建任何文件.

    探针形状：构造并开始回合后，把 ``workspace/.astra`` 改名，再把 ``.astra``
    指向外部目录。工作区 inode 未变（旧检查仍通过），但存储链身份已变，必须
    拒绝写入而不是跟随链接。
    """
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "note.txt").write_bytes(b"new\n")

    subject = store.TurnChangeStore(workspace, "review")
    subject.begin_turn("symlink-storage")
    subject.note_absent("note.txt")

    (workspace / ".astra").rename(workspace / ".astra-original")
    (workspace / ".astra").symlink_to(outside, target_is_directory=True)

    manifest = subject.seal()

    assert manifest is not None  # 内存清单仍返回（落盘被拒绝）
    created = sorted(
        str(path.relative_to(outside)) for path in outside.rglob("*") if path.is_file()
    )
    assert created == []


def test_after_read_refuses_a_symlinked_target(tmp_path: Path) -> None:
    """F2：after 读取不跟随符号链接，降级 unknown 且不复制外部内容."""
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = workspace / "note.txt"
    target.write_bytes(b"before\n")
    private = outside / "private.txt"
    private.write_bytes(b"SYNTHETIC-OUTSIDE-CONTENT\n")

    subject = store.TurnChangeStore(workspace, "review", root=tmp_path / "ledger")
    subject.begin_turn("swap-after-capture")
    subject.note_capture(target, b"before\n")

    target.unlink()
    target.symlink_to(private)

    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files == []  # 未读到最终状态 → 不进已确认清单（F5 同口径）
    change = manifest.unknown[0]
    assert change.path == "note.txt"
    assert change.after_state == store.SIDE_UNCAPTURED
    assert change.reason == store.REASON_ERROR
    assert subject.load_sides(0, "note.txt").after is None


def test_same_display_name_keeps_distinct_absolute_identities(tmp_path: Path) -> None:
    """同名但绝对目标不同的文件保持两个条目（身份键不得按展示名合并）."""
    workspace = tmp_path / "files"
    elsewhere = tmp_path / "elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    (workspace / "a.txt").write_bytes(b"mine\n")
    (elsewhere / "a.txt").write_bytes(b"theirs\n")
    subject = make_store(workspace)
    subject.begin_turn("req-1")

    subject.note_capture(elsewhere / "a.txt", b"old\n", display="a.txt")
    subject.note_capture(workspace / "a.txt", b"old\n", display="a.txt")

    manifest = subject.seal()

    assert manifest is not None
    assert len(manifest.files) == 2
    assert sorted(change.display for change in manifest.files) == ["a.txt", "a.txt"]


# ---------------------------------------------------------------------------
# completed-turn index (M3 · review R1) — the addressable "/changes" history
# ---------------------------------------------------------------------------

def index_payload(tmp_path: Path, session_id: str = "sess-1") -> dict:
    path = session_dir(tmp_path, session_id) / store.INDEX_NAME
    return json.loads(path.read_text(encoding="utf-8"))


def completed_seqs(read: store.TurnIndexRead) -> list[int]:
    return [turn.turn_seq for turn in read.records]


def test_index_absent_is_an_empty_but_available_history(tmp_path: Path) -> None:
    subject = make_store(tmp_path)

    read = subject.completed_turns()

    assert (read.ok, read.records, read.reason) == (True, [], "")


def test_missing_index_after_published_history_is_unavailable(tmp_path: Path) -> None:
    """已发布过的索引消失 → “暂不可用”，不假装没有历史、不写回（review R2）.

    快照目录仍在（turn-1/manifest.json）；查询必须只读：不修复、不写回。
    """
    subject = make_store(tmp_path)
    seal_modified_turn(subject, tmp_path, request_id="req-1")
    assert subject.completed_turns().ok is True

    index_path = session_dir(tmp_path) / store.INDEX_NAME
    assert (session_dir(tmp_path) / "turn-1" / store.MANIFEST_NAME).exists()
    index_path.unlink()

    read = subject.completed_turns()

    assert read.ok is False
    assert read.records == []
    assert read.reason
    assert not index_path.exists()  # 查询不修复、不写回


def test_missing_index_is_unavailable_for_a_later_instance_too(tmp_path: Path) -> None:
    """跨实例：曾发布过索引的会话，索引消失后对新实例同样报不可用."""
    subject = make_store(tmp_path)
    seal_modified_turn(subject, tmp_path, request_id="req-1")
    (session_dir(tmp_path) / store.INDEX_NAME).unlink()

    later = make_store(tmp_path)

    read = later.completed_turns()

    assert read.ok is False
    assert read.records == []


def test_session_history_evidence_is_read_only_and_distinguishes_states(
    tmp_path: Path,
) -> None:
    """R5 只读探测：无痕迹=False；有 turn-N/turns.json 痕迹=True；不创建目录."""
    assert store.session_history_evidence(tmp_path, "sess-x") is False
    assert not (tmp_path / ".astra" / "turn-changes" / "sess-x").exists()

    (tmp_path / ".astra" / "turn-changes" / "sess-x" / "turn-1").mkdir(parents=True)
    assert store.session_history_evidence(tmp_path, "sess-x") is True

    session_two = tmp_path / ".astra" / "turn-changes" / "sess-y"
    session_two.mkdir(parents=True)
    (session_two / store.INDEX_NAME).write_text("{}", encoding="utf-8")
    assert store.session_history_evidence(tmp_path, "sess-y") is True

    assert store.session_history_evidence(tmp_path, "sess-fresh") is False
    assert not (tmp_path / ".astra" / "turn-changes" / "sess-fresh").exists()


def test_empty_turn_index_write_rechecks_owner_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """空回合索引发布必须在会话锁内复核 owner（review R1）.

    注入：``_ensure_session_dir()`` 成功返回后、取得持久化锁之前，换入一个
    合法格式的另一 owner token（pid 存活）。本实例已失去会话所有权 → 索引
    不得写入，保守停为未落定。
    """
    subject = make_store(tmp_path)
    subject.begin_turn("empty-after-owner-change")
    original_ensure = subject._ensure_session_dir
    replacement = {
        "session_id": subject.session_id,
        "pid": os.getpid(),
        "token": "replacement-owner",
        "created_at": time.time(),
    }

    def ensure_then_replace() -> bool:
        ok = original_ensure()
        if ok:
            (subject.session_dir / store.OWNER_NAME).write_text(
                json.dumps(replacement), encoding="utf-8"
            )
        return ok

    monkeypatch.setattr(subject, "_ensure_session_dir", ensure_then_replace)

    assert subject.seal() is None  # 空回合
    assert subject._owner_is_ours() is False
    assert not (subject.session_dir / store.INDEX_NAME).exists()
    read = subject.completed_turns()
    assert read.ok is False
    assert read.records == []


def test_empty_turn_is_indexed_and_consumes_a_sequence(tmp_path: Path) -> None:
    """空回合不落 manifest，但仍占号并留下 dir=null 的记录."""
    subject = make_store(tmp_path)

    subject.begin_turn("req-1")

    assert subject.seal() is None
    read = subject.completed_turns()
    assert read.ok is True
    assert len(read.records) == 1
    (turn,) = read.records
    assert (turn.turn_seq, turn.empty, turn.dir_name) == (1, True, None)
    assert (turn.files, turn.unknown, turn.available) == (0, 0, True)
    assert turn.request_id == "req-1"
    payload = index_payload(tmp_path)
    assert payload["version"] == store.INDEX_VERSION
    assert [record["turn_seq"] for record in payload["records"]] == [1]
    assert payload["records"][0]["empty"] is True
    assert payload["records"][0]["dir"] is None
    assert not (session_dir(tmp_path) / "turn-1").exists()  # 空回合不发事件、不落盘


def test_reverted_turn_is_indexed_empty_and_older_changes_stay_addressable(
    tmp_path: Path,
) -> None:
    """有改动 → 无操作 → 改回原样：@1/@2 空态可辨、@3 仍是原改动."""
    subject = make_store(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"new\n")
    subject.begin_turn("req-1")
    subject.note_capture("a.txt", b"old\n")
    original = subject.seal()
    assert original is not None
    assert original.turn_seq == 1

    subject.begin_turn("req-2")
    assert subject.seal() is None  # 无操作回合

    (tmp_path / "a.txt").write_bytes(b"new\n")  # 改回原样：before == 盘上内容
    subject.begin_turn("req-3")
    subject.note_capture("a.txt", b"new\n")
    assert subject.seal() is None

    read = subject.completed_turns()

    assert read.ok is True
    assert completed_seqs(read) == [3, 2, 1]
    newest, middle, oldest = read.records
    assert (newest.empty, newest.dir_name) == (True, None)
    assert (middle.empty, middle.dir_name) == (True, None)
    assert (oldest.empty, oldest.files, oldest.unknown) == (False, 1, 0)
    assert oldest.available is True
    assert subject.manifest_for(3) is None
    assert subject.manifest_for(2) is None
    assert subject.manifest_for(1) == original
    assert subject.manifest_for(4) is None


def test_index_stays_bounded_and_sequences_never_go_backwards(tmp_path: Path) -> None:
    """混合空/非空回合：磁盘索引 ≤N 条、seq 单调、出窗记录目录一并淘汰."""
    limits = store.TurnChangeLimits(max_turns_retained=3)
    subject = make_store(tmp_path, "sess-1", limits=limits)
    seqs: list[int] = []

    for index in range(7):
        subject.begin_turn(f"req-{index}")
        if index % 2 == 0:
            (tmp_path / "a.txt").write_bytes(f"after-{index}\n".encode())
            subject.note_capture("a.txt", f"before-{index}\n".encode())
            manifest = subject.seal()
            assert manifest is not None
            seqs.append(manifest.turn_seq)
        else:
            assert subject.seal() is None

    payload = index_payload(tmp_path)
    assert len(payload["records"]) <= 3
    assert [record["turn_seq"] for record in payload["records"]] == [5, 6, 7]
    assert seqs == [1, 3, 5, 7]
    read = subject.completed_turns()
    assert completed_seqs(read) == [7, 6, 5]
    assert (read.records[0].empty, read.records[1].empty, read.records[2].empty) == (
        False,
        True,
        False,
    )
    retained = sorted(
        path.name for path in session_dir(tmp_path).iterdir() if path.name.startswith("turn-")
    )
    assert retained == ["turn-5", "turn-7"]  # 出窗记录（含空回合挤掉的）目录一并淘汰


def test_seal_paths_register_each_turn_exactly_once(tmp_path: Path) -> None:
    import asyncio

    subject = make_store(tmp_path)
    seal_modified_turn(subject, tmp_path, request_id="req-1")

    records = index_payload(tmp_path)["records"]
    assert [record["turn_seq"] for record in records] == [1]  # 同步路径只登记一次

    subject._register_completed_turn(
        request_id="duplicate",
        turn_seq=1,
        created_at=7.0,
        dir_name=None,
        files=99,
        unknown=99,
        empty=True,
    )

    assert index_payload(tmp_path)["records"] == records  # 兜底重复登记被幂等拦住

    subject.begin_turn("req-2")
    assert asyncio.run(subject.seal_async()) is None  # 空回合走异步收尾
    records = index_payload(tmp_path)["records"]
    assert [record["turn_seq"] for record in records] == [1, 2]
    assert (records[1]["empty"], records[1]["dir"]) == (True, None)


def test_index_write_failure_is_unavailable_and_carried_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写失败未落定 → 查询"暂不可用"（不是空态、不是上一轮），不复用序号."""
    subject = make_store(tmp_path)
    seal_modified_turn(subject, tmp_path, request_id="req-1")

    original = store._write_json_atomic
    failures = {"left": 1}

    def flaky(path, payload, **kwargs):
        if Path(path).name == store.INDEX_NAME and failures["left"] > 0:
            failures["left"] -= 1
            raise OSError("simulated index write failure")
        return original(path, payload, **kwargs)

    monkeypatch.setattr(store, "_write_json_atomic", flaky)
    (tmp_path / "a.txt").write_bytes(b"second\n")
    subject.begin_turn("req-2")
    subject.note_capture("a.txt", b"first\n")
    manifest = subject.seal()

    assert manifest is not None
    assert manifest.turn_seq == 2  # 原回合的成功结果不受索引失败影响
    read = subject.completed_turns()
    assert read.ok is False
    assert read.records == []
    assert read.reason
    assert subject.manifest_for(1) is None  # 不得把上一轮冒充 @1
    assert subject.manifest_for(2) is None

    (tmp_path / "a.txt").write_bytes(b"third\n")
    subject.begin_turn("req-3")
    subject.note_capture("a.txt", b"second\n")
    third = subject.seal()

    assert third is not None
    assert third.turn_seq == 3  # 索引失败后不得复用已分配序号
    read = subject.completed_turns()
    assert read.ok is True
    assert completed_seqs(read) == [3, 2, 1]  # 失败记录随下一次成功写入一并落定
    assert subject.manifest_for(2) == manifest


def test_corrupt_index_is_unavailable_and_never_clobbered(tmp_path: Path) -> None:
    """读取损坏 → 不误报"无历史"，也不做破坏性重写."""
    subject = make_store(tmp_path)
    seal_modified_turn(subject, tmp_path, request_id="req-1")
    index_path = session_dir(tmp_path) / store.INDEX_NAME
    index_path.write_text("{not json", encoding="utf-8")

    read = subject.completed_turns()

    assert read.ok is False
    assert read.records == []
    assert read.reason

    (tmp_path / "a.txt").write_bytes(b"newer\n")
    subject.begin_turn("req-2")
    subject.note_capture("a.txt", b"older\n")
    assert subject.seal() is not None

    assert index_path.read_text(encoding="utf-8") == "{not json"
    assert subject.completed_turns().ok is False
    assert subject.manifest_for(1) is None


def test_byte_quota_eviction_keeps_the_record_but_marks_it_unavailable(
    tmp_path: Path,
) -> None:
    """目录被配额淘汰 ≠ 空回合：窗口内记录保留、available=false."""
    limits = store.TurnChangeLimits(
        max_file_bytes=1024,
        max_turn_bytes=4096,
        max_session_bytes=160,
        max_turns_retained=10,
    )
    subject = make_store(tmp_path, "sess-1", limits=limits)
    seal_modified_turn(subject, tmp_path, before=b"A" * 32, after=b"B" * 32, request_id="req-1")
    seal_modified_turn(subject, tmp_path, before=b"A" * 32, after=b"B" * 32, request_id="req-2")

    (tmp_path / "a.txt").write_bytes(b"C" * 32)
    subject.begin_turn("req-3")
    subject.note_capture("a.txt", b"D" * 64)  # 会话预算不足 → 淘汰最旧回合目录
    assert subject.seal() is not None

    read = subject.completed_turns()

    assert read.ok is True
    assert completed_seqs(read) == [3, 2, 1]
    oldest = read.records[-1]
    assert oldest.empty is False  # 不是"确定完成且净改动为空"
    assert oldest.available is False  # 快照已淘汰：与 empty 显式区分
    assert (oldest.files, oldest.unknown) == (1, 0)
    assert read.records[0].available is True
    assert subject.manifest_for(1) is None


def test_cancelled_seal_registers_without_evicting_beyond_the_stop_point(
    tmp_path: Path,
) -> None:
    """取消/短期预算：登记照做，淘汰按既有停止语义停下."""
    limits = store.TurnChangeLimits(compute_budget_ms=60_000, max_turns_retained=1)
    subject = make_store(tmp_path, "sess-1", limits=limits)
    for index in range(2):
        seal_modified_turn(subject, tmp_path, request_id=f"req-{index}")
    assert len(subject._turn_dirs()) == 1

    (tmp_path / "a.txt").write_bytes(b"cancelled\n")
    subject.begin_turn("req-cancelled")
    subject.note_capture("a.txt", b"before-cancel\n")

    assert subject.seal(cancelled=lambda: True) is not None

    assert len(subject._turn_dirs()) == 2  # 取消后不再继续淘汰旧目录
    read = subject.completed_turns()
    assert read.ok is True
    assert completed_seqs(read) == [3]


def test_queries_never_write_back_to_the_index(tmp_path: Path) -> None:
    subject = make_store(tmp_path)
    seal_modified_turn(subject, tmp_path, request_id="req-1")
    index_path = session_dir(tmp_path) / store.INDEX_NAME
    before = index_path.read_bytes()

    subject.completed_turns()
    subject.manifest_for(1)
    assert subject.completed_turns().ok is True

    assert index_path.read_bytes() == before
    assert sorted(path.name for path in session_dir(tmp_path).iterdir()) == [
        "owner.json",
        "turn-1",
        "turns.json",
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-handle semantics")
def test_index_read_refuses_a_replaced_session_directory(tmp_path: Path) -> None:
    """目录身份被替换 → 索引读取降级为不可用，不采信换入者（锚/所有权守卫）."""
    subject = make_store(tmp_path, "review")
    seal_modified_turn(subject, tmp_path, request_id="req-1")

    held = tmp_path / "review-held"
    subject.session_dir.rename(held)
    impostor = subject.session_dir
    (impostor / "turn-1").mkdir(parents=True)
    (impostor / store.INDEX_NAME).write_text(
        json.dumps(
            {
                "version": store.INDEX_VERSION,
                "records": [
                    {
                        "turn_seq": 9,
                        "request_id": "impostor",
                        "created_at": 0.0,
                        "dir": "turn-1",
                        "empty": False,
                        "files": 1,
                        "unknown": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    read = subject.completed_turns()

    assert read.ok is False
    assert read.records == []
    assert subject.manifest_for(9) is None


# ---------------------------------------------------------------------------
# per-entry identity reads (M3 · review R2) — "/changes <n>" addressing
# ---------------------------------------------------------------------------

def test_load_sides_for_reads_same_display_twins_by_entry_index(tmp_path: Path) -> None:
    """同 display、不同绝对路径的两条：按 entry_index 各读各的（display 不选条）."""
    workspace = tmp_path / "files"
    elsewhere = tmp_path / "elsewhere"
    workspace.mkdir()
    elsewhere.mkdir()
    (workspace / "a.txt").write_bytes(b"mine\n")
    (elsewhere / "a.txt").write_bytes(b"theirs\n")
    subject = make_store(workspace)
    subject.begin_turn("req-1")
    subject.note_capture(elsewhere / "a.txt", b"old-elsewhere\n", display="a.txt")
    subject.note_capture(workspace / "a.txt", b"old-workspace\n", display="a.txt")
    manifest = subject.seal()

    assert manifest is not None
    assert [change.display for change in manifest.files] == ["a.txt", "a.txt"]
    turn_seq = manifest.turn_seq
    first = subject.load_sides_for(turn_seq, 0)
    second = subject.load_sides_for(turn_seq, 1)

    assert (first.before, first.after) == (b"old-elsewhere\n", b"theirs\n")
    assert (second.before, second.after) == (b"old-workspace\n", b"mine\n")
    # 兼容口径仍是"首条匹配、命令不使用"（review R2 明示）
    assert subject.load_sides(0, "a.txt").before == b"old-elsewhere\n"


def test_load_sides_for_numbers_unknown_entries_after_files(tmp_path: Path) -> None:
    """合并序 = files 在前、unknown 在后，与展示编号一致（展示编号 = index + 1）."""
    subject = make_store(tmp_path)
    (tmp_path / "real.txt").write_bytes(b"new\n")
    (tmp_path / "ghost.py").write_bytes(b"ghost-content\n")
    subject.begin_turn("req-1")
    subject.note_capture("real.txt", b"old\n")
    subject.note_paths(["ghost.py"])  # 仅候选：未取得 before 快照 → unknown 区
    manifest = subject.seal()

    assert manifest is not None
    assert [change.path for change in manifest.files] == ["real.txt"]
    assert [change.path for change in manifest.unknown] == ["ghost.py"]
    turn_seq = manifest.turn_seq

    confirmed = subject.load_sides_for(turn_seq, 0)
    candidate = subject.load_sides_for(turn_seq, 1)

    assert (confirmed.before, confirmed.after) == (b"old\n", b"new\n")
    assert candidate.before_state == store.SIDE_UNCAPTURED
    assert candidate.after == b"ghost-content\n"
    assert subject.load_sides_for(turn_seq, 2).before_state == store.SIDE_UNCAPTURED


def test_load_sides_for_is_stable_after_the_on_disk_file_changes(tmp_path: Path) -> None:
    """回合结束后改盘上文件不影响回看：读的是持久化快照，不读实时目标."""
    subject = make_store(tmp_path)
    manifest = seal_modified_turn(subject, tmp_path, request_id="req-1")
    (tmp_path / "a.txt").write_bytes(b"edited long after the turn\n")

    sides = subject.load_sides_for(manifest.turn_seq, 0)

    assert (sides.before, sides.after) == (b"old\n", b"new\n")
    (tmp_path / "a.txt").unlink()
    assert subject.load_sides_for(manifest.turn_seq, 0).after == b"new\n"


def test_load_sides_for_does_not_invent_bytes_for_uncaptured_entries(tmp_path: Path) -> None:
    """配额导致 uncaptured 的条目：如实返回 uncaptured，不误读相邻条目/实时文件."""
    limits = store.TurnChangeLimits(max_file_bytes=16)
    (tmp_path / "big.txt").write_bytes(b"small\n")
    subject = make_store(tmp_path, "sess-1", limits=limits)
    subject.begin_turn("req-1")
    subject.note_capture("big.txt", b"BIG-SNAPSHOT-" * 8)
    manifest = subject.seal()

    assert manifest is not None
    assert manifest.files == []
    sides = subject.load_sides_for(manifest.turn_seq, 0)

    assert sides.before is None
    assert sides.before_state == store.SIDE_UNCAPTURED
    assert sides.after == b"small\n"


def test_load_sides_for_is_unavailable_when_the_index_is_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """索引不可用 → 不按 offset/display 猜条目，一律 uncaptured（review R1/R2）."""
    subject = make_store(tmp_path)
    manifest = seal_modified_turn(subject, tmp_path, request_id="req-1")
    index_path = session_dir(tmp_path) / store.INDEX_NAME
    index_path.write_text("{not json", encoding="utf-8")

    broken = subject.load_sides_for(manifest.turn_seq, 0)

    assert broken == store.LoadedSides(
        None, None, store.SIDE_UNCAPTURED, store.SIDE_UNCAPTURED
    )
    assert subject.load_sides_for(-1, 0).before is None
    assert subject.load_sides_for(manifest.turn_seq, -1).before is None

    index_path.write_text(
        json.dumps({"version": store.INDEX_VERSION, "records": []}), encoding="utf-8"
    )
    assert subject.load_sides_for(manifest.turn_seq, 0).before is None


def test_load_sides_for_rejects_a_directory_whose_manifest_is_another_turn(
    tmp_path: Path,
) -> None:
    """索引指向的目录内容若属于别的回合（身份不符）→ 不采信、不返回其字节."""
    subject = make_store(tmp_path)
    manifest = seal_modified_turn(subject, tmp_path, request_id="req-1")
    (tmp_path / "a.txt").write_bytes(b"second\n")
    subject.begin_turn("req-2")
    subject.note_capture("a.txt", b"first\n")
    assert subject.seal() is not None
    area = session_dir(tmp_path)
    # 目录自称属于别的回合（换入/串号）：manifest 自报 turn_seq 与索引不一致
    decoy = json.loads((area / "turn-2" / "manifest.json").read_text(encoding="utf-8"))
    decoy["turn_seq"] = 1
    (area / "turn-2" / "manifest.json").write_text(json.dumps(decoy), encoding="utf-8")

    sides = subject.load_sides_for(2, 0)
    wrong = subject.load_sides_for(manifest.turn_seq, 0)

    assert sides.before_state == store.SIDE_UNCAPTURED
    assert sides.before is None and sides.after is None
    assert subject.manifest_for(2) is None  # 身份不符：不冒充该回合的清单
    assert wrong.after == b"new\n"  # 原回合（turn-1）仍可正常回看


def test_load_sides_for_is_guarded_by_directory_identity(tmp_path: Path) -> None:
    """目录身份被替换 → 条目读取降级 uncaptured，不采信换入者内容（锚/所有权守卫）."""
    subject = make_store(tmp_path, "review")
    manifest = seal_modified_turn(subject, tmp_path, request_id="req-1")
    held = tmp_path / "review-held"
    subject.session_dir.rename(held)
    impostor = subject.session_dir
    (impostor / "turn-1").mkdir(parents=True)
    (impostor / "turn-1" / "before.0.bin").write_bytes(b"IMPOSTOR-BEFORE\n")
    (impostor / "turn-1" / "after.0.bin").write_bytes(b"IMPOSTOR-AFTER\n")
    (impostor / "turn-1" / "manifest.json").write_text(
        json.dumps(
            {
                "session_id": "review",
                "request_id": "impostor",
                "turn_seq": 1,
                "created_at": 0.0,
                "files": [
                    {
                        "path": "a.txt",
                        "display": "a.txt",
                        "state": "modified",
                        "before_state": "captured",
                        "after_state": "captured",
                        "before_file": "before.0.bin",
                        "after_file": "after.0.bin",
                    }
                ],
                "unknown": [],
                "totals": {"files": 1, "added": 1, "removed": 1},
            }
        ),
        encoding="utf-8",
    )
    (impostor / store.INDEX_NAME).write_text(
        json.dumps(
            {
                "version": store.INDEX_VERSION,
                "records": [
                    {
                        "turn_seq": 1,
                        "request_id": "impostor",
                        "created_at": 0.0,
                        "dir": "turn-1",
                        "empty": False,
                        "files": 1,
                        "unknown": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    sides = subject.load_sides_for(manifest.turn_seq, 0)

    assert sides.before is None and sides.after is None
    assert sides.before_state == store.SIDE_UNCAPTURED
