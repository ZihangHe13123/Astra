import asyncio
import threading

import pytest

from agent.cli.model_catalog import CatalogEntry, ModelCatalog
from agent.cli.models import ModelProfile
from agent.runtime.conclave import core
from agent.runtime.conclave import config as config_module
from agent.runtime.conclave import llm as llm_module
from agent.runtime.conclave.llm import chairperson_chat, resolve_chairperson_llm
from agent.runtime.llm import LLMConfig, OpenAICompatibleProvider
from agent.runtime.tools import conclave as conclave_tool_module
from agent.runtime.tools.conclave import (
    _parse_search_results,
    _persist_discussion_transcript,
    register_conclave_tools,
)
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_conclave_uses_configured_experts_source_limit_and_parallel_threads(monkeypatch):
    calls = []
    # Every round must reach all three worker threads before any can finish.
    # Wall-clock duration also includes unrelated scheduling delays on CI.
    search_barrier = threading.Barrier(3, timeout=5)

    def fake_search(name, engine, question, limit):
        calls.append((name, engine, question, limit))
        search_barrier.wait()
        return [{
            "title": f"{name}-{index}",
            "url": f"https://example.com/{index}",
            "snippet": "evidence",
        } for index in range(limit)]

    monkeypatch.setattr(core, "expert_search", fake_search)
    result = run(core.Conclave().run(
        "compare implementations",
        experts="GitHub专家, StackOverflow 专家, Reddit 专家",
        max_sources_per_expert=2,
        cross_discussion=False,
    ))

    assert result.experts_selected == [
        "GitHub 专家",
        "StackOverflow 专家",
        "Reddit 专家",
    ]
    assert all(call[3] == 2 for call in calls)
    assert all(len(items) == 2 for items in result.findings.values())
    assert result.synthesis
    assert len(calls) == 6
    assert not search_barrier.broken


def test_conclave_rejects_unknown_custom_expert():
    with pytest.raises(ValueError, match="Unknown Conclave expert"):
        core.resolve_experts("general", "不存在专家")


def test_conclave_skips_discussion_but_still_calls_chairperson_for_synthesis(monkeypatch):
    monkeypatch.setattr(
        core,
        "expert_search",
        lambda name, engine, question, limit: [{
            "title": name,
            "url": "https://example.com",
            "snippet": "evidence",
        }],
    )
    prompts = []

    async def fake_llm(messages):
        prompts.append(messages)
        return "synthesized"

    result = run(core.Conclave().run(
        "research",
        llm_chat=fake_llm,
        experts="学术专家",
        cross_discussion=False,
    ))
    assert result.synthesis == "synthesized"
    assert len(prompts) == 2
    assert "独立专家报告" in prompts[0][0]["content"]
    assert "最终报告" in prompts[1][0]["content"]


def test_chairperson_chat_uses_active_llm_and_configured_token_limit():
    class FakeLlm:
        def __init__(self):
            self.config = type("Config", (), {"model": "active-model"})()
            self.calls = []

        async def chat_limited(self, messages, *, max_tokens, temperature):
            self.calls.append((messages, max_tokens, temperature))
            return {"content": "answer"}

    llm = FakeLlm()
    callback = chairperson_chat(llm, "active", 1024)
    assert run(callback([{"role": "user", "content": "question"}])) == "answer"
    assert llm.calls[0][1] == 1024


def test_chairperson_resolves_canonical_model_catalog_key(monkeypatch):
    profile = ModelProfile(
        base_url="http://127.0.0.1:9999/v1",
        context_limit=32_000,
        provider="openai-compatible",
        api_key_env="",
        model_id="deepseek-v4-flash",
        catalog_provider="research",
    )
    entry = CatalogEntry(
        key="research::deepseek-v4-flash",
        model_id="deepseek-v4-flash",
        provider_id="research",
        provider_label="Research",
        base_url=profile.base_url,
        profile=profile,
    )
    monkeypatch.setattr(llm_module, "model_profiles", lambda: {})
    monkeypatch.setattr(
        "agent.cli.model_catalog.configured_model_catalog",
        lambda: ModelCatalog((entry,), {}),
    )

    resolved = resolve_chairperson_llm(object(), entry.key)

    assert resolved.config.model == "deepseek-v4-flash"
    assert resolved.config.base_url == profile.base_url
    assert resolved.config.reasoning_effort == "low"


def test_chairperson_invalid_config_falls_back_to_active(monkeypatch):
    active = object()
    monkeypatch.setattr(llm_module, "model_profiles", lambda: {})
    monkeypatch.setattr(
        "agent.cli.model_catalog.configured_model_catalog",
        lambda: ModelCatalog((), {}),
    )

    assert resolve_chairperson_llm(
        active,
        "removed::model",
        fallback_to_active=True,
    ) is active


def test_chairperson_request_failure_retries_inside_same_call(monkeypatch):
    class FailingLlm:
        async def chat_limited(self, messages, **kwargs):
            raise RuntimeError("secondary unavailable")

    class ActiveLlm:
        def __init__(self):
            self.calls = 0

        async def chat_limited(self, messages, **kwargs):
            self.calls += 1
            return {"content": "active fallback"}

    active = ActiveLlm()
    monkeypatch.setattr(
        llm_module,
        "resolve_chairperson_llm",
        lambda *args, **kwargs: FailingLlm(),
    )

    callback = chairperson_chat(active, "secondary", 1024)

    assert run(callback([{"role": "user", "content": "question"}])) == "active fallback"
    assert active.calls == 1


def test_chairperson_empty_visible_answer_retries_with_active_model(monkeypatch):
    class EmptyReasoningLlm:
        config = type("Config", (), {"model": "deepseek-v4-flash"})()

        async def chat_limited(self, messages, **kwargs):
            return {
                "content": "",
                "reasoning_content": "reasoning used the whole budget",
                "finish_reason": "length",
                "usage": {"completion_tokens": 2048},
            }

    class ActiveLlm:
        config = type("Config", (), {"model": "active-model"})()

        async def chat_limited(self, messages, **kwargs):
            return {"content": "# 主席总结陈词\n\nclean conclusion"}

    monkeypatch.setattr(
        llm_module,
        "resolve_chairperson_llm",
        lambda *args, **kwargs: EmptyReasoningLlm(),
    )

    callback = chairperson_chat(ActiveLlm(), "deepseek::deepseek-v4-flash", 2048)

    assert "clean conclusion" in run(callback([{"role": "user", "content": "question"}]))


def test_chat_limited_preserves_deepseek_reasoning_answer_budget():
    provider = object.__new__(OpenAICompatibleProvider)
    provider.config = LLMConfig(
        model="deepseek-v4-flash",
        reasoning_effort="max",
        capabilities=frozenset({"reasoning"}),
    )
    captured = []

    async def fake_completion(kwargs):
        captured.append(kwargs)
        return {"content": "answer"}

    provider._chat_completion = fake_completion

    result = run(provider.chat_limited(
        [{"role": "user", "content": "synthesize"}],
        max_tokens=2048,
    ))

    assert result == {"content": "answer"}
    assert captured[0]["max_tokens"] == 384_000


def test_synthesis_prompt_deduplicates_sources_and_requires_chair_summary():
    findings = {
        "学术专家": [{
            "title": "Shared source",
            "url": "https://example.com/shared",
            "snippet": "important academic evidence",
        }],
        "GitHub 专家": [{
            "title": "Shared source duplicate",
            "url": "https://example.com/shared",
            "snippet": "duplicate evidence",
        }],
    }

    prompt = core.Conclave()._build_synthesis_prompt(
        "research question",
        "academic",
        findings,
        "(交叉讨论已关闭)",
    )

    assert prompt.count("https://example.com/shared") == 1
    assert "important academic evidence" in prompt
    assert "duplicate evidence" not in prompt
    assert "# 主席总结陈词" in prompt
    assert "不要按专家逐组复述来源" in prompt
    assert "1800 个中文字符以内" in prompt
    assert "URL 必须逐字复制" in prompt
    assert core.unique_source_count(findings) == 1


def test_parse_search_results_preserves_sources_and_snippets():
    output = """Search: topic
Provider: Exa

[1] First result
    URL: https://example.com/one
    Snippet: first evidence

[2] Second result
    URL: https://example.com/two
    Page content: second evidence
"""

    assert _parse_search_results(output) == [
        {
            "title": "First result",
            "url": "https://example.com/one",
            "snippet": "first evidence",
        },
        {
            "title": "Second result",
            "url": "https://example.com/two",
            "snippet": "second evidence",
        },
    ]


def test_conclave_tool_reuses_runtime_search_and_has_research_timeout(monkeypatch):
    registry = ToolRegistry()

    async def fake_search(query: str, **kwargs):
        return """[1] Runtime provider result
    URL: https://example.com/runtime
    Snippet: provider evidence
"""

    registry.register(ToolDef(
        name="search_web",
        description="fake search",
        parameters={"type": "object", "properties": {}},
        fn=fake_search,
        risk="network",
        approval="never",
    ))

    class ActiveLlm:
        config = type("Config", (), {"model": "active-model"})()

        async def chat_limited(self, messages, **kwargs):
            assert "https://example.com/runtime" in messages[0]["content"]
            return {"content": "synthesized from runtime provider"}

    monkeypatch.setattr(
        config_module.ConclaveConfig,
        "load",
        classmethod(lambda cls: cls(
            chairperson_model="active",
            experts="GitHub专家",
            max_sources_per_expert=2,
            cross_discussion=False,
        )),
    )
    register_conclave_tools(registry, llm_getter=ActiveLlm)
    tool = registry.get("conclave")
    progress = []

    result = run(registry.execute(
        "conclave",
        {"question": "compare agent runtimes"},
        on_progress=progress.append,
    ))

    assert result["error"] == ""
    assert "synthesized from runtime provider" in result["output"]
    assert "1 条来源" in result["output"]
    assert tool.timeout == 600.0
    assert tool.approval == "never"
    assert any(item["stage"] == "sources_collected" for item in progress)


def _conclave_with_search(monkeypatch, search):
    registry = ToolRegistry()
    registry.register(ToolDef(
        name="search_web",
        description="fake search",
        parameters={"type": "object", "properties": {}},
        fn=search,
        risk="network",
        approval="never",
    ))

    class ActiveLlm:
        config = type("Config", (), {"model": "active-model"})()

        async def chat_limited(self, messages, **kwargs):
            return {"content": "# 主席总结陈词\n\nwritten by the chairperson"}

    monkeypatch.setattr(
        config_module.ConclaveConfig,
        "load",
        classmethod(lambda cls: cls(chairperson_model="active", experts="GitHub专家", cross_discussion=False)),
    )
    register_conclave_tools(registry, llm_getter=ActiveLlm)
    return registry


def test_conclave_tool_fails_when_every_search_failed(monkeypatch):
    async def broken_search(query: str, **kwargs):
        raise RuntimeError("search backend is down")

    registry = _conclave_with_search(monkeypatch, broken_search)

    result = run(registry.execute("conclave", {"question": "compare agent runtimes"}))

    # The chairperson still wrote something, but from no evidence: that is not a result.
    assert result["output"] == ""
    assert result["code"] == "conclave_search_failed"
    assert "searches failed" in result["error"]
    assert "search backend is down" in result["error"]
    assert "search_web" in result["recovery_hint"]


def test_conclave_tool_counts_failed_searches_next_to_the_sources(monkeypatch):
    calls = []

    async def flaky_search(query: str, **kwargs):
        calls.append(query)
        if len(calls) == 1:
            raise RuntimeError("rate limited")
        return "[1] Runtime provider result\n    URL: https://example.com/runtime\n    Snippet: provider evidence\n"

    registry = _conclave_with_search(monkeypatch, flaky_search)

    result = run(registry.execute("conclave", {"question": "compare agent runtimes"}))

    assert result["error"] == ""
    assert "1 条来源" in result["output"]
    assert "次检索（其中 1 次失败）" in result["output"]


def test_conclave_tool_discussion_argument_overrides_saved_default(monkeypatch):
    registry = ToolRegistry()
    observed = []

    monkeypatch.setattr(
        config_module.ConclaveConfig,
        "load",
        classmethod(lambda cls: cls(
            chairperson_model="active",
            experts="auto",
            cross_discussion=False,
        )),
    )

    async def fake_run(self, *, question, cross_discussion, **kwargs):
        observed.append(cross_discussion)
        return core.ConclaveResult(
            question=question,
            intent="general",
            experts_selected=["Google 专家"],
            findings={
                "Google 专家": [{
                    "title": "Evidence",
                    "url": "https://example.com/evidence",
                    "snippet": "evidence",
                }],
            },
            synthesis="# 主席总结陈词\n\ncomplete",
            duration_ms=10,
        )

    monkeypatch.setattr(core.Conclave, "run", fake_run)
    register_conclave_tools(registry)
    tool = registry.get("conclave")

    enabled = run(registry.execute(
        "conclave",
        {"question": "compare evidence", "discussion": True},
    ))
    inherited = run(registry.execute(
        "conclave",
        {"question": "ordinary research"},
    ))

    assert observed == [True, False]
    assert "含专家交叉讨论" in enabled["output"]
    assert "专家报告后主席总结" in inherited["output"]
    assert tool.parameters["required"] == ["question"]
    assert tool.parameters["properties"]["discussion"]["type"] == "boolean"


def test_discussion_transcript_persists_complete_reports_and_turns(tmp_path):
    result = core.ConclaveResult(
        question="Which design wins?",
        intent="comparison",
        experts_selected=["专家甲", "专家乙"],
        expert_reports={"专家甲": "REPORT-A", "专家乙": "REPORT-B"},
        discussion_contributions={
            "专家甲": "A challenges B",
            "专家乙": "B responds to A",
        },
        cross_discussion="## 专家甲\nA challenges B\n\n## 专家乙\nB responds to A",
        synthesis="# 主席总结陈词\n\nVERDICT",
        duration_ms=1234,
    )

    path = _persist_discussion_transcript(result, tmp_path)
    transcript = path.read_text(encoding="utf-8")

    assert path.parent == tmp_path.resolve()
    assert path.name.startswith("conclave-")
    assert "Which design wins?" in transcript
    assert "REPORT-A" in transcript and "REPORT-B" in transcript
    assert "A challenges B" in transcript
    assert "B responds to A" in transcript
    assert "VERDICT" in transcript


def test_conclave_tool_returns_discussion_transcript_path(monkeypatch, tmp_path):
    registry = ToolRegistry()
    monkeypatch.setattr(
        config_module.ConclaveConfig,
        "load",
        classmethod(lambda cls: cls(cross_discussion=True)),
    )

    async def fake_run(self, *, question, **kwargs):
        del kwargs
        return core.ConclaveResult(
            question=question,
            intent="general",
            experts_selected=["专家甲"],
            expert_reports={"专家甲": "REPORT"},
            discussion_contributions={"专家甲": "FULL DISCUSSION"},
            cross_discussion="## 专家甲\nFULL DISCUSSION",
            synthesis="# 主席总结陈词\n\nFINAL",
            duration_ms=10,
        )

    monkeypatch.setattr(core.Conclave, "run", fake_run)
    register_conclave_tools(registry, transcript_dir=tmp_path)

    response = run(registry.execute("conclave", {"question": "audit this"}))

    assert response["error"] == ""
    assert "讨论逐字稿：" in response["output"]
    transcript_path = next(tmp_path.glob("conclave-*.md"))
    assert str(transcript_path.resolve()) in response["output"]
    assert "FULL DISCUSSION" in transcript_path.read_text(encoding="utf-8")


def test_conclave_tool_keeps_answer_when_transcript_write_fails(monkeypatch, tmp_path):
    registry = ToolRegistry()
    monkeypatch.setattr(
        config_module.ConclaveConfig,
        "load",
        classmethod(lambda cls: cls(cross_discussion=True)),
    )

    async def fake_run(self, *, question, **kwargs):
        del kwargs
        return core.ConclaveResult(
            question=question,
            intent="general",
            experts_selected=["专家甲"],
            discussion_contributions={"专家甲": "FULL DISCUSSION"},
            cross_discussion="## 专家甲\nFULL DISCUSSION",
            synthesis="# 主席总结陈词\n\nFINAL",
            duration_ms=10,
        )

    def fail_persistence(result, directory=None):
        del result, directory
        raise OSError("disk unavailable")

    monkeypatch.setattr(core.Conclave, "run", fake_run)
    monkeypatch.setattr(
        conclave_tool_module,
        "_persist_discussion_transcript",
        fail_persistence,
    )
    register_conclave_tools(registry, transcript_dir=tmp_path)

    response = run(registry.execute("conclave", {"question": "audit this"}))

    assert response["error"] == ""
    assert "FINAL" in response["output"]
    assert "讨论逐字稿：" not in response["output"]


def test_experts_run_two_distinct_searches_deduplicate_and_report_once():
    search_queries = []
    expert_prompts = []
    chair_prompts = []

    async def search(expert, query, limit):
        search_queries.append(query)
        suffix = "shared" if len(search_queries) == 1 else "follow-up"
        return [{
            "title": suffix,
            "url": f"https://example.com/{suffix}",
            "snippet": f"{query} evidence",
        }, {
            "title": "duplicate",
            "url": "https://example.com/shared",
            "snippet": "duplicate evidence",
        }]

    async def expert_chat(messages):
        expert_prompts.append(messages[0]["content"])
        return "独立报告结论"

    async def chair_chat(messages):
        chair_prompts.append(messages[0]["content"])
        return "主席综合"

    result = run(core.Conclave().run(
        "compare runtimes",
        llm_chat=chair_chat,
        expert_llm_chat=expert_chat,
        experts="GitHub专家",
        cross_discussion=False,
        search=search,
        max_search_rounds=2,
    ))

    assert len(search_queries) == 2
    assert search_queries[0] != search_queries[1]
    assert len(expert_prompts) == 1
    assert len(result.findings["GitHub 专家"]) == 2
    assert "可核验来源" in result.expert_reports["GitHub 专家"]
    assert len(chair_prompts) == 1
    assert "独立报告结论" in chair_prompts[0]
    assert "去重后的来源证据" not in chair_prompts[0]
    assert result.stats["search_calls"] == 2
    assert result.stats["expert_llm_calls"] == 1


def test_discussion_is_sequential_and_each_expert_sees_reports_and_prior_turns():
    discussion_prompts = []
    chair_prompts = []

    async def search(expert, query, limit):
        name = expert[0]
        return [{
            "title": name,
            "url": f"https://example.com/{name}",
            "snippet": query,
        }]

    async def expert_chat(messages):
        prompt = messages[0]["content"]
        if "写一份可供主席直接使用的独立专家报告" in prompt:
            return "REPORT-GITHUB" if "GitHub 专家" in prompt else "REPORT-REDDIT"
        discussion_prompts.append(prompt)
        if len(discussion_prompts) == 1:
            return "FIRST-CONTRIBUTION"
        return "SECOND-CONTRIBUTION"

    async def chair_chat(messages):
        chair_prompts.append(messages[0]["content"])
        return "FINAL"

    result = run(core.Conclave().run(
        "compare community evidence",
        llm_chat=chair_chat,
        expert_llm_chat=expert_chat,
        experts="GitHub专家,Reddit专家",
        cross_discussion=True,
        search=search,
    ))

    assert len(discussion_prompts) == 2
    assert all("REPORT-GITHUB" in prompt and "REPORT-REDDIT" in prompt for prompt in discussion_prompts)
    assert "FIRST-CONTRIBUTION" not in discussion_prompts[0]
    assert "FIRST-CONTRIBUTION" in discussion_prompts[1]
    assert list(result.discussion_contributions) == ["GitHub 专家", "Reddit 专家"]
    assert "FIRST-CONTRIBUTION" in chair_prompts[0]
    assert "SECOND-CONTRIBUTION" in chair_prompts[0]
    assert result.stats["discussion_llm_calls"] == 2


def test_expert_failures_degrade_without_blocking_chairperson():
    async def failing_search(expert, query, limit):
        raise RuntimeError("provider down")

    async def failing_expert(messages):
        raise RuntimeError("expert unavailable")

    chair_prompts = []

    async def chair_chat(messages):
        chair_prompts.append(messages[0]["content"])
        return "chair still completes"

    result = run(core.Conclave().run(
        "research failure handling",
        llm_chat=chair_chat,
        expert_llm_chat=failing_expert,
        experts="学术专家",
        cross_discussion=False,
        search=failing_search,
    ))

    assert result.synthesis == "chair still completes"
    assert "降级报告" in result.expert_reports["学术专家"]
    assert "没有检索到可引用来源" in chair_prompts[0]
    assert result.stats["search_calls"] == 2
    assert result.stats["search_failures"] == 2


def test_chairperson_failure_returns_complete_expert_reports():
    async def search(expert, query, limit):
        return [{
            "title": expert[0],
            "url": f"https://example.com/{expert[1]}",
            "snippet": query,
        }]

    async def expert_chat(messages):
        return "完整专家报告 https://example.com/github"

    async def failing_chair(messages):
        raise RuntimeError("chair unavailable")

    result = run(core.Conclave().run(
        "research despite chair failure",
        llm_chat=failing_chair,
        expert_llm_chat=expert_chat,
        experts="GitHub专家",
        cross_discussion=False,
        search=search,
    ))

    assert "主席总结陈词（降级）" in result.synthesis
    assert "完整专家报告" in result.synthesis
    assert result.stats["chairperson_failures"] == 1


def test_multiple_search_rounds_respect_per_expert_source_limit():
    async def search(expert, query, limit):
        marker = "first" if "primary sources" not in query else "second"
        return [
            {
                "title": f"{marker}-{index}",
                "url": f"https://example.com/{marker}-{index}",
                "snippet": query,
            }
            for index in range(limit)
        ]

    result = run(core.Conclave().run(
        "bounded evidence",
        experts="GitHub专家",
        cross_discussion=False,
        search=search,
        max_sources_per_expert=3,
    ))

    assert len(result.findings["GitHub 专家"]) == 3


def test_discussion_failure_is_recorded_and_later_experts_continue():
    discussion_calls = 0

    async def search(expert, query, limit):
        return [{
            "title": expert[0],
            "url": f"https://example.com/{expert[1]}",
            "snippet": query,
        }]

    async def expert_chat(messages):
        nonlocal discussion_calls
        prompt = messages[0]["content"]
        if "写一份可供主席直接使用的独立专家报告" in prompt:
            return f"REPORT {prompt.splitlines()[0]}"
        discussion_calls += 1
        if discussion_calls == 1:
            raise RuntimeError("first expert unavailable")
        assert "交叉讨论失败" in prompt
        return "second expert continued"

    result = run(core.Conclave().run(
        "failure tolerant debate",
        llm_chat=lambda messages: asyncio.sleep(0, result="FINAL"),
        expert_llm_chat=expert_chat,
        experts="GitHub专家,Reddit专家",
        cross_discussion=True,
        search=search,
    ))

    assert discussion_calls == 2
    assert "交叉讨论失败" in result.discussion_contributions["GitHub 专家"]
    assert result.discussion_contributions["Reddit 专家"] == "second expert continued"
    assert result.synthesis == "FINAL"


def test_conclave_config_migrates_legacy_hermes_location(tmp_path, monkeypatch):
    legacy = tmp_path / "hermes" / "conclave.json"
    target_dir = tmp_path / ".astra"
    target = target_dir / "conclave.json"
    legacy.parent.mkdir()
    legacy.write_text(
        '{"chairperson_model":"active","experts":"GitHub专家"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "CONFIG_DIR", target_dir)
    monkeypatch.setattr(config_module, "CONFIG_PATH", target)
    monkeypatch.setattr(config_module, "LEGACY_CONFIG_PATH", legacy)

    config = config_module.ConclaveConfig.load()

    assert config.experts == "GitHub专家"
    assert target.exists()
