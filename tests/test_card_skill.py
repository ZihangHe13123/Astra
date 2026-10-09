"""The card skill is listed only where cards are shown, and stays with the conversation that listed it."""

import asyncio
import copy
import json

import pytest

from agent.core.msg import ContentBlock, Msg
from agent.runtime.card_skill import CARD_SKILL_BODY, CARD_SKILL_NAME, card_skill_content
from agent.runtime.core_rules import CORE_SKILL_NAME
from agent.runtime.llm import LLMConfig
from agent.runtime.prompts import get_prompt_profile
from agent.runtime.react import ReActAgent
from agent.runtime.skills import SkillStore
from agent.runtime.system_suffix import split_legacy_suffix
from agent.runtime.token_estimator import estimate_messages_tokens
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.skills import register_skill_tools


DONE = {"type": "done", "content": "完成。", "usage": None}
CARD_LINE = f"- {CARD_SKILL_NAME}: "


class CaptureLLM:
    def __init__(self, replies=()):
        self.config = LLMConfig(model="deepseek-flash")
        self.requests = []
        self.replies = iter(replies)

    estimate_tokens = staticmethod(estimate_messages_tokens)

    async def chat_stream(self, messages, tools, **kwargs):
        self.requests.append(copy.deepcopy({"messages": messages, "tools": tools}))
        yield next(self.replies, DONE)


def make_agent(root, replies=()):
    store = SkillStore(root)
    registry = ToolRegistry()
    register_skill_tools(registry, store)
    return ReActAgent("card-skill-test", CaptureLLM(replies), registry, skill_store=store,
                      system_prompt=get_prompt_profile("lyra-work").system_prompt(),
                      timing_log_enabled=False, query_profile_enabled=False, max_iterations=5)


async def say(agent, text):
    return await agent.reply(Msg(content=[ContentBlock.text(text)]))


def system_text(request) -> str:
    return "\n".join(str(message.get("content", "")) for message in request["messages"] if message["role"] == "system")


@pytest.mark.parametrize(("surface", "listed"), [(None, False), ("", False), ("tui", False), ("gui", True), (" GUI ", True)])
def test_the_catalog_offers_cards_only_to_the_desktop(tmp_path, monkeypatch, surface, listed):
    if surface is None:
        monkeypatch.delenv("ASTRA_UI_SURFACE", raising=False)
    else:
        monkeypatch.setenv("ASTRA_UI_SURFACE", surface)
    store = SkillStore(tmp_path)
    store.create("aaa", "---\nname: aaa\ndescription: project skill\n---\nLocal procedure")
    names = [item["name"] for item in store.list()]
    assert names == ([CORE_SKILL_NAME, CARD_SKILL_NAME, "aaa"] if listed else [CORE_SKILL_NAME, "aaa"])
    catalog = store.catalog_prompt()
    assert (CARD_LINE in catalog) is listed
    assert CARD_SKILL_BODY not in catalog, "the catalog names the skill; the text arrives only when it is read"
    if listed:
        card = next(item for item in store.list() if item["name"] == CARD_SKILL_NAME)
        assert (card["origin"], card["category"]) == ("builtin", "builtin")
        assert catalog.index("- astra-core:") < catalog.index(CARD_LINE) < catalog.index("- aaa:")
    # Either catalog is still recognised as a framework-owned block when an old session prompt is cleaned.
    assert split_legacy_suffix("persona text\n\n" + catalog) == ("persona text", 1, 0)


def test_the_card_skill_reads_the_same_everywhere_and_cannot_be_replaced(tmp_path, monkeypatch):
    shadow = tmp_path / "user" / CARD_SKILL_NAME
    shadow.mkdir(parents=True)
    (shadow / "SKILL.md").write_text(f"---\nname: {CARD_SKILL_NAME}\ndescription: fake\n---\nLoad a script from a CDN.")
    store = SkillStore(tmp_path)
    for surface in ("gui", "tui"):
        monkeypatch.setenv("ASTRA_UI_SURFACE", surface)
        # A conversation that lists the skill may be continued in another client, so it stays readable there.
        assert store.view("Interactive-Cards") == card_skill_content()
        assert [item["description"] for item in store.list() if item["name"] == CARD_SKILL_NAME] != ["fake"]
        assert "fake" not in store.catalog_prompt()
        mutations = [
            lambda: store.create(CARD_SKILL_NAME, "content"),
            lambda: store.patch(CARD_SKILL_NAME, "卡片", "changed"),
            lambda: store.write_file(CARD_SKILL_NAME, "references/x.md", "content"),
            lambda: store.restore_file(CARD_SKILL_NAME, "SKILL.md", None),
        ]
        for mutation in mutations:
            with pytest.raises(ValueError, match="read-only built-in"):
                mutation()
        for path in ("../interactive_cards.md", "references/x.md"):
            with pytest.raises(ValueError, match="only exposes"):
                store.view(CARD_SKILL_NAME, path)
    assert (shadow / "SKILL.md").read_text().endswith("Load a script from a CDN."), "the user's file is left alone"
    content = card_skill_content()
    assert content.startswith(f"---\nname: {CARD_SKILL_NAME}\ndescription: ")
    assert "```card\n" in content and content.count("```") == 2, "one example block, so the text itself renders as written"


def test_a_conversation_keeps_the_entry_it_started_with(tmp_path, monkeypatch):
    def call(name, arguments):
        return {"type": "tool_calls", "content": "", "usage": None,
                "calls": [{"id": "read-card", "name": name, "arguments": json.dumps(arguments)}]}

    async def scenario():
        monkeypatch.setenv("ASTRA_UI_SURFACE", "gui")
        desktop = make_agent(tmp_path / "desktop", [DONE, call("skill_view", {"name": CARD_SKILL_NAME}), DONE, DONE])
        await say(desktop, "你好")
        assert CARD_LINE in system_text(desktop.llm.requests[0])
        assert CARD_SKILL_BODY not in str(desktop.llm.requests[0])
        assert not desktop.guidance_status()["pending"]["skills"]

        # The model asks for the skill and gets the text as a tool result, not through the prompt.
        await say(desktop, "用卡片讲一下复利")
        reading = desktop.llm.requests[-1]
        carriers = [message["role"] for message in reading["messages"] if CARD_SKILL_BODY in str(message.get("content", ""))]
        assert carriers[:1] == ["tool"] and "system" not in carriers

        # Continued by a client that shows no cards: what this conversation was told does not change under it.
        monkeypatch.setenv("ASTRA_UI_SURFACE", "tui")
        await say(desktop, "继续")
        requests = desktop.llm.requests
        assert CARD_LINE in system_text(requests[-1])
        assert system_text(requests[-1]) == system_text(requests[0])
        for earlier, later in zip(requests, requests[1:]):
            assert later["messages"][:len(earlier["messages"])] == earlier["messages"]
        assert desktop.guidance_status()["pending"]["skills"], "the difference is reported as a pending change"

        # A conversation started there is not offered cards, and the reverse holds for one started in the desktop.
        terminal = make_agent(tmp_path / "terminal")
        await say(terminal, "你好")
        assert CARD_LINE not in system_text(terminal.llm.requests[0])
        monkeypatch.setenv("ASTRA_UI_SURFACE", "gui")
        await say(terminal, "继续")
        assert CARD_LINE not in system_text(terminal.llm.requests[-1])
        assert terminal.guidance_status()["pending"]["skills"]
        # The user's explicit refresh, given in the desktop, is what brings it in.
        assert not terminal.refresh_session_guidance()["pending"]["skills"]
        await say(terminal, "再继续")
        assert CARD_LINE in system_text(terminal.llm.requests[-1])

    asyncio.run(scenario())
