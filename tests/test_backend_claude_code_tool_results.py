"""Real backend + recording CLI: a Claude Code conversation's old tool results are never cleared.

The CLI reads a request again in full from the first message that changed, so a result cleared in the
middle of a conversation is paid for with everything after it. Cleared where the cache had gone anyway,
after an hour's pause, they took off about 1% of what that request read (live, 2026-11-12 to 11-13),
and the model could no longer see them. A long conversation is summarized instead.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from agent.runtime.session_store import SessionStore
from test_backend_claude_code_compaction import MODELS, RECORDING_CLAUDE, SUMMARY, TURN_SUMMARY
from test_backend_served_model import send
from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake claude executable is a shebang script")

CLEARED = "[Old tool result content cleared"
SESSION = "claude_code_tool_results"
# Four results of this size were already over the old budget for old results.
PAGE, BUDGET = 2_000, 6_000


class Conversation:
    """One stored conversation, held over several runs of a backend on the recording CLI."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.work = tmp_path / "work"
        self.work.mkdir()
        self.log = tmp_path / "requests.jsonl"
        self.cli = tmp_path / "claude"
        self.cli.write_text(RECORDING_CLAUDE % (str(self.log), SUMMARY, TURN_SUMMARY, str(self.work)), encoding="utf-8")
        self.cli.chmod(self.cli.stat().st_mode | stat.S_IXUSR)
        self.models = tmp_path / "claude-models.yaml"
        self.models.write_text(MODELS, encoding="utf-8")
        self.sessions = tmp_path / "sessions"
        self.sessions.mkdir()
        self.pages = 0
        self.runs = 0
        self.proc = None

    def start(self) -> None:
        self.runs += 1
        state = self.root / f"run-{self.runs}"
        state.mkdir()
        self.proc, _, _, self.wait_for = _start_question_protocol_backend(
            state, 0, SESSION, settings_overrides={"selected_model": "sonnet"}, workdir=self.work,
            env_overrides={
                "AGENT_MODELS_FILE": str(self.models), "ASTRA_CLAUDE_CODE_COMMAND": str(self.cli),
                # The conversation outlives a backend; everything else is a run's own.
                "AGENT_SESSION_DIR": str(self.sessions),
                "SANDBOX_WORKDIR": str(self.work), "TOOL_RESULT_DIR": str(self.root / "tool-results"),
                "ASTRA_CHANNEL_CONFIG": str(self.root / "no-channels.json"), "ASTRA_CONTEXT_INDEX_EMBEDDING": "off",
                # Stated in full: a checkout's own configuration must not decide any of them.
                "TOOL_MAX_INLINE_CHARS": "12000", "TOOL_MAX_FRESH_RESULT_CHARS": "100000",
            })
        self.wait_for(lambda event: event.get("type") == "model_info")

    def stop(self) -> None:
        if self.proc is not None:
            _stop_protocol_backend(self.proc)
            self.proc = None

    def read(self, pages: int) -> list[dict]:
        """A turn in which the model reads this many new pages; the requests the turn made."""
        for _ in range(pages):
            (self.work / f"file-{self.pages}.txt").write_text(
                "".join(f"{self.pages:02d}-{line:03d} lorem ipsum dolor sit amet\n" for line in range(PAGE // 34)),
                encoding="utf-8")
            self.pages += 1
        before = len(self.requests())
        send(self.proc, {"type": "message", "text": "Read the files that are new."})
        self.wait_for(lambda event: event.get("type") == "done", timeout=60)
        self.wait_for(lambda event: event.get("type") == "model_info")
        return self.requests()[before:]

    def requests(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def two_hours_pass(self) -> None:
        """The stored conversation as the person finds it when they come back: everything in it two hours old."""
        store = SessionStore(self.sessions / f"{SESSION}.json")
        data = store.load(readonly=True)
        stamped = [message for message in data["messages"] if "timestamp" in message]
        assert stamped, "the conversation records when its messages were added"
        for message in stamped:
            message["timestamp"] -= 2 * 3600
        store.save(data, append_from=0, snapshot=True)


def results(request: dict) -> dict[str, str]:
    """Each tool result a request carries, by the call it answers, oldest first."""
    found = {}
    for _, blocks in request["turns"]:
        for block in blocks:
            if block.get("type") == "tool_result":
                content = block.get("content")
                found[block["tool_use_id"]] = content if isinstance(content, str) else "".join(
                    str(part.get("text") or "") for part in content)
    return found


def cleared(request: dict) -> list[str]:
    return [call for call, text in results(request).items() if text.startswith(CLEARED)]


def repeats(earlier: dict, later: dict) -> bool:
    """Whether the later request begins with everything the earlier one carried, word for word."""
    return later["turns"][:len(earlier["turns"])] == earlier["turns"]


@pytest.fixture
def conversation(tmp_path):
    held = Conversation(tmp_path)
    try:
        yield held
    finally:
        held.stop()


def test_old_results_are_never_cleared_also_in_the_request_after_the_cache_has_gone(conversation):
    """Live Astra 2026-11-11: on Opus through the CLI, 13 requests cleared 28 old results, about 47k
    tokens, and read 1.52M tokens uncached to do it. The results stay as they were read."""
    conversation.start()
    busy = [conversation.read(4), conversation.read(4), conversation.read(4)]
    conversation.stop()
    # Turn after turn within the hour: the results before the latest request hold several times what
    # used to be the budget, and every request repeats the one before it.
    used = [request for turn in busy for request in turn]
    assert len(used) == 15 and not any(cleared(request) for request in used)
    assert sum(len(text) for text in results(busy[2][0]).values()) > 2 * BUDGET
    assert all(repeats(earlier, later) for earlier, later in zip(used, used[1:]))

    conversation.two_hours_pass()
    conversation.start()
    back = conversation.read(2)
    again = conversation.read(2)
    conversation.stop()
    # After the pause too: every result is there in full, the oldest as much as the newest, and the
    # conversation goes on word for word.
    later = [*back, *again]
    assert not any(cleared(request) for request in later)
    kept = results(back[0])
    assert list(kept)[:12] == [f"toolu_{number}" for number in range(12)]
    assert all(f"{number:02d}-000 lorem" in kept[f"toolu_{number}"] for number in range(12))
    assert all(repeats(earlier, later_request) for earlier, later_request in zip(later, later[1:]))
