"""Relative dates in a question become a calendar window instead of search words."""

from datetime import datetime, timedelta, timezone

import pytest

from agent.runtime.context_index.lexical import LexicalQuery
from agent.runtime.context_index.models import SourceResult
from agent.runtime.context_index.query import plan_query
from agent.runtime.context_index.selection import select_rows
from tests.test_context_index_native import candidate

ZONE = timezone(timedelta(hours=8))
FRIDAY = datetime(2026, 10, 16, 15, 30, tzinfo=ZONE)


def _day(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=ZONE).timestamp()


@pytest.mark.parametrize("question,phrase,start,end", [
    ("上周体重是多少？", "上周", "2026-10-05", "2026-10-12"),
    ("上个星期三开会说了什么", "上个星期三", "2026-10-07", "2026-10-08"),
    ("三天前我去做了什么？", "三天前", "2026-10-13", "2026-10-14"),
    ("大前天吃了什么", "大前天", "2026-10-13", "2026-10-14"),
    ("两周前称的体重是多少？", "两周前", "2026-09-28", "2026-10-05"),
    ("这周跑了几次", "这周", "2026-10-12", None),
    ("上个月的房租交了吗", "上个月", "2026-09-01", "2026-10-01"),
    ("二十天前的事", "二十天前", "2026-09-26", "2026-09-27"),
    ("9月27号那天下雨吗", "9月27号", "2026-09-27", "2026-09-28"),
    ("12月31号的计划", "12月31号", "2025-12-31", "2026-01-01"),
    ("How much did I weigh last week?", "last week", "2026-10-05", "2026-10-12"),
    ("What did we decide on Sep 27?", "Sep 27", "2026-09-27", "2026-09-28"),
    ("What happened 3 days ago?", "3 days ago", "2026-10-13", "2026-10-14"),
])
def test_relative_dates_become_calendar_windows_and_leave_the_search_words(question, phrase, start, end):
    plan = plan_query(question, FRIDAY)
    assert plan.intent == "temporal" and plan.include_recent
    assert (plan.time_start, plan.time_end) == (_day(start), _day(end) if end else 0.0)
    assert phrase not in plan.query and plan.original == question


def test_a_past_week_runs_until_now_and_its_words_are_not_required():
    plan = plan_query("最近一周我一共跑了多少公里？", FRIDAY)
    assert plan.time_start == (FRIDAY - timedelta(days=7)).timestamp() and not plan.time_end
    assert LexicalQuery.from_text(plan.query).terms == ("公里",)


@pytest.mark.parametrize("question", ["晚上周末去哪玩", "早上月饼还有吗", "may 5 people come?"])
def test_words_that_only_contain_a_date_are_not_windows(question):
    plan = plan_query(question, FRIDAY)
    assert plan.intent != "temporal" and not (plan.time_start or plan.time_end)


def test_a_named_topic_with_no_match_in_a_long_window_abstains_instead_of_filling_with_recent_items():
    last_week = _day("2026-10-07")
    run = candidate("run", "今天跑了 5 公里", timestamp=last_week)
    unrelated = SourceResult("available", recency=(run,))
    named = plan_query("上周体脂率是多少？", FRIDAY)
    assert select_rows(unrelated, SourceResult("absent"), named) == ()
    # A question about the period itself still gets what happened then.
    assert [row.identity for row in select_rows(unrelated, SourceResult("absent"), plan_query("上周我做了什么？", FRIDAY))] == ["run"]
    # So does a named topic that matched there.
    weight = candidate("weight", "上周称体重 71.6 公斤", timestamp=last_week)
    matched = SourceResult("available", relevance=(weight,), recency=(run,))
    assert {row.identity for row in select_rows(matched, SourceResult("absent"), named)} == {"weight", "run"}
