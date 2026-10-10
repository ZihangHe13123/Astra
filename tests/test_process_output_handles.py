import asyncio
import json
import shlex
import sys

import pytest

from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.registry import ToolRegistry
from agent.sandbox.local import LocalSandbox


@pytest.mark.skipif(sys.platform == "win32", reason="native Bash integration")
@pytest.mark.parametrize("foreground_yield_ms", [0, 10000])
def test_truncated_foreground_output_has_usable_process_reader(tmp_path, foreground_yield_ms):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path / "artifacts")
        register_code_tools(registry, LocalSandbox(workdir=str(tmp_path), max_output_bytes=64))
        command = shlex.join([sys.executable, "-c", "print('begin-' + 'x' * 200 + '-end')"])
        result = await registry.execute("execute_shell", {"command": command, "foreground_yield_ms": foreground_yield_ms})
        assert not result["error"], result
        marker = "[Read full output: "
        handle = json.loads(result["output"].split(marker)[1].split("]")[0])
        assert handle["tool"] == "process_read"
        readback = await registry.execute(handle["tool"], handle["arguments"])
        assert not readback["error"], readback
        assert "begin-" + "x" * 200 + "-end" in json.loads(readback["output"])["content"]
    asyncio.run(scenario())


# No newlines: a Windows child would write them as two bytes.
_LINES = "".join(f"line {index:04d};" for index in range(300))
_PRINT_LINES = "import sys\nfor index in range(300):\n    sys.stdout.write(f'line {index:04d};')\n"


async def _finished_background_process(registry) -> tuple[str, dict]:
    started = await registry.execute("execute_python", {"code": _PRINT_LINES, "background": True})
    assert not started["error"], started
    process_id = json.loads(started["output"])["process_id"]
    polled = await registry.execute("process_poll", {"process_id": process_id, "wait_ms": 20_000})
    description = json.loads(polled["output"])
    assert description["status"] == "completed", description
    return process_id, description


def test_suggested_reader_arguments_page_through_the_output_once(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path / "artifacts")
        sandbox = LocalSandbox(workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        try:
            _process_id, description = await _finished_background_process(registry)
            reader = description["output_reader"]
            pages = []
            for _ in range(8):
                assert reader["tool"] == "process_read"
                read = await registry.execute(reader["tool"], {**reader["arguments"], "max_chars": 1000})
                assert not read["error"], read
                page = json.loads(read["output"])
                pages.append(page["content"])
                if page["eof"]:
                    break
                # Reusing the arguments each result suggests must move forward.
                reader = page["output_reader"]
            assert pages == [_LINES[:1000], _LINES[1000:2000], _LINES[2000:]]
        finally:
            await sandbox.close()
    asyncio.run(scenario())


def test_explicit_byte_read_keeps_the_cursor_and_reports_no_character_positions(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path / "artifacts")
        sandbox = LocalSandbox(workdir=str(tmp_path))
        register_code_tools(registry, sandbox)

        async def read(**arguments):
            result = await registry.execute("process_read", arguments)
            assert not result["error"], result
            return json.loads(result["output"])

        try:
            process_id, _description = await _finished_background_process(registry)
            first = await read(process_id=process_id, max_chars=1000)
            assert first["content"] == _LINES[:1000]

            tail = await read(process_id=process_id, byte_offset=2900, max_chars=1000)
            assert tail["content"] == _LINES[2900:]
            assert tail["eof"] is True
            assert tail["next_byte_offset"] == tail["total_bytes"] == len(_LINES)
            # A byte position does not say how many characters precede it.
            assert not {"offset", "next_offset", "total_chars"} & set(tail)

            # Looking at the tail did not lose the place of the cursor read.
            second = await read(process_id=process_id, max_chars=1000)
            assert second["content"] == _LINES[1000:2000]
            assert second["offset"] == 1000

            # An explicit page suggests the byte cursor where it stopped.
            page = await read(process_id=process_id, byte_offset=0, max_chars=1500)
            following = await read(**page["output_reader"]["arguments"])
            assert page["content"] + following["content"] == _LINES
            assert following["eof"] is True
        finally:
            await sandbox.close()
    asyncio.run(scenario())


def test_process_read_past_the_end_says_so_and_reports_the_real_end(tmp_path):
    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path / "artifacts")
        sandbox = LocalSandbox(workdir=str(tmp_path))
        register_code_tools(registry, sandbox)
        try:
            process_id, _description = await _finished_background_process(registry)
            result = await registry.execute(
                "process_read", {"process_id": process_id, "byte_offset": len(_LINES) + 1000}
            )
            assert not result["error"], result
            beyond = json.loads(result["output"])
            assert beyond["content"] == ""
            assert beyond["eof"] is True
            # The requested position is not echoed as if output existed there.
            assert beyond["byte_offset"] == beyond["next_byte_offset"] == beyond["total_bytes"] == len(_LINES)
            assert str(len(_LINES) + 1000) in beyond["offset_past_end"]
            assert beyond["output_reader"]["arguments"]["byte_offset"] == len(_LINES)
        finally:
            await sandbox.close()
    asyncio.run(scenario())
