"""Model usage: every request is accounted by day, model, source and conversation; prices are the person's;
the daily budget is said once and holds back scheduled checks until the next day."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from agent.cli.usage_commands import execute_usage_command, main as usage_main, over_budget_message
from agent.runtime.jobs.store import BUDGET_NOTICE, JobStore
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.usage import (
    UsageLedger,
    UsagePreferences,
    budget_exceeded,
    install_usage_ledger,
    usage_context,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("ASTRA_USAGE_DB", str(tmp_path / "home" / "usage.sqlite3"))
    monkeypatch.setenv("AGENT_TIMEZONE", "Asia/Singapore")
    yield tmp_path
    install_usage_ledger(None)


def settings(tmp_path: Path, **values) -> None:
    (tmp_path / "settings.json").write_text(json.dumps(values))


class Provider:
    """Answers each request with fixed usage, streamed or not."""

    async def chat_stream(self, messages, tools):
        yield {"type": "chunk", "content": "hi"}
        yield {"type": "done", "content": "hi", "usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                                                         "prompt_cache_hit_tokens": 800}}

    async def chat(self, messages, tools):
        return {"content": "ok", "usage": {"prompt_tokens": 300, "completion_tokens": 50}}


def ledger(tmp_path: Path) -> UsageLedger:
    return UsageLedger(preferences=UsagePreferences(tmp_path / "settings.json"))


def test_every_answered_request_is_accounted_with_its_source_and_conversation(home):
    install_usage_ledger(ledger(home))
    client = LLMClient(LLMConfig(provider="openai", model="deepseek-chat", api_key="k"), provider=Provider())

    async def talk() -> None:
        with usage_context(source="channel", session="channel_weixin_private_owner"):
            async for _ in client.chat_stream([{"role": "user", "content": "hi"}]):
                pass
        await client.chat([{"role": "user", "content": "summarise"}])

    asyncio.run(talk())
    day = ledger(home).day()
    assert day["tokens"] == 1550 and day["sources"] == {"channel": 1200, "chat": 350}
    assert day["models"] == [{"model": "deepseek-chat", "prompt": 1300, "completion": 250, "cached": 800,
                              "requests": 2, "cost": None}]
    assert day["cost"] is None and day["unpriced"] == ["deepseek-chat"]
    assert ledger(home).session("channel_weixin_private_owner") == {"prompt": 1000, "completion": 200, "requests": 1}
    # Without an installed ledger nothing is written.
    install_usage_ledger(None)
    asyncio.run(client.chat([{"role": "user", "content": "x"}]))
    assert ledger(home).day()["tokens"] == 1550


def test_cost_follows_the_persons_prices_and_cached_input_is_cheaper(home):
    settings(home, model_prices={"deepseek-chat": {"input": 2, "output": 8, "cached_input": 0.5}}, price_currency="¥")
    book = ledger(home)
    book.record(provider="openai", model="deepseek-chat", usage={"prompt_tokens": 1_000_000, "completion_tokens": 100_000,
                                                                  "prompt_cache_hit_tokens": 600_000}, source="chat")
    book.record(provider="openai", model="other", usage={"prompt_tokens": 10, "completion_tokens": 10}, source="job")
    day = book.day()
    # 400k fresh at 2, 600k cached at 0.5, 100k out at 8: 0.8 + 0.3 + 0.8.
    assert day["models"][0]["cost"] == pytest.approx(1.9) and day["cost"] == pytest.approx(1.9)
    assert day["unpriced"] == ["other"]


def test_the_budget_is_said_once_a_day_whichever_process_crosses_it(home):
    settings(home, usage_budget={"daily_tokens": 1000})
    said: list[str] = []
    first, second = ledger(home), ledger(home)
    for book in (first, second):
        book.on_over_budget = lambda summary: said.append(over_budget_message(summary))
    first.record(provider="p", model="m", usage={"prompt_tokens": 600, "completion_tokens": 0})
    assert said == [] and not first.day()["over_budget"]
    first.record(provider="p", model="m", usage={"prompt_tokens": 600, "completion_tokens": 0})
    second.record(provider="p", model="m", usage={"prompt_tokens": 600, "completion_tokens": 0})
    assert len(said) == 1 and said[0].startswith("今天的模型用量 1.2k token 已超过每日预算 1.0k token")
    # Tomorrow starts fresh.
    tomorrow = time.time() + 86_400
    second.record(provider="p", model="m", usage={"prompt_tokens": 2000, "completion_tokens": 0}, at=tomorrow)
    assert len(said) == 2


def test_usage_command_reads_today_and_sets_the_budget_and_prices(home):
    book = ledger(home)
    install_usage_ledger(book)
    book.record(provider="p", model="deepseek-chat", usage={"prompt_tokens": 120_000, "completion_tokens": 8_000},
                source="chat", session="s1")
    text, error = execute_usage_command("", session="s1")
    assert error == "" and "120.0k" in text and "本会话：输入 120.0k，输出 8.0k，1 次请求" in text
    assert execute_usage_command("budget 500000")[0].startswith("每日预算设为 500.0k token")
    assert execute_usage_command("price deepseek-chat 2 8 0.5")[0].startswith("deepseek-chat 的单价")
    assert execute_usage_command("budget ¥5")[0].startswith("每日预算设为 ¥5.00")
    stored = json.loads((home / "settings.json").read_text())
    assert stored["usage_budget"] == {"daily_cost": 5.0}
    assert stored["model_prices"] == {"deepseek-chat": {"input": 2.0, "output": 8.0, "cached_input": 0.5}}
    assert "约 ¥0.30" in execute_usage_command("")[0]
    assert execute_usage_command("budget off")[0] == "已关闭每日预算。"
    assert "usage_budget" not in json.loads((home / "settings.json").read_text())
    for bad in ("budget lots", "price deepseek-chat two", "nonsense"):
        assert execute_usage_command(bad)[1], bad
    assert usage_main(["today"]) == 0 and usage_main(["budget", "-3"]) == 1


def test_over_budget_checks_wait_for_tomorrow_but_reminders_and_asked_runs_do_not(home):
    store = JobStore(home / "home")
    now = time.time()
    # The check comes due first, so the claim that returns the reminder has already passed it by.
    check = store.add(name="新闻", prompt="看看新闻", original_request="每天看新闻", workdir=home, next_at=now - 2,
                      interval_seconds=3600, model="fixture", now=now - 10)
    reminder = store.add(name="喝水", prompt="喝水", original_request="提醒我喝水", kind="reminder", workdir=home,
                         next_at=now - 1, now=now - 10)
    first = store.claim_due(now=now, over_budget=True)
    assert first is not None and first["job_id"] == reminder["id"]
    skipped = [run for run in store.history(check["id"]) if run["state"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["detail"] == BUDGET_NOTICE
    assert store.job(check["id"])["next_at"] > now
    # Skipped for the budget is not pushed, and asking for a run still runs it.
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM deliveries WHERE run_id=?", (skipped[0]["id"],)).fetchone()[0] == 0
    asked = store.claim_due(now=now, manual_job=check["id"])
    assert asked is not None and asked["state"] == "claimed"


def test_the_scheduler_only_looks_at_the_budget_when_nothing_was_accounted(home):
    assert not budget_exceeded() and not (home / "home" / "usage.sqlite3").exists()
    settings(home, usage_budget={"daily_tokens": 10})
    ledger(home).record(provider="p", model="m", usage={"prompt_tokens": 50, "completion_tokens": 0})
    assert budget_exceeded()
