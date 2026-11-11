"""Real backend + scripted model: an earlier tool result reads the same in the next model request.

What a request repeats from the one before it is billed at a fraction of the price, up to the
first place where the two differ. A result that is reworded later is paid for again together
with everything after it, so these tests put the requests of one conversation side by side.
"""

import json
import re
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from test_backend_task_protocol import (
    _SlowOpenAIHandler,
    _start_question_protocol_backend,
    _stop_protocol_backend,
)
from test_session_lifecycle_protocol import send

CLEARED = "[Old tool result content cleared"
SHORTENED = "[Tool output truncated"


def _text(message: dict) -> str:
    content = message.get("content") or ""
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content)


def _plain(message: dict) -> str:
    return re.sub(r"^<message_time>.*?</message_time>\s*", "", _text(message))


def _asked(messages: list[dict]) -> tuple[int, str]:
    """Where the request being answered sits, and what it says."""
    index = max(i for i, m in enumerate(messages) if m["role"] == "user" and _plain(m).startswith("Read"))
    return index, _plain(messages[index])


class Script:
    """A model that reads the files a request names, one in each step, and then answers."""

    def __init__(self):
        self.requests: list[list[dict]] = []
        self.call_for: dict[str, str] = {}
        self.lock = threading.Lock()

    def reply(self, messages: list[dict]):
        index, text = _asked(messages)
        files = text.split()[1:]
        read = sum(m["role"] == "tool" for m in messages[index + 1:])
        with self.lock:
            self.requests.append(messages)
            if read == len(files):
                return f"Read {len(files)}."
            self.call_for[files[read]] = f"call-{len(self.requests)}"
            return {"id": self.call_for[files[read]], "path": files[read]}


@pytest.fixture
def model():
    script = Script()

    class Handler(_SlowOpenAIHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if not (payload.get("stream") and payload.get("tools")):
                # Not a step of the conversation.
                self._json({"id": "fixture", "object": "chat.completion", "model": "Qwen3.6-35B-A3B",
                            "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": "ok"}}]})
                return
            answer = script.reply(payload["messages"])
            if isinstance(answer, dict):
                delta = {"content": "", "tool_calls": [{
                    "index": 0, "id": answer["id"], "type": "function",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": answer["path"]})},
                }]}
                finish = "tool_calls"
            else:
                delta, finish = {"content": answer}, "stop"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "model": "Qwen3.6-35B-A3B",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            try:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, script
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _page(name: str, chars: int, middle: str = "") -> str:
    """A text of this many characters, with a word halfway that a head-and-tail preview leaves out."""
    filler = "".join(f"{name} line {number:04d} of plain filler text\n" for number in range(chars // 30 + 1))
    half = (chars - len(middle)) // 2
    return filler[:half] + middle + filler[half:chars - len(middle)]


def _converse(tmp_path: Path, model, files: dict[str, str], turns: list[str], **limits: str) -> Script:
    """Hold these turns with a backend whose workspace has these files; return what the model was sent."""
    port, script = model
    work = tmp_path / "work"
    work.mkdir()
    for name, text in files.items():
        (work / name).write_bytes(text.encode("utf-8"))
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, port, "tool_result_session", workdir=work,
        env_overrides={
            "SANDBOX_WORKDIR": str(work),
            "TOOL_RESULT_DIR": str(tmp_path / "tool-results"),
            "ASTRA_CHANNEL_CONFIG": str(tmp_path / "no-channels.json"),
            "ASTRA_CONTEXT_INDEX_EMBEDDING": "off",
            # Stated in full: a checkout's own configuration must not decide any of them.
            "TOOL_MAX_INLINE_CHARS": "12000",
            "TOOL_MAX_FRESH_RESULT_CHARS": "100000",
            "TOOL_RESULT_PROMPT_BUDGET_CHARS": "24000",
            "TOOL_RESULT_KEEP_RECENT": "5",
            "TOOL_RESULT_CLEAR_BATCH": "10",
            **limits,
        },
    )
    try:
        wait_for(lambda event: event.get("type") == "startup_banner", timeout=30)
        for text in turns:
            send(proc, {"type": "message", "text": text})
            wait_for(lambda event: event.get("type") == "done")
    finally:
        _stop_protocol_backend(proc)
    return script


def _results(request: list[dict]) -> dict[str, str]:
    """Each tool result a request carries, by the call it answers, oldest first."""
    return {m["tool_call_id"]: _text(m) for m in request if m["role"] == "tool"}


def _repeats(earlier: list[dict], later: list[dict], *, apart_from: set[str]) -> bool:
    """Whether the later request begins with the earlier one, the named results aside."""
    def conversation(request: list[dict]) -> list[dict]:
        return [m for m in request if m["role"] != "system"]

    before = conversation(earlier)
    after = conversation(later)[:len(before)]
    return ([m for m in after if m.get("tool_call_id") not in apart_from]
            == [m for m in before if m.get("tool_call_id") not in apart_from])


def test_a_result_up_to_twice_the_inline_limit_is_never_swapped_for_a_preview(tmp_path, model):
    files = {
        "near.txt": _page("near", 3_000, "NEAR-MIDDLE"),
        "far.txt": _page("far", 6_000, "FAR-MIDDLE"),
        "small.txt": _page("small", 600),
    }
    script = _converse(tmp_path, model, files, ["Read near.txt far.txt", "Read small.txt"],
                       TOOL_MAX_INLINE_CHARS="2000")
    requests = script.requests
    near, far = script.call_for["near.txt"], script.call_for["far.txt"]

    # One and a half times the limit: the same complete text in the step that asked for it, the
    # step after and the next turn.
    seen_near = [_results(request)[near] for request in requests if near in _results(request)]
    assert len(seen_near) == 4 and len(set(seen_near)) == 1
    assert files["near.txt"] in seen_near[0]
    assert "Result completeness: complete." in seen_near[0] and SHORTENED not in seen_near[0]

    # Three times the limit: whole for the one step that asked for it, a preview from then on.
    seen_far = [_results(request)[far] for request in requests if far in _results(request)]
    assert len(seen_far) == 3
    assert files["far.txt"] in seen_far[0]
    assert seen_far[1] == seen_far[2]
    assert SHORTENED in seen_far[1] and "middle omitted" in seen_far[1] and "FAR-MIDDLE" not in seen_far[1]
    saved = re.search(r"Full result saved to (.+?)\]", seen_far[1])
    assert saved and Path(saved.group(1)).read_text(encoding="utf-8") == files["far.txt"]

    # That swap is the only place where a request differs from the one before it.
    for earlier, later in zip(requests, requests[1:]):
        swapped = _results(earlier).get(far) == seen_far[0]
        assert _repeats(earlier, later, apart_from={far} if swapped else set())


def test_old_results_are_cleared_a_batch_at_a_time_when_a_turn_begins(tmp_path, model):
    budget, batch = 6_000, 3
    files = {f"page-{number}.txt": _page(f"page-{number}", 1_500) for number in range(1, 10)}
    busy = "Read page-4.txt page-5.txt page-6.txt"
    script = _converse(
        tmp_path, model, files,
        ["Read page-1.txt", "Read page-2.txt", "Read page-3.txt", busy,
         "Read page-7.txt", "Read page-8.txt", "Read page-9.txt", "Read"],
        TOOL_RESULT_PROMPT_BUDGET_CHARS=str(budget), TOOL_RESULT_KEEP_RECENT="1",
        TOOL_RESULT_CLEAR_BATCH=str(batch),
    )
    requests = script.requests

    cleared: list[str] = []
    cleared_in: list[int] = []
    for number, request in enumerate(requests):
        results = _results(request)
        now = [call for call, text in results.items() if text.startswith(CLEARED)]
        # Oldest first, and a result that was cleared stays cleared.
        assert now == list(results)[:len(now)] and now[:len(cleared)] == cleared
        new = now[len(cleared):]
        if number:
            assert _repeats(requests[number - 1], request, apart_from=set(new))
        if new:
            # Only whole batches, and only before the model has started on the request.
            assert len(new) % batch == 0
            assert _asked(request)[0] == max(i for i, m in enumerate(request) if m["role"] != "system")
            cleared_in.append(number)
        cleared = now

    # The turn that read three files carried more than the budget by its end, and still every
    # one of its requests repeated the one before it.
    during = [request for request in requests if _asked(request)[1] == busy]
    assert len(during) == 4
    assert sum(len(text) for text in _results(during[-1]).values()) > budget
    assert not any(text.startswith(CLEARED) for request in during for text in _results(request).values())
    # Results went over the budget one at a time, turn after turn; a later batch went the same way.
    assert len(cleared_in) >= 2
