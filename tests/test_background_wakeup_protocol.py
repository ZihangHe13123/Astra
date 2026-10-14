"""Real backend + scripted model: background work that ends after the reply wakes the agent."""

import json
import re
import threading
import time
from http.server import ThreadingHTTPServer

import pytest
from test_backend_task_protocol import (
    _SlowOpenAIHandler,
    _start_question_protocol_backend,
    _stop_protocol_backend,
)
from test_session_lifecycle_protocol import send

WAKE = "[Background work ended"
RUN_ENDED = "[SYSTEM-DELIVERED BACKGROUND RUN ENDED"
SUBAGENT_RESULT = "[SYSTEM-DELIVERED SUBAGENT RESULT"


def _text(message: dict) -> str:
    content = message.get("content") or ""
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    return str(content)


def _plain(message: dict) -> str:
    return re.sub(r"^<message_time>.*?</message_time>\s*", "", _text(message))


def _tool_json(message: dict) -> dict:
    # A tool result reaches the model as its JSON between a header line and notes.
    text = _text(message)
    return json.JSONDecoder().raw_decode(text[text.index("{"):])[0]


def _call(name: str, **arguments) -> dict:
    return {"tool": name, "arguments": arguments}


class Script:
    """What the model answers, decided from the conversation it is sent.

    A command meant to end after the turn answered sleeps two seconds, so on a busy
    machine it is still running when the agent takes its next step.
    """

    def __init__(self):
        self.parent_requests: list[list[dict]] = []
        self.worker_gate = threading.Event()
        self.worker_gate.set()
        self.lock = threading.Lock()

    def reply(self, messages: list[dict]):
        system = _text(messages[0]) if messages and messages[0]["role"] == "system" else ""
        if system.startswith(("You are a focused explorer agent", "You are the final report writer")):
            self.worker_gate.wait(timeout=20)
            return "Survey report: three notes found."
        with self.lock:
            self.parent_requests.append(messages)
        # The request being answered: the last user message that is not a delivered notice.
        user_index = max(i for i, m in enumerate(messages) if m["role"] == "user" and m.get("content")
                         and not _plain(m).startswith(("[SYSTEM-DELIVERED", "[SYSTEM-SUPPLIED")))
        user = _plain(messages[user_index])
        after = messages[user_index + 1:]
        tools_run = [m for m in after if m["role"] == "tool"]
        if WAKE in user:
            if "chain-1" in user:
                if not tools_run:
                    return _call("execute_shell", command="sleep 2 && echo chain-2", background=True)
                return "Started chain 2."
            if SUBAGENT_RESULT in user:
                return "The survey found three notes."
            return "The check passed."
        if user == "Run the slow check in the background":
            if not tools_run:
                return _call("execute_shell", command="sleep 2 && echo slow-check-ok", background=True)
            return "Started the check."
        if user == "Run a quick check while you work":
            if not tools_run:
                return _call("execute_shell", command="echo quick-check-ok", background=True)
            if len(tools_run) == 1:
                return _call("execute_shell", command="sleep 1.5", foreground_yield_ms=10000)
            return "Quick check done."
        if user == "Run it and wait for it yourself":
            if not tools_run:
                # Still running at the next step, so only the poll sees it end.
                return _call("execute_shell", command="sleep 2 && echo polled-ok", background=True)
            if len(tools_run) == 1:
                process_id = _tool_json(tools_run[0])["process_id"]
                return _call("process_poll", process_id=process_id, wait_ms=15000)
            return "It finished."
        if user == "Delegate the survey":
            if not tools_run:
                return _call("delegate_task", goal="Survey the notes folder", mode="explorer", background=True)
            return "Dispatched the survey."
        if user == "Start the chain":
            if not tools_run:
                return _call("execute_shell", command="sleep 2 && echo chain-1", background=True)
            return "Started chain 1."
        return f"Answered: {user}"


@pytest.fixture
def model():
    script = Script()

    class Handler(_SlowOpenAIHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if "messages" not in payload:
                self._json({"total_tokens": 100})
                return
            answer = script.reply(payload["messages"])
            if isinstance(answer, dict):
                delta = {"content": "", "tool_calls": [{
                    "index": 0, "id": f"call-{time.monotonic_ns()}", "type": "function",
                    "function": {"name": answer["tool"], "arguments": json.dumps(answer["arguments"])},
                }]}
                finish = "tool_calls"
            else:
                delta, finish = {"content": answer}, "stop"
            if not payload.get("stream"):
                # Subagents ask without streaming.
                self._json({"id": "fixture", "object": "chat.completion", "model": "Qwen3.6-35B-A3B",
                            "choices": [{"index": 0, "finish_reason": finish,
                                         "message": {"role": "assistant", **delta}}],
                            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
                return
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
    script.worker_gate.set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def start(tmp_path, model, **env):
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, _, seen, wait_for = _start_question_protocol_backend(
        tmp_path, model[0], "background_session", workdir=workdir,
        env_overrides={"ASTRA_CHANNEL_CONFIG": str(tmp_path / "no-channels.json"),
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off", **env},
    )
    answered: set[str] = set()

    def wait(predicate, timeout: float = 20) -> dict:
        def check(event: dict) -> bool:
            # The scripted commands run on the host; this test approves each one.
            if event.get("type") == "tool_approval_request" and event["request_id"] not in answered:
                answered.add(event["request_id"])
                send(proc, {"type": "tool_approval_response", "request_id": event["request_id"], "decision": "once"})
            return predicate(event)
        return wait_for(check, timeout=timeout)

    def quiet(seconds: float) -> list[dict]:
        """The events of the next ``seconds``, waited out in full."""
        start_index = len(seen)
        with pytest.raises(AssertionError, match="Timed out"):
            wait(lambda event: False, timeout=seconds)
        return seen[start_index:]

    wait(lambda e: e.get("type") == "startup_banner", timeout=30)
    return proc, seen, wait, quiet


def turn(proc, wait, text: str) -> list[str]:
    send(proc, {"type": "message", "text": text})
    chunks: list[str] = []
    wait(lambda e: (e.get("type") == "chunk" and chunks.append(e.get("content", ""))) or e.get("type") == "done")
    return chunks


def wakes(seen: list[dict], state: str = "started") -> list[dict]:
    return [e for e in seen if e.get("type") == "background_wakeup" and e.get("state") == state]


def tool_outputs(messages: list[dict]) -> list[str]:
    return [_text(m) for m in messages if m["role"] == "tool"]


def test_a_background_command_that_ends_after_the_reply_starts_one_turn_with_its_output(tmp_path, model):
    proc, seen, wait, quiet = start(tmp_path, model)
    script = model[1]
    try:
        assert "".join(turn(proc, wait, "Run the slow check in the background")) == "Started the check."
        started = script.parent_requests[-1]
        assert "you are told when it ends" in tool_outputs(started)[-1]

        wake = wait(lambda e: e.get("type") == "background_wakeup")
        assert wake["state"] == "started" and "sleep 2 && echo slow-check-ok" in wake["message"]
        chunks: list[str] = []
        wait(lambda e: (e.get("type") == "chunk" and chunks.append(e.get("content", ""))) or e.get("type") == "done")
        assert "".join(chunks) == "The check passed."
        woken = script.parent_requests[-1]
        delivered = [_text(m) for m in woken if m["role"] == "user" and WAKE in _text(m)]
        assert len(delivered) == 1
        assert "slow-check-ok" in delivered[0] and "exit code 0" in delivered[0]
        # The earlier exchange reaches the model unchanged ahead of the notice.
        earlier = [m for m in started if m["role"] != "system"]
        assert [m for m in woken if m["role"] != "system"][:len(earlier)] == earlier

        assert not wakes(quiet(2.5))
        # Reopened from disk, the turn shows as the notice, not as words of the user.
        send(proc, {"type": "command", "cmd": "/session background_session"})
        history = wait(lambda e: e.get("type") == "history")
        rows = [(m["role"], m["content"]) for m in history["messages"]]
        assert ("system", wake["message"]) in rows
        assert not any(role == "user" and WAKE in content for role, content in rows)
    finally:
        _stop_protocol_backend(proc)


def test_a_run_that_ends_while_the_agent_works_is_given_at_its_next_step_and_wakes_nothing(tmp_path, model):
    proc, seen, wait, quiet = start(tmp_path, model)
    script = model[1]
    try:
        assert "".join(turn(proc, wait, "Run a quick check while you work")) == "Quick check done."
        final = script.parent_requests[-1]
        notices = [_text(m) for m in final if RUN_ENDED in _text(m)]
        assert len(notices) == 1 and "quick-check-ok" in notices[0]
        assert not wakes(quiet(2.5))
    finally:
        _stop_protocol_backend(proc)


def test_a_run_the_agent_saw_end_itself_is_not_reported_again(tmp_path, model):
    proc, seen, wait, quiet = start(tmp_path, model)
    script = model[1]
    try:
        assert "".join(turn(proc, wait, "Run it and wait for it yourself")) == "It finished."
        polled = _tool_json(next(m for m in reversed(script.parent_requests[-1]) if m["role"] == "tool"))
        assert polled["status"] == "completed"
        assert not wakes(quiet(2.5))
        assert not any(RUN_ENDED in _text(m) or WAKE in _text(m)
                       for request in script.parent_requests for m in request)
    finally:
        _stop_protocol_backend(proc)


def test_the_parent_answers_while_its_subagent_works_and_the_report_starts_a_turn(tmp_path, model):
    # The fixture model is on this computer, where parent and subagent share its request
    # slots; two let the parent answer while the subagent's request is held.
    proc, seen, wait, quiet = start(tmp_path, model, LLM_MAX_CONCURRENT_REQUESTS="2")
    script = model[1]
    script.worker_gate.clear()
    try:
        # The subagent is held; the parent's turn still ends with its own answer.
        assert "".join(turn(proc, wait, "Delegate the survey")) == "Dispatched the survey."
        dispatched = tool_outputs(script.parent_requests[-1])[-1]
        assert "You need not wait for it" in dispatched
        assert "their results start a new turn for you" in _text(script.parent_requests[-1][-1])
        # The user keeps talking to the parent meanwhile.
        assert "".join(turn(proc, wait, "What is 2+2?")) == "Answered: What is 2+2?"
        assert not wakes(seen)

        script.worker_gate.set()
        wake = wait(lambda e: e.get("type") == "background_wakeup", timeout=30)
        assert "Survey the notes folder" in wake["message"]
        chunks: list[str] = []
        wait(lambda e: (e.get("type") == "chunk" and chunks.append(e.get("content", ""))) or e.get("type") == "done")
        assert "".join(chunks) == "The survey found three notes."
        woken = script.parent_requests[-1]
        reports = [_text(m) for m in woken if "Survey report: three notes found." in _text(m)]
        assert len(reports) == 1 and SUBAGENT_RESULT in reports[0] and WAKE in reports[0]
        assert not wakes(quiet(2.5))
    finally:
        script.worker_gate.set()
        _stop_protocol_backend(proc)


def test_automatic_turns_stop_at_the_limit_and_the_result_waits_for_the_user(tmp_path, model):
    proc, seen, wait, quiet = start(tmp_path, model, ASTRA_BACKGROUND_WAKE_LIMIT="1")
    script = model[1]
    try:
        assert "".join(turn(proc, wait, "Start the chain")) == "Started chain 1."
        wait(lambda e: e.get("type") == "background_wakeup" and e.get("state") == "started")
        wait(lambda e: e.get("type") == "done")
        limited = wait(lambda e: e.get("type") == "background_wakeup")
        assert limited["state"] == "limited"
        assert len(wakes(quiet(2.0))) == 0

        turn(proc, wait, "Any news?")
        asked = script.parent_requests[-1]
        question = max(i for i, m in enumerate(asked) if _plain(m) == "Any news?")
        notices = [_text(m) for m in asked[question + 1:] if RUN_ENDED in _text(m)]
        assert len(notices) == 1 and "chain-2" in notices[0]
        assert len(wakes(seen)) == 1
    finally:
        _stop_protocol_backend(proc)
