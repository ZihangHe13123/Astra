"""Real backend protocol: a Claude Code request's profile tells the CLI's wait from the model's."""

import json
import os
import stat

import pytest

from test_backend_served_model import MODELS, send
from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake claude executable is a shebang script")

START, TURN, ANSWER = 0.3, 0.15, 0.2
# A CLI that is slow in three places: starting, taking each earlier turn, and answering.
SLOW_CLAUDE = """#!/usr/bin/env python3
import json, sys, time
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}))
    sys.exit(0)
START, TURN, ANSWER = %r, %r, %r
emit = lambda value: (print(json.dumps(value)), sys.stdout.flush())
time.sleep(START)
emit({"type": "system", "subtype": "init", "model": "claude-sonnet-5"})
for line in sys.stdin:
    frame = json.loads(line)
    if frame["type"] == "assistant":
        continue
    if frame.get("shouldQuery") is False:
        time.sleep(TURN)
        emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "usage": {}})
        continue
    break
time.sleep(ANSWER)
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "pong"}]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": "pong",
      "usage": {"input_tokens": 10, "cache_read_input_tokens": 100, "output_tokens": 5}})
""" % (START, TURN, ANSWER)


def cli_phases(record: dict) -> dict:
    return {name: value for name, value in record["phases_ms"].items() if name.startswith("claude_code_")}


def test_a_replayed_request_profile_times_the_cli_apart_from_the_model(tmp_path):
    """Live Astra 2026-11-11: a 484-message thread waited 11 to 41 s for its first event, and the
    profile could not say whether the CLI taking the conversation or the model was slow. Each
    request that replays earlier turns now records the CLI's start (up to the first turn it took)
    and the rest of the replay; the model's wait is what remains of the time to the first event."""
    fake = tmp_path / "claude"
    fake.write_text(SLOW_CLAUDE, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    models = tmp_path / "claude-models.yaml"
    models.write_text(MODELS, encoding="utf-8")
    state = tmp_path / "state"
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, 0, "claude_code_timing",
        settings_overrides={"selected_model": "sonnet"},
        env_overrides={"AGENT_MODELS_FILE": str(models), "ASTRA_CLAUDE_CODE_COMMAND": str(fake),
                       "ASTRA_PROFILE_QUERY": "1", "ASTRA_HOME": str(state)})
    try:
        wait_for(lambda event: event.get("type") == "model_info")
        for text in ("one", "two", "three"):
            send(proc, {"type": "message", "text": text})
            wait_for(lambda event: event.get("type") == "done")
            wait_for(lambda event: event.get("type") == "model_info")
    finally:
        _stop_protocol_backend(proc)
    profile = (state / "query-profile.jsonl").read_text(encoding="utf-8")
    first, second, third = (json.loads(line) for line in profile.splitlines())

    # Nothing to replay: the CLI acknowledges no turn, so there is nothing to time its start by.
    assert cli_phases(first) == {}
    for record in (second, third):
        phases = cli_phases(record)
        assert set(phases) == {"claude_code_start", "claude_code_history_replay"}
        assert phases["claude_code_start"] >= (START + TURN) * 1000
        # The wait for the answer is in neither.
        assert record["request_to_first_event_ms"] - sum(phases.values()) >= ANSWER * 1000
    # The third request has a second earlier turn of the user's to hand over.
    assert cli_phases(third)["claude_code_history_replay"] >= TURN * 1000
