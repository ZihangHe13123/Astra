"""File tools must not block the backend's event loop.

Each test holds one real filesystem call for one path (a "gate" inside
``os.scandir`` or ``Path.open``) and observes the loop from a concurrent ticker
task while the tool runs through the real registry.
"""

import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.registry import ToolRegistry


GATE_WAIT_SECONDS = 5.0

SEARCHES = [
    ("search_files", {"pattern": "**/*.txt"}),
    ("search_text", {"pattern": "needle"}),
]
EXPECTED_OUTPUT = {
    "search_files": os.path.join("dir07", "needle.txt"),
    "search_text": "1 match(es):\n" + os.path.join("dir07", "needle.txt") + ":1: needle",
}


class PathGate:
    """Hold the first call of ``owner.attribute`` for one path until released."""

    def __init__(self, monkeypatch, owner, attribute, target):
        self.target = os.path.normcase(os.path.abspath(target))
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls_after_release: list[str] = []
        original = getattr(owner, attribute)

        def gated(path=".", *args, **kwargs):
            normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
            if self.release.is_set():
                self.calls_after_release.append(normalized)
            elif normalized == self.target and not self.entered.is_set():
                self.entered.set()
                # A loop-blocking implementation stalls here on the loop thread.
                self.release.wait(GATE_WAIT_SECONDS)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(owner, attribute, gated)

    async def wait_entered(self):
        deadline = time.monotonic() + GATE_WAIT_SECONDS
        while not self.entered.is_set():
            assert time.monotonic() < deadline, "the tool never reached the gated path"
            await asyncio.sleep(0.01)


class _Listing:
    """A ``scandir`` iterator over entries already read, in a chosen order."""

    def __init__(self, entries):
        self._entries = iter(entries)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def list_entries_in_reverse_name_order(monkeypatch):
    """List directories in reverse name order, as ext4 and others may.

    NTFS returns entries sorted by name, so without this a walk that depends
    on the filesystem's order would only fail on other platforms.
    """
    original = os.scandir

    def reversed_scandir(path=".", *args, **kwargs):
        with original(path, *args, **kwargs) as entries:
            return _Listing(sorted(entries, key=lambda entry: entry.name, reverse=True))

    monkeypatch.setattr(os, "scandir", reversed_scandir)


def build_workspace(tmp_path, monkeypatch, needles=("dir07",)):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "astra-home"))
    monkeypatch.delenv("ASTRA_SEARCH_TIME_BUDGET_SECONDS", raising=False)
    workspace = tmp_path / "workspace"
    for index in range(20):
        (workspace / f"dir{index:02d}").mkdir(parents=True)
        (workspace / f"dir{index:02d}" / "other.md").write_text("unrelated\n", encoding="utf-8")
    for directory in needles:
        (workspace / directory / "needle.txt").write_text("needle\n", encoding="utf-8", newline="\n")
    registry = ToolRegistry()
    register_file_tools(registry, str(workspace))
    return workspace, registry


async def tick(ticks: list[float], stop: asyncio.Event) -> None:
    while not stop.is_set():
        ticks.append(time.perf_counter())
        await asyncio.sleep(0.01)


@pytest.mark.parametrize("tool,args", SEARCHES)
def test_search_keeps_the_event_loop_responsive(tmp_path, monkeypatch, tool, args):
    workspace, registry = build_workspace(tmp_path, monkeypatch)
    gate = PathGate(monkeypatch, os, "scandir", workspace / "dir07")

    async def scenario():
        ticks: list[float] = []
        stop = asyncio.Event()
        ticker = asyncio.create_task(tick(ticks, stop))
        search = asyncio.create_task(registry.execute(tool, args))
        try:
            await gate.wait_entered()
            before = len(ticks)
            await asyncio.sleep(0.3)
            ticked_while_blocked = len(ticks) - before
            still_running = not search.done()
        finally:
            gate.release.set()
        result = await asyncio.wait_for(search, GATE_WAIT_SECONDS)
        stop.set()
        await ticker
        return ticked_while_blocked, still_running, result

    ticked_while_blocked, still_running, result = asyncio.run(scenario())
    # The walk is parked in its worker, while the loop keeps scheduling tasks.
    assert still_running
    assert ticked_while_blocked >= 5
    assert result["error"] == ""
    assert result["output"] == EXPECTED_OUTPUT[tool]


@pytest.mark.parametrize("tool,args", SEARCHES)
def test_cancelled_search_returns_at_once_and_stops_walking(tmp_path, monkeypatch, tool, args):
    workspace, registry = build_workspace(tmp_path, monkeypatch)
    gate = PathGate(monkeypatch, os, "scandir", workspace)

    async def scenario():
        search = asyncio.create_task(registry.execute(tool, args))
        try:
            await gate.wait_entered()
            search.cancel()
            started = time.perf_counter()
            with pytest.raises(asyncio.CancelledError):
                await search
            cancel_seconds = time.perf_counter() - started
        finally:
            gate.release.set()
        # The worker resumes from the held listing; give it time to walk on.
        await asyncio.sleep(0.3)
        return cancel_seconds

    cancel_seconds = asyncio.run(scenario())
    assert cancel_seconds < 0.5
    # Twenty unvisited directories remained; a cancelled walk lists none of them.
    assert gate.calls_after_release == []


@pytest.mark.parametrize("tool,args", SEARCHES)
def test_search_time_limit_interrupts_a_long_walk(tmp_path, monkeypatch, tool, args):
    workspace, registry = build_workspace(tmp_path, monkeypatch)
    tool_def = registry.get(tool)
    assert tool_def is not None
    tool_def.timeout = 0.3
    # One listing outlasts both the search's own budget and the tool limit,
    # so the worker never gets to return its matches itself.
    gate = PathGate(monkeypatch, os, "scandir", workspace / "dir07")

    async def scenario():
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(registry.execute(tool, args), GATE_WAIT_SECONDS)
        finally:
            gate.release.set()
        elapsed = time.perf_counter() - started
        await asyncio.sleep(0.3)
        return result, elapsed

    result, elapsed = asyncio.run(scenario())
    assert elapsed < 2.0
    assert result["code"] == "overall_timeout"
    assert "narrower directory" in result["recovery_hint"]
    assert result["details"]["directories_listed"] >= 1
    assert gate.calls_after_release == []


INCOMPLETE_NOTE = "Results are incomplete: the search stopped at its time limit after "


def split_partial_output(tool: str, output: str) -> tuple[list[str], str]:
    """Separate a budget-stopped search's matches from its incompleteness note."""
    lines = output.splitlines()
    if tool == "search_files":
        return lines[:-1], lines[-1]
    header, *matches = lines
    count, note = header.split(" so far. ", 1)
    assert count == f"{len(matches)} match(es)"
    return matches, note


@pytest.mark.parametrize("listing", ["filesystem order", "reverse name order"])
@pytest.mark.parametrize("tool,args", SEARCHES)
def test_search_out_of_budget_returns_the_matches_found_so_far(
    tmp_path, monkeypatch, tool, args, listing
):
    workspace, registry = build_workspace(tmp_path, monkeypatch, needles=("dir03", "dir12"))
    monkeypatch.setenv("ASTRA_SEARCH_TIME_BUDGET_SECONDS", "0.2")
    if listing == "reverse name order":
        # The part searched before the budget runs out is the same on every
        # filesystem: the walk goes in name order, not the listing's order.
        list_entries_in_reverse_name_order(monkeypatch)
    gate = PathGate(monkeypatch, os, "scandir", workspace / "dir07")

    async def scenario():
        search = asyncio.create_task(registry.execute(tool, args))
        try:
            await gate.wait_entered()
            # Hold the walk past its budget, far inside the tool's own limit.
            await asyncio.sleep(0.4)
        finally:
            gate.release.set()
        return await asyncio.wait_for(search, GATE_WAIT_SECONDS)

    result = asyncio.run(scenario())
    assert result["error"] == ""
    assert result["partial"] is True
    matches, note = split_partial_output(tool, result["output"])
    found = os.path.join("dir03", "needle.txt")
    assert matches == [found if tool == "search_files" else f"{found}:1: needle"]
    assert note.startswith(INCOMPLETE_NOTE)
    assert "narrower directory" in note
    # The walk stopped where the budget ran out: dir12 was never listed.
    assert gate.calls_after_release == []


@pytest.mark.parametrize("tool,args", SEARCHES)
def test_search_out_of_budget_without_matches_says_the_rest_was_not_searched(
    tmp_path, monkeypatch, tool, args
):
    workspace, registry = build_workspace(tmp_path, monkeypatch, needles=("dir12",))
    monkeypatch.setenv("ASTRA_SEARCH_TIME_BUDGET_SECONDS", "0.2")
    gate = PathGate(monkeypatch, os, "scandir", workspace / "dir07")

    async def scenario():
        search = asyncio.create_task(registry.execute(tool, args))
        try:
            await gate.wait_entered()
            await asyncio.sleep(0.4)
        finally:
            gate.release.set()
        return await asyncio.wait_for(search, GATE_WAIT_SECONDS)

    result = asyncio.run(scenario())
    assert result["error"] == ""
    assert result["partial"] is True
    first, note = result["output"].splitlines()
    assert first.startswith("No files matching" if tool == "search_files" else "No lines matching")
    assert first.endswith("searched so far.")
    assert note.startswith(INCOMPLETE_NOTE)
    # Neither the rest of the walk nor a near-miss probe listed anything more.
    assert gate.calls_after_release == []


@pytest.mark.parametrize("tool,args", SEARCHES)
def test_search_budget_ends_before_the_tool_time_limit(tmp_path, monkeypatch, tool, args):
    workspace, registry = build_workspace(tmp_path, monkeypatch, needles=("dir03",))
    for index in range(300):
        (workspace / "more" / f"d{index:03d}").mkdir(parents=True)
    tool_def = registry.get(tool)
    assert tool_def is not None
    tool_def.timeout = 2.0
    original_scandir = os.scandir

    def slow_scandir(path=".", *args, **kwargs):
        # Every listing is slow, so the whole walk takes longer than the limit.
        time.sleep(0.01)
        return original_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", slow_scandir)

    async def scenario():
        started = time.perf_counter()
        result = await asyncio.wait_for(registry.execute(tool, args), GATE_WAIT_SECONDS)
        return result, time.perf_counter() - started

    result, elapsed = asyncio.run(scenario())
    assert elapsed < 2.0
    assert result["error"] == ""
    assert result["partial"] is True
    matches, note = split_partial_output(tool, result["output"])
    found = os.path.join("dir03", "needle.txt")
    assert matches == [found if tool == "search_files" else f"{found}:1: needle"]
    assert note.startswith(INCOMPLETE_NOTE)


@pytest.mark.parametrize("tool", ["read_file", "stat_file"])
def test_file_reads_keep_the_event_loop_responsive(tmp_path, monkeypatch, tool):
    workspace, registry = build_workspace(tmp_path, monkeypatch)
    gate = PathGate(monkeypatch, Path, "open", workspace / "dir07" / "needle.txt")

    async def scenario():
        ticks: list[float] = []
        stop = asyncio.Event()
        ticker = asyncio.create_task(tick(ticks, stop))
        read = asyncio.create_task(registry.execute(tool, {"path": os.path.join("dir07", "needle.txt")}))
        try:
            await gate.wait_entered()
            before = len(ticks)
            await asyncio.sleep(0.3)
            ticked_while_blocked = len(ticks) - before
            still_running = not read.done()
        finally:
            gate.release.set()
        result = await asyncio.wait_for(read, GATE_WAIT_SECONDS)
        stop.set()
        await ticker
        return ticked_while_blocked, still_running, result

    ticked_while_blocked, still_running, result = asyncio.run(scenario())
    assert still_running
    assert ticked_while_blocked >= 5
    assert result["error"] == ""
    if tool == "read_file":
        assert result["output"] == "needle\n"
    else:
        assert json.loads(result["output"])["total_lines"] == 1
