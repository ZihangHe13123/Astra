import asyncio
import json
import os
import subprocess
import time

import pytest

from agent.runtime.tools.processes import ManagedProcess, ProcessManager


def _managed(tmp_path, text: str, *, external: bool = False) -> tuple[ProcessManager, ManagedProcess]:
    root = tmp_path / "processes"
    manager = ProcessManager(artifact_dir=root)
    process_id = "stream-test"
    manifest, output, stdout, stderr = manager._paths(process_id)
    output.write_text(text, encoding="utf-8")
    stdout.write_text(text, encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    process = ManagedProcess(
        process_id=process_id,
        kind="shell",
        label="stream test",
        task=None,
        started_at=time.time(),
        manifest_path=manifest,
        output_path=output,
        stdout_path=stdout,
        stderr_path=stderr,
        recovered_status="completed",
        output_chars=len(text),
        stdout_chars=len(text),
        external=external,
    )
    manager._processes[process_id] = process
    return manager, process


def test_process_read_pages_utf8_by_byte_cursor(tmp_path):
    manager, process = _managed(tmp_path, "ab你好cd")

    first = manager.read(process, offset=None, max_chars=3)
    second = manager.read(
        process,
        offset=None,
        byte_offset=first["next_byte_offset"],
        max_chars=3,
    )

    assert first["content"] == "ab你"
    assert first["next_byte_offset"] == len("ab你".encode("utf-8"))
    assert second["content"] == "好cd"
    assert second["eof"] is True
    assert second["total_bytes"] == len("ab你好cd".encode("utf-8"))


def test_process_read_default_cursor_is_independent_per_stream(tmp_path):
    manager, process = _managed(tmp_path, "abcdef")
    process.stderr_path.write_text("错误详情", encoding="utf-8")

    stdout = manager.read(process, offset=None, max_chars=2, stream="stdout")
    stderr = manager.read(process, offset=None, max_chars=2, stream="stderr")
    stdout_next = manager.read(process, offset=None, max_chars=2, stream="stdout")

    assert stdout["content"] == "ab"
    assert stderr["content"] == "错误"
    assert stdout_next["content"] == "cd"


def test_process_read_legacy_character_offset_scans_without_materializing(tmp_path):
    manager, process = _managed(tmp_path, "a你好bc")

    result = manager.read(process, offset=2, max_chars=2)

    assert result["content"] == "好b"
    assert result["byte_offset"] == len("a你".encode("utf-8"))
    assert result["next_offset"] == 4


def test_explicit_byte_read_leaves_cursor_and_character_fields_alone(tmp_path):
    manager, process = _managed(tmp_path, "ab你好cd")

    first = manager.read(process, offset=None, max_chars=3)
    peek = manager.read(
        process,
        offset=None,
        byte_offset=len("ab你好".encode("utf-8")),
        max_chars=10,
    )
    second = manager.read(process, offset=None, max_chars=10)

    assert first["content"] == "ab你"
    assert peek["content"] == "cd"
    assert peek["eof"] is True
    # Counting the characters before a byte position would mean scanning the log.
    assert not {"offset", "next_offset", "total_chars"} & set(peek)
    assert peek["output_reader"]["arguments"]["byte_offset"] == peek["next_byte_offset"]
    assert second["content"] == "好cd"
    assert second["offset"] == 3
    assert second["total_chars"] == len("ab你好cd")


def test_reader_arguments_continue_after_each_kind_of_read(tmp_path):
    manager, process = _managed(tmp_path, "a你好bcdef")

    def reread(result):
        arguments = dict(result["output_reader"]["arguments"])
        assert arguments.pop("process_id") == process.process_id
        arguments["max_chars"] = 2
        return manager.read(process, offset=arguments.pop("offset", None), **arguments)

    # Before any read the suggestion starts at the beginning.
    assert "offset" not in manager.describe(process)["output_reader"]["arguments"]
    cursor_first = manager.read(process, offset=None, max_chars=2)
    cursor_second = reread(cursor_first)
    explicit = manager.read(process, offset=1, max_chars=2)
    explicit_next = reread(explicit)

    assert (cursor_first["content"], cursor_second["content"]) == ("a你", "好b")
    assert (explicit["content"], explicit_next["content"]) == ("你好", "bc")
    # The explicit reads did not move the cursor.
    assert manager.read(process, offset=None, max_chars=2)["content"] == "cd"


def test_result_only_output_pages_by_byte_cursor(tmp_path):
    manager, process = _managed(tmp_path, "")
    for path in (process.output_path, process.stdout_path, process.stderr_path):
        path.unlink()
    process.result = {"output": "你好cd", "error": "", "exit_code": 0}

    first = manager.read(process, offset=None, byte_offset=0, max_chars=2)
    arguments = dict(first["output_reader"]["arguments"])
    arguments.pop("process_id")
    second = manager.read(process, offset=None, **arguments)

    assert first["content"] == "你好"
    assert first["next_byte_offset"] == len("你好".encode("utf-8"))
    assert first["eof"] is False
    assert second["content"] == "cd"
    assert second["eof"] is True


def test_byte_offset_past_the_end_is_reported_from_the_real_end(tmp_path):
    text = "ab你好cd"
    total = len(text.encode("utf-8"))
    manager, process = _managed(tmp_path, text)

    result = manager.read(process, offset=None, byte_offset=total + 500, max_chars=10)

    assert result["content"] == ""
    assert result["byte_offset"] == result["next_byte_offset"] == result["total_bytes"] == total
    assert result["eof"] is True
    assert str(total + 500) in result["offset_past_end"] and str(total) in result["offset_past_end"]
    assert result["output_reader"]["arguments"]["byte_offset"] == total
    # A position that exists, including the end itself, carries no such note.
    for valid in (0, total):
        assert "offset_past_end" not in manager.read(process, offset=None, byte_offset=valid, max_chars=10)


def test_byte_offset_past_the_end_of_a_running_process_does_not_become_the_next_position(tmp_path):
    manager, process = _managed(tmp_path, "")
    for path in (process.output_path, process.stdout_path, process.stderr_path):
        path.unlink()
    process.recovered_status = "running"

    early = manager.read(process, offset=None, byte_offset=500, max_chars=10)

    assert early["eof"] is False
    assert early["byte_offset"] == early["next_byte_offset"] == early["total_bytes"] == 0
    assert "so far" in early["offset_past_end"]
    # Output that arrives later is read from its start, not from byte 500.
    process.output_path.write_text("late output", encoding="utf-8")
    arguments = dict(early["output_reader"]["arguments"])
    assert arguments.pop("process_id") == process.process_id
    assert manager.read(process, offset=None, **arguments)["content"] == "late output"


def test_character_offset_past_the_end_does_not_inflate_the_total(tmp_path):
    text = "a你好bc"
    manager, process = _managed(tmp_path, text)

    result = manager.read(process, offset=500, max_chars=10)

    assert result["content"] == ""
    assert result["offset"] == result["next_offset"] == result["total_chars"] == len(text)
    assert result["byte_offset"] == result["total_bytes"] == len(text.encode("utf-8"))
    assert "500" in result["offset_past_end"] and str(len(text)) in result["offset_past_end"]
    assert result["output_reader"]["arguments"]["offset"] == len(text)
    assert "offset_past_end" not in manager.read(process, offset=len(text), max_chars=10)


def test_result_only_output_reports_positions_past_the_end_from_the_real_end(tmp_path):
    manager, process = _managed(tmp_path, "")
    for path in (process.output_path, process.stdout_path, process.stderr_path):
        path.unlink()
    process.result = {"output": "你好cd", "error": "", "exit_code": 0}
    total = len("你好cd".encode("utf-8"))

    by_byte = manager.read(process, offset=None, byte_offset=total + 9, max_chars=10)
    by_char = manager.read(process, offset=40, max_chars=10)

    assert by_byte["byte_offset"] == by_byte["next_byte_offset"] == by_byte["total_bytes"] == total
    assert by_char["offset"] == by_char["next_offset"] == by_char["total_chars"] == 4
    for result in (by_byte, by_char):
        assert result["content"] == "" and result["eof"] is True
        assert "past the end" in result["offset_past_end"]


def test_external_refresh_uses_manifest_counters_without_scanning_logs(tmp_path, monkeypatch):
    manager, process = _managed(tmp_path, "large output", external=True)
    process.manifest_path.write_text(
        json.dumps({
            "status": "completed",
            "output_chars": 120_000,
            "stdout_chars": 119_000,
            "stderr_chars": 1_000,
            "completed_at": time.time(),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        manager,
        "_char_count",
        lambda _path: (_ for _ in ()).throw(AssertionError("must not scan output")),
    )

    manager._refresh_external(process)

    assert process.output_chars == 120_000
    assert process.stdout_chars == 119_000
    assert process.stderr_chars == 1_000


def test_process_prune_reserves_slot_and_deletes_old_artifacts(tmp_path):
    root = tmp_path / "processes"
    manager = ProcessManager(artifact_dir=root, max_processes=2)
    for index in range(2):
        process_id = f"old-{index}"
        manifest, output, stdout, stderr = manager._paths(process_id)
        for path in (manifest, output, stdout, stderr):
            path.write_text("old", encoding="utf-8")
        manager._processes[process_id] = ManagedProcess(
            process_id=process_id,
            kind="shell",
            label="old",
            task=None,
            started_at=float(index),
            completed_at=float(index),
            manifest_path=manifest,
            output_path=output,
            stdout_path=stdout,
            stderr_path=stderr,
            recovered_status="completed",
            visible=True,
        )

    oldest = manager._processes["old-0"]
    manager._prune(reserve=1)

    assert list(manager._processes) == ["old-1"]
    assert not oldest.manifest_path.exists()
    assert not oldest.output_path.exists()


def test_supervised_startup_poll_yields_to_event_loop(tmp_path, monkeypatch):
    manager = ProcessManager(artifact_dir=tmp_path / "processes")
    popen_kwargs = {}

    class Child:
        pid = 12345

        @staticmethod
        def poll():
            return None

    def fake_popen(*_args, **kwargs):
        popen_kwargs.update(kwargs)
        return Child()

    monkeypatch.setattr("agent.runtime.tools.processes.subprocess.Popen", fake_popen)
    original_sleep = asyncio.sleep
    yielded = False

    async def publish_after_yield(_delay):
        nonlocal yielded
        yielded = True
        manifest = next(
            path for path in manager.artifact_dir.glob("*.json")
            if not path.name.endswith(".spec.json")
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload.update({"status": "completed", "supervisor_pid": Child.pid, "completed_at": time.time()})
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        await original_sleep(0)

    monkeypatch.setattr("agent.runtime.tools.processes.asyncio.sleep", publish_after_yield)

    process = asyncio.run(manager.start_supervised(
        {"type": "shell", "command": "demo"},
        kind="shell",
        label="demo",
    ))

    assert yielded is True
    assert process.recovered_status == "completed"
    if os.name == "nt":
        flags = popen_kwargs["creationflags"]
        assert flags & subprocess.CREATE_NO_WINDOW
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
        assert not flags & subprocess.DETACHED_PROCESS
    else:
        assert popen_kwargs["start_new_session"] is True


def test_cancelling_a_supervised_start_asks_the_owner_to_stop_and_keeps_it_listed(tmp_path, monkeypatch):
    """The supervisor is already running when the start is cancelled, and nobody holds its process_id yet."""
    manager = ProcessManager(artifact_dir=tmp_path / "processes")

    class NeverPublishes:
        pid = 12345

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(
        "agent.runtime.tools.processes.subprocess.Popen", lambda *_args, **_kwargs: NeverPublishes()
    )
    # The stand-in's pid is not a real process: never probe it.
    monkeypatch.setattr(manager, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        "agent.runtime.tools.processes._ABANDONED_STOP_WAIT_SECONDS", 0.05, raising=False,
    )

    async def scenario():
        start = asyncio.create_task(manager.start_supervised(
            {"sandbox": {"type": "local"}, "command": "demo"}, kind="shell", label="demo",
        ))
        await asyncio.sleep(0.05)
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start

    asyncio.run(scenario())
    assert [item["status"] for item in manager.list()] == ["running"]
    assert len(list(manager.artifact_dir.glob("*.cancel"))) == 1


def test_compacted_read_stamp_replays_same_utf8_page_after_cursor_advanced(tmp_path):
    from agent.runtime.micro_compact import micro_compact_tool_results

    manager, process = _managed(tmp_path, "前缀" + "中" * 4000 + "第二页" * 2000)
    process.result = {"exit_code": 0}
    manager.read(process, offset=None, max_chars=2, stream="stdout")
    original = manager.read(process, offset=None, max_chars=4000, stream="stdout")
    manager.read(process, offset=None, max_chars=1000, stream="stdout")
    messages = [
        {"role": "user", "content": "old"},
        {"role": "tool", "name": "process_read", "content": json.dumps(original)},
        {"role": "tool", "name": "read_file", "content": "x" * 2000},
        {"role": "user", "content": "continue"},
    ]
    compacted, stats = micro_compact_tool_results(messages, char_budget=1000, keep_recent=1)
    stamp = json.loads(compacted[1]["content"].split("\n", 1)[1])
    args = stamp["reread"]["arguments"].copy()
    assert args.pop("process_id") == process.process_id
    replay = manager.read(process, offset=None, **args)
    assert replay["content"] == original["content"]
    assert replay["byte_offset"] == original["byte_offset"]
    assert replay["next_byte_offset"] == original["next_byte_offset"]
    again, _ = micro_compact_tool_results(compacted, char_budget=1000, keep_recent=1)
    assert again[1] == compacted[1]
    assert stats.saved_chars > 0
