import ast
import asyncio
import base64
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

import agent.runtime.tools.image as image_module
from agent.core.msg import ContentBlock, Msg
from agent.runtime.hooks import HookRegistry
from agent.runtime.react import ReActAgent
from agent.runtime.tools.image import (
    _inspect_image_metadata,
    _materialize_wsl_image,
    _resolve_image_path,
    register_image_tools,
)
from agent.runtime.tools.registry import ToolRegistry


def run(coro):
    return asyncio.run(coro)


def _write_png(path):
    path.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    ))


def _png_chunk(chunk_type, data):
    return (
        struct.pack(">I", len(data))
        + chunk_type
        + data
        + struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
    )


def _write_comfy_png(path, workflow):
    ihdr = struct.pack(">IIBBBBB", 1024, 1344, 8, 2, 0, 0, 0)
    prompt = json.dumps(workflow).encode("latin-1")
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"tEXt", b"prompt\x00" + prompt)
        + _png_chunk(b"IEND", b"")
    )


def test_read_image_tool_returns_attachable_image_payload(tmp_path):
    image = tmp_path / "sample.png"
    _write_png(image)
    registry = ToolRegistry()
    register_image_tools(registry, workdir=str(tmp_path))

    async def scenario():
        result = await registry.execute("read_image", {
            "path": "sample.png",
            "question": "describe visible content",
            "detail": "original",
        })
        payload = json.loads(result["output"])

        assert result["error"] == ""
        assert payload["type"] == "image_attachment"
        assert payload["image_paths"] == [str(image.resolve())]
        assert payload["question"] == "describe visible content"
        assert payload["detail"] == "original"

    run(scenario())


def test_read_image_tiles_uses_injected_bounded_selector(tmp_path):
    calls = []
    tile = tmp_path / "r1c1.png"
    _write_png(tile)
    data_url = "data:image/png;base64," + base64.b64encode(tile.read_bytes()).decode("ascii")

    def select(tile_set_id, tile_ids):
        calls.append((tile_set_id, tile_ids))
        return {
            "data_urls": [data_url],
            "labels": ["image1-r1c1 [0,768) x [0,768)"],
            "unserved_ids": [],
            "remaining_images": 10,
            "remaining_inline_bytes": 1_000_000,
        }

    registry = ToolRegistry()
    register_image_tools(registry, workdir=str(tmp_path), select_tiles=select)
    result = run(registry.execute("read_image_tiles", {
        "tile_set_id": "opaque",
        "tile_ids": ["image1-r1c1"],
    }))
    payload = json.loads(result["output"])
    assert result["error"] == ""
    assert calls == [("opaque", ["image1-r1c1"])]
    assert payload["type"] == "image_attachment"
    assert payload["image_labels"] == ["image1-r1c1 [0,768) x [0,768)"]
    assert "data:image" not in result["output"]
    assert result["_private_result"] == {"image_data_urls": [data_url]}


def test_read_image_tiles_verified_bytes_bypass_public_tool_hooks(tmp_path):
    tile = tmp_path / "r1c1.png"
    _write_png(tile)
    data_url = "data:image/png;base64," + base64.b64encode(tile.read_bytes()).decode("ascii")
    observed = []
    hooks = HookRegistry()
    hooks.on_after_tool(lambda _name, _args, result, _tool: observed.append(("after", result.copy())))
    hooks.on_tool_result(lambda _name, _args, result, _tool: observed.append(("result", result.copy())))
    registry = ToolRegistry(hooks=hooks)
    register_image_tools(
        registry,
        workdir=str(tmp_path),
        select_tiles=lambda _set, _ids: {
            "data_urls": [data_url],
            "labels": ["image1-r1c1"],
            "unserved_ids": [],
            "remaining_images": 0,
            "remaining_inline_bytes": 0,
        },
    )

    result = run(registry.execute("read_image_tiles", {
        "tile_set_id": "opaque",
        "tile_ids": ["image1-r1c1"],
    }))

    assert result["_private_result"] == {"image_data_urls": [data_url]}
    assert observed
    assert all("data:image" not in json.dumps(item) for _kind, item in observed)
    assert all("_private_result" not in item for _kind, item in observed)


def test_read_image_tiles_private_bytes_are_extracted_before_around_hook_transform(tmp_path):
    tile = tmp_path / "around.png"
    _write_png(tile)
    data_url = "data:image/png;base64," + base64.b64encode(tile.read_bytes()).decode("ascii")
    observed = []
    hooks = HookRegistry()

    async def around(_name, _args, _tool, call_next):
        public_result = await call_next()
        observed.append(public_result)
        payload = json.loads(public_result)
        payload["middleware"] = "public-only"
        return json.dumps(payload)

    hooks.on_around_tool(around)
    registry = ToolRegistry(hooks=hooks)
    register_image_tools(
        registry,
        workdir=str(tmp_path),
        select_tiles=lambda _set, _ids: {
            "data_urls": [data_url],
            "labels": ["image1-r1c1"],
            "unserved_ids": [],
            "remaining_images": 0,
            "remaining_inline_bytes": 0,
        },
    )

    result = run(registry.execute("read_image_tiles", {
        "tile_set_id": "around",
        "tile_ids": ["image1-r1c1"],
    }))

    assert result["error"] == ""
    assert json.loads(result["output"])["middleware"] == "public-only"
    assert result["_private_result"] == {"image_data_urls": [data_url]}
    assert all(isinstance(item, str) for item in observed)
    assert all("data:image" not in item for item in observed)


def test_concurrent_private_tool_results_are_isolated_from_around_hooks(tmp_path):
    urls = {}
    for name in ("first", "second"):
        tile = tmp_path / f"{name}.png"
        _write_png(tile)
        urls[name] = "data:image/png;base64," + base64.b64encode(
            tile.read_bytes() + name.encode()
        ).decode("ascii")

    observed = []
    entered = 0
    both_entered = asyncio.Event()
    hooks = HookRegistry()

    async def around(_name, args, _tool, call_next):
        nonlocal entered
        public_result = await call_next()
        entered += 1
        if entered == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=1)
        observed.append((args["tile_set_id"], public_result))
        return public_result

    hooks.on_around_tool(around)
    registry = ToolRegistry(hooks=hooks)
    register_image_tools(
        registry,
        workdir=str(tmp_path),
        select_tiles=lambda tile_set_id, _ids: {
            "data_urls": [urls[tile_set_id]],
            "labels": [f"image-{tile_set_id}"],
            "unserved_ids": [],
            "remaining_images": 0,
            "remaining_inline_bytes": 0,
        },
    )

    async def scenario():
        return await asyncio.gather(*(
            registry.execute("read_image_tiles", {
                "tile_set_id": name,
                "tile_ids": [f"image-{name}"],
            })
            for name in ("first", "second")
        ))

    first, second = run(scenario())

    assert first["_private_result"] == {"image_data_urls": [urls["first"]]}
    assert second["_private_result"] == {"image_data_urls": [urls["second"]]}
    assert all(
        isinstance(public, str) and "data:image" not in public
        for _name, public in observed
    )


def test_read_image_tiles_rejects_duplicate_and_overlong_ids_before_selector(tmp_path):
    calls = []

    def select(tile_set_id, tile_ids):
        calls.append((tile_set_id, tile_ids))
        return {}

    registry = ToolRegistry()
    register_image_tools(registry, workdir=str(tmp_path), select_tiles=select)

    duplicate = run(registry.execute("read_image_tiles", {
        "tile_set_id": "opaque",
        "tile_ids": ["image1-r1c1", "image1-r1c1"],
    }))
    overlong = run(registry.execute("read_image_tiles", {
        "tile_set_id": "opaque",
        "tile_ids": [f"image1-r1c{index}" for index in range(1, 13)],
    }))

    assert "duplicate" in duplicate["error"]
    assert "between 1 and 11" in overlong["error"]
    assert calls == []


def test_read_image_tiles_is_registered_only_with_bounded_selector(tmp_path):
    without_selector = ToolRegistry()
    register_image_tools(without_selector, workdir=str(tmp_path))
    assert run(without_selector.execute("read_image_tiles", {}))["code"] == "invalid_arguments"

    with_selector = ToolRegistry()
    register_image_tools(with_selector, workdir=str(tmp_path), select_tiles=lambda _set, _ids: {
        "data_urls": [],
        "labels": [],
        "unserved_ids": [],
        "remaining_images": 0,
        "remaining_inline_bytes": 0,
    })
    definition = next(
        tool["function"]
        for tool in with_selector.to_openai_tools()
        if tool["function"]["name"] == "read_image_tiles"
    )
    assert definition["parameters"]["properties"]["tile_ids"]["maxItems"] == 11
    metadata = next(item for item in with_selector.describe() if item["name"] == "read_image_tiles")
    assert metadata == {
        **metadata,
        "risk": "read",
        "idempotent": False,
        "max_calls_per_turn": 2,
        "group": "image",
    }
    definition = with_selector.get("read_image_tiles")
    assert definition is not None
    assert definition.cache_results is False


def test_all_local_image_entrypoints_register_request_scoped_tile_selector():
    project_root = Path(__file__).resolve().parents[1]
    entrypoints = (
        "agent/cli/main.py",
        "agent/cli/backend.py",
        "agent/cli/api_server.py",
    )

    for relative in entrypoints:
        tree = ast.parse((project_root / relative).read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "register_image_tools"
        ]
        assert calls, f"{relative} must register local image tools"
        assert all(
            any(keyword.arg == "select_tiles" for keyword in call.keywords)
            for call in calls
        ), f"{relative} must inject the request-scoped tile selector"


def test_agent_holder_tile_selector_fails_clearly_until_agent_is_ready(tmp_path):
    holder = {}
    registry = ToolRegistry()
    register_image_tools(
        registry,
        workdir=str(tmp_path),
        select_tiles=lambda tile_set_id, tile_ids: (
            image_module.select_vision_tiles_from_holder(
                holder,
                tile_set_id,
                tile_ids,
            )
        ),
    )

    unavailable = run(registry.execute("read_image_tiles", {
        "tile_set_id": "opaque",
        "tile_ids": ["image1-r1c1"],
    }))

    assert unavailable["code"] == "vision_tile_selection_rejected"
    assert "agent is ready" in unavailable["error"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows WSL path translation")
def test_resolve_image_path_translates_wsl_linux_paths_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.runtime.tools.image.sys.platform", "win32")
    monkeypatch.setenv("AGENT_WSL_DISTRO", "Ubuntu-24.04")

    resolved = _resolve_image_path("/home/test/output/sample.png", tmp_path)

    assert str(resolved) == r"\\wsl.localhost\Ubuntu-24.04\home\test\output\sample.png"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows WSL path translation")
def test_resolve_image_path_translates_wsl_mounted_windows_drive(monkeypatch, tmp_path):
    monkeypatch.setattr("agent.runtime.tools.image.sys.platform", "win32")

    resolved = _resolve_image_path("/mnt/d/images/sample.png", tmp_path)

    assert str(resolved) == r"D:\images\sample.png"


def test_materialize_wsl_image_uses_argument_safe_copy(monkeypatch, tmp_path):
    class Result:
        def __init__(self, returncode=0, stdout=b"", stderr=b""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[4] == "stat":
            return Result(stdout=b"4\n")
        kwargs["stdout"].write(b"data")
        return Result()

    monkeypatch.setattr("agent.runtime.tools.image.subprocess.run", fake_run)
    monkeypatch.setenv("AGENT_WSL_DISTRO", "Ubuntu")

    result = _materialize_wsl_image("/home/test/a file.png", tmp_path, 100)

    assert result.read_bytes() == b"data"
    assert calls[0][0] == ["wsl.exe", "-d", "Ubuntu", "--", "stat", "-c", "%s", "--", "/home/test/a file.png"]
    assert calls[1][0] == ["wsl.exe", "-d", "Ubuntu", "--", "cat", "--", "/home/test/a file.png"]
    expected_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    assert calls[0][1]["creationflags"] == expected_flags
    assert calls[1][1]["creationflags"] == expected_flags


def test_inspect_image_metadata_summarizes_comfyui_prompt_graph(tmp_path):
    image = tmp_path / "generated.png"
    workflow = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "example-model.safetensors"}},
        "2": {"class_type": "LoraLoader", "inputs": {
            "lora_name": "style.safetensors", "strength_model": 0.8, "strength_clip": 0.0,
        }},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "positive prompt"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "negative prompt"}},
        "6": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1344, "batch_size": 1}},
        "7": {"class_type": "KSampler", "inputs": {
            "positive": ["4", 0], "negative": ["5", 0], "seed": 42,
            "steps": 16, "cfg": 1.0, "sampler_name": "euler_ancestral", "scheduler": "normal",
        }},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "generated/test"}},
    }
    _write_comfy_png(image, workflow)

    result = _inspect_image_metadata(image)
    generation = result["generation"]

    assert result["metadata_found"] is True
    assert result["prompt_extracted"] is True
    assert result["raw_metadata_included"] is False
    assert result["raw_metadata_needed_for_prompt"] is False
    assert "No additional metadata extraction is required" in result["guidance"]
    assert result["image"]["width"] == 1024
    assert list(generation)[:4] == ["format", "node_count", "positive_prompt", "negative_prompt"]
    assert generation["models"][0]["unet_name"] == "example-model.safetensors"
    assert generation["loras"][0]["strength_model"] == 0.8
    assert generation["samplers"][0]["seed"] == 42
    assert generation["positive_prompt"] == "positive prompt"
    assert generation["negative_prompt"] == "negative prompt"
    assert generation["latent_images"][0]["height"] == 1344
    assert generation["filename_prefixes"] == ["generated/test"]

    raw_result = _inspect_image_metadata(image, include_raw=True)
    assert raw_result["prompt_extracted"] is True
    assert raw_result["raw_metadata_included"] is True
    assert raw_result["raw_metadata_needed_for_prompt"] is False
    assert "prompt" in raw_result["raw_metadata"]


def test_inspect_image_metadata_tool_is_exposed(tmp_path):
    image = tmp_path / "generated.png"
    _write_comfy_png(image, {})
    registry = ToolRegistry()
    register_image_tools(registry, workdir=str(tmp_path))

    async def scenario():
        result = await registry.execute("inspect_image_metadata", {"path": str(image)})
        payload = json.loads(result["output"])
        assert result["error"] == ""
        assert payload["metadata_keys"] == ["prompt"]

    run(scenario())


def test_read_image_tool_attaches_image_to_next_llm_turn(tmp_path):
    image = tmp_path / "sample.png"
    _write_png(image)

    class FakeLLM:
        def __init__(self):
            self.calls = 0
            self.second_messages = None

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield {
                    "type": "tool_calls",
                    "calls": [{
                        "id": "call-1",
                        "name": "read_image",
                        "arguments": json.dumps({
                            "path": str(image),
                            "question": "What is in this image?",
                        }),
                    }],
                    "content": "",
                    "usage": None,
                }
                return

            self.second_messages = messages
            yield {"type": "done", "content": "I can see the image.", "usage": None}

    async def scenario():
        registry = ToolRegistry()
        register_image_tools(registry, workdir=str(tmp_path))
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=3)

        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text(str(image))]))]

        assert events[-1]["type"] == "done"
        assert llm.calls == 2
        image_messages = [
            msg for msg in llm.second_messages
            if isinstance(msg.get("content"), list)
            and any(part.get("type") == "image_url" for part in msg["content"])
        ]
        assert image_messages
        note = next(
            line
            for part in image_messages[0]["content"] if part.get("type") == "text"
            for line in part.get("text", "").splitlines() if "What is in this image?" in line
        )
        # The model wrote the question when it called the tool, so it must not be handed back as the user's words.
        assert "用户" not in note and "user" not in note.lower()
        assert "调用工具" in note
        image_part = next(part for part in image_messages[0]["content"] if part["type"] == "image_url")
        assert image_part["image_url"]["url"].startswith("data:image/png;base64,")

    run(scenario())
