"""A turn that fails is an error over HTTP, not an assistant answer.

The API server runs on a free loopback port with lifespan off (no .env, no bootstrap) and talks to
a fake upstream model server on another loopback port through the real LLM client.
"""

import asyncio
import json
import socket

import httpx
import pytest
import uvicorn

from agent.cli import api_server
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.tools.registry import ToolRegistry

ANSWER = (
    b'data: {"id":"u","object":"chat.completion.chunk","created":0,"model":"fake-model",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"hi"},"finish_reason":null}]}\n\n'
    b'data: {"id":"u","object":"chat.completion.chunk","created":0,"model":"fake-model",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, mode: str) -> None:
    try:
        await reader.readuntil(b"\r\n\r\n")
        if mode == "answer":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n" + ANSWER)
        elif mode == "stall":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
            await writer.drain()
            await asyncio.sleep(30)
        else:
            body = json.dumps({"error": {"message": "UPSTREAM_DETAIL", "type": "server_error"}}).encode()
            status = b"504 Gateway Timeout" if mode == "504" else b"500 Internal Server Error"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nConnection: close\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("ASTRA_API_KEY", raising=False)
    monkeypatch.setattr(api_server, "_sessions", {})

    def run(mode: str, *bodies: dict, model=None) -> list[httpx.Response]:
        async def scenario():
            monkeypatch.setattr(api_server, "_agent_lock", asyncio.Lock())
            fake = await asyncio.start_server(lambda r, w: upstream(r, w, mode), "127.0.0.1", 0)
            port = fake.sockets[0].getsockname()[1]
            config = LLMConfig(provider="openai-compatible", model="fake-model", api_key="local",
                               base_url=f"http://127.0.0.1:{port}/v1", timeout=5, connect_timeout=2, idle_timeout=0.5,
                               overall_timeout=5, stream_overall_timeout=5, max_retries=0, reasoning_effort=None)
            monkeypatch.setattr(api_server, "_agent", ReActAgent(
                "fixture", model or LLMClient(config), ToolRegistry(), system_prompt="sys",
                skill_store=SkillStore(tmp_path / "skills"), timing_log_enabled=False, max_iterations=2))
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            server = uvicorn.Server(uvicorn.Config(api_server.app, lifespan="off", log_level="warning"))
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                while not server.started:
                    await asyncio.sleep(0.01)
                url = f"http://127.0.0.1:{sock.getsockname()[1]}/v1/chat/completions"
                async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                    responses = []
                    for body in bodies:
                        response = await client.post(url, json=body)
                        await response.aread()
                        responses.append(response)
                    return responses
            finally:
                server.should_exit = True
                await serving
                fake.close()
                await fake.wait_closed()
        return asyncio.run(scenario())
    return run


HELLO = {"messages": [{"role": "user", "content": "hello"}]}
STREAM = {**HELLO, "stream": True, "stream_options": {"include_usage": True}}


def frames(response: httpx.Response) -> list:
    data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
    return [item if item == "[DONE]" else json.loads(item) for item in data]


def assert_failed_stream(response: httpx.Response, code: str) -> None:
    assert response.status_code == 200
    sent = frames(response)
    assert sent[-1] == "[DONE]"
    errors = [frame for frame in sent if isinstance(frame, dict) and "error" in frame]
    assert [error["error"]["code"] for error in errors] == [code]
    finishes = [choice["finish_reason"] for frame in sent if isinstance(frame, dict)
                for choice in frame.get("choices", []) if choice["finish_reason"] is not None]
    assert finishes == ["error"]
    # The failure is not presented as assistant text, and the usage chunk still follows it.
    assert "[Error:" not in response.text
    assert sent.index(errors[0]) < len(sent) - 2 and "usage" in sent[-2]


def test_answers_keep_the_openai_shape(api):
    plain, streamed = api("answer", HELLO, STREAM)
    assert plain.status_code == 200
    assert plain.json()["choices"][0] == {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
    sent = frames(streamed)
    assert [c["delta"].get("content") for f in sent[:-1] for c in f.get("choices", []) if c["delta"]] == ["hi"]
    assert sent[-3]["choices"][0]["finish_reason"] == "stop" and sent[-1] == "[DONE]"


def test_an_idle_upstream_is_a_gateway_timeout(api):
    plain, streamed = api("stall", HELLO, STREAM)
    assert plain.status_code == 504
    assert plain.json()["error"]["code"] == "idle_timeout"
    assert plain.json()["error"]["type"] == "server_error"
    assert "choices" not in plain.json()
    assert_failed_stream(streamed, "idle_timeout")


@pytest.mark.parametrize(("mode", "status", "code"), [("504", 504, "upstream_timeout"), ("500", 502, "upstream_error")])
def test_a_raised_upstream_failure_is_reported_without_provider_text(api, mode, status, code):
    plain, streamed = api(mode, HELLO, STREAM)
    assert plain.status_code == status
    assert plain.json()["error"]["code"] == code
    assert "UPSTREAM_DETAIL" not in plain.text
    assert_failed_stream(streamed, code)
    assert "UPSTREAM_DETAIL" not in streamed.text


class TimedOutAttempt:
    model = "fake-model"

    async def chat_stream(self, messages, tools):
        raise TimeoutError("LLM request attempt timed out after 3s")
        yield  # pragma: no cover - makes this an async generator


def test_a_timed_out_attempt_is_a_gateway_timeout(api):
    plain, streamed = api("answer", HELLO, STREAM, model=TimedOutAttempt())
    assert plain.status_code == 504
    assert plain.json()["error"] == {"message": "LLM request attempt timed out after 3s", "type": "server_error",
                                     "code": "upstream_timeout"}
    assert_failed_stream(streamed, "upstream_timeout")


@pytest.mark.parametrize("body", [
    {"model": "astra"},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/a.png", "detail": ["high"]}}]}]},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/a.png", "detail": {}}}]}]},
])
def test_invalid_input_is_a_400_before_the_model(api, body):
    (response,) = api("500", body)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
