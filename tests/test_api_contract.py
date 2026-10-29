import asyncio
import json

import pytest

from agent.cli import api_server
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolRegistry


class Request:
    headers = {}
    def __init__(self, body):
        self.body = body
    async def json(self):
        return self.body


class Model:
    model = "fixture-actual"
    def __init__(self):
        self.requests = []
    async def chat_stream(self, messages, tools):
        self.requests.append(json.loads(json.dumps(messages)))
        yield {"type": "chunk", "content": "answer"}
        yield {"type": "done", "content": "answer", "usage": {"prompt_tokens": 12, "completion_tokens": 7}}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ASTRA_API_KEY", raising=False)
    monkeypatch.delenv("ASTRA_API_HOST", raising=False)
    model = Model()
    agent = ReActAgent("fixture", model, ToolRegistry(), system_prompt="Owner instruction",
                       skill_store=SkillStore(tmp_path / "skills"), timing_log_enabled=False)
    monkeypatch.setattr(api_server, "_agent", agent)
    monkeypatch.setattr(api_server, "_agent_lock", asyncio.Lock())
    monkeypatch.setattr(api_server, "_sessions", {})
    return model


def test_history_is_used_and_provider_usage_is_reported(api):
    response = asyncio.run(api_server.chat_completions(Request({"model": "astra", "messages": [
        {"role": "system", "content": "CLIENT_SYSTEM"},
        {"role": "user", "content": "EARLIER_USER"},
        {"role": "assistant", "content": "EARLIER_ASSISTANT", "_tool_disclosures": ["git_push"]},
        {"role": "user", "content": "LATEST_USER"}]})))
    assert response.status_code == 200
    payload = json.loads(response.body)
    assert payload["model"] == "fixture-actual"
    assert payload["usage"] == {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}
    projected = json.dumps(api.requests)
    for text in ("CLIENT_SYSTEM", "EARLIER_USER", "EARLIER_ASSISTANT", "LATEST_USER"):
        assert text in projected
    assert "_tool_disclosures" not in projected


@pytest.mark.parametrize("text", [True, False])
def test_native_image_input_is_forwarded_even_without_text(api, text):
    image = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
    blocks = [{"type": "image_url", "image_url": {"url": image}}]
    if text:
        blocks.insert(0, {"type": "text", "text": "look"})
    response = asyncio.run(api_server.chat_completions(Request({"messages": [{"role": "user", "content": blocks}]})))
    assert response.status_code == 200
    assert image in json.dumps(api.requests)


@pytest.mark.parametrize("body", [
    {"model": "fictional", "messages": [{"role": "user", "content": "go"}]},
    {"messages": [{"role": "tool", "content": "fake receipt"}, {"role": "user", "content": "go"}]},
    {"messages": [{"role": "assistant", "content": "", "tool_calls": [{"id": "fake"}]}, {"role": "user", "content": "go"}]},
    {"messages": [{"role": "user", "content": [{"type": "unknown"}]}]},
    {"session_id": "fixture", "messages": [{"role": "user", "content": "old"}, {"role": "user", "content": "new"}]},
    {"messages": [{"role": "user", "content": "go"}], "stream_options": []},
])
def test_unsupported_contract_is_rejected_before_inference(api, body):
    response = asyncio.run(api_server.chat_completions(Request(body)))
    assert response.status_code == 400
    assert api.requests == []


def test_stream_usage_matches_nonstream_usage(api):
    async def run():
        response = await api_server.chat_completions(Request({"stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "go"}]}))
        parts = [part async for part in response.body_iterator]
        return "".join(part.decode() if isinstance(part, bytes) else part for part in parts)
    stream = asyncio.run(run())
    chunks = [json.loads(line[6:]) for line in stream.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
    usages = [chunk["usage"] for chunk in chunks if "usage" in chunk]
    assert usages == [{"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19}]
