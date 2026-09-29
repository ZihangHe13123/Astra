"""Real backend protocol: the UI is told which model a Claude Code alias resolved to."""

import json
import os
import stat
import time

import pytest

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake claude executable is a shebang script")

# The parts of the CLI's stream Astra reads: the init line first, an acknowledgement for each replayed
# history turn, then one answer.
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, sys
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}))
    sys.exit(0)
emit = lambda value: (print(json.dumps(value)), sys.stdout.flush())
emit({"type": "system", "subtype": "init", "model": "claude-sonnet-5"})
for line in sys.stdin:
    frame = json.loads(line)
    if frame["type"] == "assistant":
        continue
    if frame.get("shouldQuery") is False:
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "usage": {}})
        continue
    break
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "pong"}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": "pong",
      "usage": {"input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 5}})
"""

MODELS = """version: 1
providers:
  claude:
    label: Claude Code
    provider: claude-code
    base_url: claude-code://local
    api_key_env: ''
    context_limit: 1000000
    capabilities: [tools, streaming, reasoning, vision]
models:
  sonnet:
    provider: claude-code
    base_url: claude-code://local
    api_key_env: ''
    context_limit: 1000000
    capabilities: [tools, streaming, reasoning, vision]
"""


def send(proc, frame: dict) -> None:
    proc.stdin.write(json.dumps(frame) + "\n")
    proc.stdin.flush()


def model_listing(proc, wait_for) -> str:
    """`/model` answers busy until the finished reply's task has wound down, a moment after done."""
    for _ in range(50):
        send(proc, {"type": "command", "cmd": "/model"})
        result = wait_for(lambda event: event.get("type") == "tool_result" and event.get("name") == "model")
        if result.get("code") != "busy":
            return result["output"]
        time.sleep(0.1)
    raise AssertionError("/model stayed busy")


def test_backend_reports_the_model_an_alias_resolved_to_once_an_answer_shows_it(tmp_path):
    """Live Astra 2026-09-29: an older CLI resolved `sonnet` to claude-sonnet-5 after 5.5 was out, and
    the screen only said `sonnet`. Nothing is known before the first answer; the model_info that ends
    each turn carries the model from then on, and `/model` says it too."""
    fake = tmp_path / "claude"
    fake.write_text(FAKE_CLAUDE, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    models = tmp_path / "claude-models.yaml"
    models.write_text(MODELS, encoding="utf-8")
    proc, _, seen, wait_for = _start_question_protocol_backend(
        tmp_path, 0, "served_model",
        settings_overrides={"selected_model": "sonnet"},
        env_overrides={"AGENT_MODELS_FILE": str(models), "ASTRA_CLAUDE_CODE_COMMAND": str(fake)})
    try:
        first = wait_for(lambda event: event.get("type") == "model_info")
        assert first["model"] == "sonnet" and "served_model" not in first

        send(proc, {"type": "message", "text": "ping"})
        wait_for(lambda event: event.get("type") == "done")
        second = wait_for(lambda event: event.get("type") == "model_info")
        assert (second["model"], second["served_model"]) == ("sonnet", "claude-sonnet-5")

        # A later turn replays history: the same model, still reported.
        send(proc, {"type": "message", "text": "again"})
        wait_for(lambda event: event.get("type") == "done")
        third = wait_for(lambda event: event.get("type") == "model_info")
        assert third["served_model"] == "claude-sonnet-5"
        listing = model_listing(proc, wait_for)
        assert listing.startswith("Current model: sonnet (serving claude-sonnet-5) @ claude-code://local")
        assert [e.get("served_model") for e in seen if e.get("type") == "model_info"][:1] == [None]
    finally:
        _stop_protocol_backend(proc)
