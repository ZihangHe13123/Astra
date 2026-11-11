"""The text CLI's record of a turn: the agent `astra --cli` builds, its own loop, a loopback model."""

import asyncio
import builtins

import pytest

from agent.cli import main as cli
from agent.runtime.llm import LLMConfig
from agent.runtime.task_store import TaskStore
from test_persistent_jobs_worker import provider as provider_fixture

# A loopback model whose responses a test sets.
provider = provider_fixture


@pytest.mark.parametrize("blank_answers,recorded", [(1, "completed"), (2, "failed")])
def test_a_turn_fails_on_the_error_that_ends_it_and_not_on_being_asked_again(
        tmp_path, monkeypatch, provider, capsys, blank_answers, recorded):
    """A response that says nothing is discarded and the model is asked once more. The loop took the
    notice of that for a failed turn, whatever the model then answered."""
    for name, value in {
        "AGENT_SETTINGS_PATH": tmp_path / "settings.json",
        "AGENT_SESSION_DIR": tmp_path / "sessions",
        "AGENT_TASK_DB": tmp_path / "tasks.db",
        "AGENT_MEMORY_PATH": tmp_path / "memory.db",
        "AGENT_LEARNING_PATH": tmp_path / "learning.db",
        "AGENT_SKILLS_PATH": tmp_path / "skills",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("SANDBOX_DOCKER", "false")
    monkeypatch.setattr(cli, "find_chrome", lambda: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *_args: None)

    class NoServers:
        async def close(self):
            pass

    async def no_servers(*_args, **_kwargs):
        return NoServers()

    monkeypatch.setattr(cli, "initialize_mcp_tools", no_servers)
    typed = iter(["Any news?"])

    def at_the_prompt(_prompt=""):
        # One message, then the terminal closes.
        try:
            return next(typed)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", at_the_prompt)
    provider.blank_answers, provider.reply = blank_answers, "Nothing new today."
    config = LLMConfig(model="cli-fixture", api_key="local", base_url=f"http://127.0.0.1:{provider.server_port}/v1",
                       context_limit=8192, max_tokens=512, max_retries=0)

    asyncio.run(cli._async_init(config, 1, str(tmp_path)))

    [turn] = TaskStore(tmp_path / "tasks.db").list_tasks()
    assert turn["status"] == recorded
    # The response that said nothing and the one asked for after it; a failed turn is not asked a third time.
    asked = [request for request in provider.requests if request["body"].get("stream")]
    assert len(asked) == 2
    assert ("Nothing new today." in capsys.readouterr().out) is (recorded == "completed")
