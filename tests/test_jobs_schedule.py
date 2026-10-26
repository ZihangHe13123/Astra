"""Wall-clock schedules, quiet and paused jobs, and the model's approved jobs tool."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from agent.cli.jobs_commands import execute_jobs_command
from agent.runtime.jobs.calendar import calendar_schedule, next_slot
from agent.runtime.jobs.scheduler import tick
from agent.runtime.jobs.store import INACTIVE_SECONDS, NO_UPDATE, JobStore, note_interaction
from agent.runtime.tools.jobs import register_jobs_tool
from agent.runtime.tools.registry import ToolRegistry

SINGAPORE = ZoneInfo("Asia/Singapore")


def at(text: str, zone=SINGAPORE) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=zone).timestamp()


def daily(store: JobStore, tmp_path: Path, *, now: float, days=None, kind="reminder", **kwargs) -> dict:
    return store.add(name="morning", prompt="Good morning", original_request="Every morning at eight",
                     workdir=tmp_path, calendar={"time": "08:00", "days": days, "tz": "Asia/Singapore"},
                     grace_seconds=3600, kind=kind, model="fixture" if kind == "check" else "", now=now, **kwargs)


def test_calendar_slots_follow_local_time_across_daylight_saving():
    new_york = ZoneInfo("America/New_York")
    nine = calendar_schedule("9:00", None, "America/New_York")
    before = next_slot(nine, at("2026-03-07T10:00", new_york))
    after = next_slot(nine, before)
    # The same 09:00 on the clock is a different UTC hour on each side of the change.
    assert datetime.fromtimestamp(before, timezone.utc).hour == 13
    assert datetime.fromtimestamp(after, timezone.utc).hour == 13
    assert datetime.fromtimestamp(at("2026-03-07T09:00", new_york), timezone.utc).hour == 14
    skipped = calendar_schedule("02:30", None, "America/New_York")
    # 02:30 does not exist on 2026-03-08; it runs once, an hour later on the clock.
    slot = next_slot(skipped, at("2026-03-08T00:00", new_york))
    assert datetime.fromtimestamp(slot, new_york).strftime("%m-%d %H:%M") == "03-08 03:30"
    weekly = calendar_schedule("08:00", ["fri", "mon"], "Asia/Singapore")
    assert weekly["days"] == ["mon", "fri"]
    # 2026-10-24 is a Saturday: the next run is Monday 08:00.
    assert next_slot(weekly, at("2026-10-24T12:00")) == at("2026-10-26T08:00")
    with pytest.raises(ValueError):
        calendar_schedule("25:00")
    with pytest.raises(ValueError):
        calendar_schedule("08:00", ["someday"])


def test_daily_job_runs_at_local_time_and_catches_up_only_the_latest_day(tmp_path):
    store = JobStore(tmp_path / "home")
    job = daily(store, tmp_path, now=at("2026-10-25T09:30"))
    assert job["next_at"] == at("2026-10-26T08:00")
    assert store.claim_due(now=at("2026-10-26T07:59")) is None
    run = store.claim_due(now=at("2026-10-26T08:00"))
    assert run["scheduled_at"] == at("2026-10-26T08:00")
    assert store.job(job["id"])["next_at"] == at("2026-10-27T08:00")
    store.finish(run["id"], "completed", output="Good morning")
    # Asleep for three mornings: one record of the missed days, then today's run.
    late = store.claim_due(now=at("2026-10-30T08:20"))
    assert late["scheduled_at"] == at("2026-10-30T08:00")
    missed = next(row for row in store.history(job["id"]) if row["state"] == "skipped")
    assert missed["skipped_count"] == 3
    assert store.job(job["id"])["next_at"] == at("2026-10-31T08:00")


def test_version_one_database_gains_columns_and_keeps_its_jobs(tmp_path):
    home = tmp_path / "home"
    (home / "jobs").mkdir(parents=True)
    db = sqlite3.connect(home / "jobs" / "jobs.sqlite3")
    db.executescript("""
        CREATE TABLE jobs (id TEXT PRIMARY KEY, name TEXT NOT NULL, spec TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('enabled','paused','completed','removed')),
            next_at REAL NOT NULL, interval_seconds REAL NOT NULL, grace_seconds REAL NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE runs (id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), scheduled_at REAL,
            created_at REAL NOT NULL, finished_at REAL,
            state TEXT NOT NULL CHECK(state IN ('claimed','running','completed','failed','unknown','skipped')),
            spec TEXT NOT NULL, output TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
            skipped_count INTEGER NOT NULL DEFAULT 0, UNIQUE(job_id, scheduled_at));
        CREATE TABLE deliveries (run_id TEXT PRIMARY KEY REFERENCES runs(id),
            state TEXT NOT NULL CHECK(state IN ('pending','delivered')), delivered_at REAL);
        CREATE TABLE inbox (run_id TEXT PRIMARY KEY REFERENCES deliveries(run_id), received_at REAL NOT NULL, read_at REAL);
        PRAGMA user_version=1;
    """)
    spec = {"name": "old", "prompt": "Stretch", "original_request": "Remind me", "workdir": str(tmp_path),
            "kind": "reminder", "model": "", "web": False, "idle_seconds": 120, "timeout_seconds": 900,
            "max_iterations": 10}
    db.execute("INSERT INTO jobs VALUES ('a' || hex(randomblob(15)), 'old', ?, 'enabled', 1000, 60, 30, 900)",
               (json.dumps(spec),))
    db.commit()
    db.close()
    store = JobStore(home)
    with store.connect() as reopened:
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == 4
    [job] = store.jobs()
    assert job["active_since"] == 900 and job["paused_reason"] == ""
    run = store.claim_due(now=1000)
    assert run["job_id"] == job["id"]
    store.finish(run["id"], "completed", output="Stretch")
    assert store.run(run["id"])["usage"] == {} and store.run(run["id"])["quiet"] == 0
    # Opening again is a no-op at the current version.
    assert JobStore(home).job(job["id"])["state"] == "enabled"


def test_three_failures_in_a_row_pause_a_recurring_job_and_say_why(tmp_path):
    store = JobStore(tmp_path / "home")
    job = store.add(name="flaky", prompt="Check", original_request="Check hourly", workdir=tmp_path,
                    interval_seconds=3600, next_at=1000, model="fixture", now=900)
    once = store.add(name="once", prompt="Check", original_request="Check once", workdir=tmp_path,
                     next_at=1000, model="fixture", now=900)
    outcomes = ["failed", "completed", "failed", "unknown", "failed"]
    for index, outcome in enumerate(outcomes):
        run = store.claim_due(now=1000 + index * 3600, manual_job="")
        if run["job_id"] == once["id"]:
            store.finish(run["id"], "failed", detail="boom")
            run = store.claim_due(now=1000 + index * 3600)
        store.finish(run["id"], outcome, detail="provider_error")
        state = store.job(job["id"])["state"]
        assert state == ("paused" if index == len(outcomes) - 1 else "enabled")
    assert store.job(job["id"])["paused_reason"] == "failures"
    assert store.history(job["id"])[0]["detail"].startswith("Paused after 3 failed runs in a row")
    # A one-shot that failed is simply finished, not paused.
    assert store.job(once["id"])["state"] == "completed"


def test_a_week_without_talking_pauses_recurring_jobs_until_resumed(tmp_path):
    home = tmp_path / "home"
    store = JobStore(home)
    start = at("2026-10-01T09:00")
    job = daily(store, tmp_path, now=start)
    note_interaction(home, now=start)
    reminder = store.add(name="later", prompt="Call back", original_request="Remind me next month", kind="reminder",
                         workdir=tmp_path, next_at=start + 30 * 86400, now=start)
    now = start + INACTIVE_SECONDS + 3600
    assert tick(store, now=now)["executed"] == []
    paused = store.job(job["id"])
    assert paused["state"] == "paused" and paused["paused_reason"] == "inactive"
    notice = store.inbox()[0]
    assert notice["job_id"] == job["id"] and "nobody talked to Astra for 7 days" in notice["detail"]
    # A one-shot is what the person asked for; it is not paused.
    assert store.job(reminder["id"])["state"] == "enabled"
    resumed = store.set_state(job["id"], "enabled", now=now)
    # The mornings that passed while paused were declined, not missed.
    assert resumed["next_at"] == next_slot(paused["spec"]["calendar"], now)
    assert tick(store, now=resumed["next_at"])["executed"][0]["state"] == "completed"
    assert all(row["skipped_count"] == 0 for row in store.history(job["id"]))


def test_run_request_adds_one_occurrence_without_moving_the_schedule(tmp_path):
    store = JobStore(tmp_path / "home")
    job = daily(store, tmp_path, now=at("2026-10-25T09:30"))
    store.request_run(job["id"], now=at("2026-10-25T10:00"))
    run = store.claim_due(now=at("2026-10-25T10:00"))
    assert run["scheduled_at"] is None and run["job_id"] == job["id"]
    assert store.job(job["id"])["next_at"] == at("2026-10-26T08:00")
    assert store.claim_due(now=at("2026-10-25T10:00")) is None


def test_update_changes_future_runs_and_records_the_request(tmp_path):
    store = JobStore(tmp_path / "home")
    job = daily(store, tmp_path, now=at("2026-10-25T09:30"))
    changed = store.update(job["id"], request="Only on Mondays at 7:30", now=at("2026-10-25T10:00"),
                           calendar={"time": "07:30", "days": ["mon"], "tz": "Asia/Singapore"}, notify="on_change")
    assert changed["next_at"] == at("2026-10-26T07:30")
    assert changed["spec"]["calendar"]["days"] == ["mon"] and changed["spec"]["notify"] == "on_change"
    assert changed["spec"]["original_request"].startswith("Every morning at eight")
    assert changed["spec"]["original_request"].endswith("Only on Mondays at 7:30")
    with pytest.raises(ValueError):
        store.update(job["id"], request="", name="x")


def test_a_check_gets_rounds_and_time_for_what_it_reads_and_can_be_given_more(tmp_path):
    store = JobStore(tmp_path / "home")
    now = at("2026-10-25T09:30")
    files = daily(store, tmp_path, now=now, kind="check")
    web = daily(store, tmp_path, now=now, kind="check", web=True)
    assert [(job["spec"]["max_iterations"], job["spec"]["timeout_seconds"]) for job in (files, web)] == [
        (10, 900), (30, 2700)]
    given = daily(store, tmp_path, now=now, kind="check", web=True, max_iterations=12, timeout_seconds=600)
    assert (given["spec"]["max_iterations"], given["spec"]["timeout_seconds"]) == (12, 600)
    changed = store.update(web["id"], request="晚报轮数太少了", max_iterations=40, timeout_seconds=3600, now=now)
    assert (changed["spec"]["max_iterations"], changed["spec"]["timeout_seconds"]) == (40, 3600)
    assert changed["spec"]["original_request"].endswith("晚报轮数太少了") and changed["next_at"] == web["next_at"]
    for limits in ({"max_iterations": 0}, {"max_iterations": 51}, {"timeout_seconds": 60}):
        with pytest.raises(ValueError):
            store.update(web["id"], request="x", now=now, **limits)
    reminder = daily(store, tmp_path, now=now)["id"]
    for change in ({"max_iterations": 20}, {"model": "fixture"}):
        with pytest.raises(ValueError, match="no model, rounds"):
            store.update(reminder, request="x", now=now, **change)
    assert store.update(web["id"], request="换成 DeepSeek", model="deepseek::deepseek-flash", now=now)["spec"]["model"] == (
        "deepseek::deepseek-flash")
    assert store.update(web["id"], request="跟随默认", model="", now=now)["spec"]["model"] == ""
    output, error = execute_jobs_command(["update", files["id"], "--iterations", "25", "--timeout", "1200"],
                                         home=tmp_path / "home", workdir=tmp_path)
    assert error == "" and (json.loads(output)["spec"]["max_iterations"], json.loads(output)["spec"]["timeout_seconds"]) == (
        25, 1200)
    assert execute_jobs_command(["update", files["id"]], home=tmp_path / "home", workdir=tmp_path)[1]


def test_quiet_result_is_kept_but_not_offered_as_the_last_report(tmp_path):
    store = JobStore(tmp_path / "home")
    job = daily(store, tmp_path, now=at("2026-10-25T09:30"), kind="check", notify="on_change")
    first = store.claim_due(now=at("2026-10-26T08:00"))
    store.finish(first["id"], "completed", output="Two new posts", usage={"prompt_tokens": 120})
    second = store.claim_due(now=at("2026-10-27T08:00"))
    assert store.last_reported_output(job["id"], before=second["id"]) == "Two new posts"
    store.finish(second["id"], "completed", output=NO_UPDATE, quiet=True)
    third = store.claim_due(now=at("2026-10-28T08:00"))
    assert store.last_reported_output(job["id"], before=third["id"]) == "Two new posts"
    assert store.run(first["id"])["usage"] == {"prompt_tokens": 120}
    assert store.run(second["id"])["quiet"] == 1


def test_cli_adds_a_weekday_reminder_at_a_local_time(tmp_path):
    output, error = execute_jobs_command(
        ["add", "standup", "--time", "9:15", "--days", "mon,tue,wed,thu,fri", "--tz", "Asia/Singapore",
         "--reminder", "--prompt", "Stand-up"], home=tmp_path / "home", workdir=tmp_path)
    assert error == ""
    job = json.loads(output)
    assert job["spec"]["calendar"] == {"time": "09:15", "days": ["mon", "tue", "wed", "thu", "fri"],
                                       "tz": "Asia/Singapore"}
    assert job["next_at_local"].endswith("09:15:00+08:00")
    for argv in (["add", "x", "--time", "09:00", "--every", "3600", "--reminder", "--prompt", "x"],
                 ["add", "x", "--after", "60", "--days", "mon", "--reminder", "--prompt", "x"]):
        assert execute_jobs_command(argv, home=tmp_path / "home", workdir=tmp_path)[1]


def tool(tmp_path: Path, request: str = "每天早上8点提醒我喝水"):
    registry = ToolRegistry()
    asked: list[dict] = []
    answer = {"decision": "once"}

    async def approve(prompt):
        asked.append(prompt)
        return answer["decision"]

    registry.set_approval_handler(approve)
    register_jobs_tool(registry, home=tmp_path / "home", workdir=lambda: tmp_path,
                       request=lambda: request, model_key=lambda: "fixture")
    return registry, asked, answer


def call(registry: ToolRegistry, **args) -> dict:
    return asyncio.run(registry.execute("jobs", args, call_id="jobs-call"))


def test_tool_creates_only_after_approval_and_records_the_persons_words(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TIMEZONE", "Asia/Singapore")
    registry, asked, answer = tool(tmp_path)
    answer["decision"] = "deny"
    denied = call(registry, action="add", name="喝水", kind="reminder", prompt="喝水", time="08:00")
    assert denied["error_type"] == "approval_denied"
    assert JobStore(tmp_path / "home").jobs() == []
    answer["decision"] = "once"
    created = call(registry, action="add", name="喝水", kind="reminder", prompt="喝水", time="08:00")
    assert created["error"] == ""
    card = asked[-1]
    assert card["approval_title"] == "新建定时任务：喝水"
    assert card["target"] == "每天 08:00（Asia/Singapore）；一条提醒"
    # Every change is decided on its own; there is no "allow for this session".
    assert card["choices"] == ["once", "deny"]
    [job] = JobStore(tmp_path / "home").jobs()
    # The authority is the person's message, not text the model wrote.
    assert job["spec"]["original_request"] == "每天早上8点提醒我喝水"
    assert job["spec"]["calendar"]["time"] == "08:00"


def test_tool_list_and_pause_need_no_approval_but_other_changes_do(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TIMEZONE", "Asia/Singapore")
    registry, asked, _answer = tool(tmp_path)
    created = json.loads(call(registry, action="add", name="Rust 早报", prompt="Summarise new Rust posts",
                              time="08:00", notify="on_change", web=True)["output"])
    job_id = created["job"]["job_id"]
    assert created["job"]["notify"] == "on_change" and created["job"]["web"] is True
    before = len(asked)
    listed = json.loads(call(registry, action="list")["output"])["jobs"]
    assert [item["name"] for item in listed] == ["Rust 早报"]
    assert json.loads(call(registry, action="pause", job_id=job_id)["output"])["job"]["state"] == "paused"
    assert len(asked) == before
    changed = call(registry, action="update", job_id=job_id, time="07:30", days=["mon", "wed"])
    assert changed["error"] == "" and asked[-1]["target"] == "每周一、三 07:30（Asia/Singapore）"
    for action, title in (("resume", "恢复定时任务"), ("run", "立即运行定时任务"), ("remove", "删除定时任务")):
        assert call(registry, action=action, job_id=job_id)["error"] == ""
        assert asked[-1]["approval_title"] == f"{title}：Rust 早报"
    mixed = call(registry, action="add", name="x", prompt="x", time="07:00", days=["mon"], every_minutes=5)
    assert mixed["error_type"] == "invalid_input" and len(asked) == before + 4


def test_tool_gives_a_check_more_rounds_and_time_with_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TIMEZONE", "Asia/Singapore")
    registry, asked, _answer = tool(tmp_path, request="晚报轮数太少了，多给点")
    created = json.loads(call(registry, action="add", name="AI 晚报", prompt="整理今天的 AI 新闻", time="20:00",
                              web=True)["output"])["job"]
    assert (created["max_rounds"], created["time_limit_minutes"]) == (30, 45)
    # A check made in a conversation follows the default model; the jobs page can pin one.
    assert created["model"] == "default" and JobStore(tmp_path / "home").job(created["job_id"])["spec"]["model"] == ""
    changed = call(registry, action="update", job_id=created["job_id"], max_rounds=40, time_limit_minutes=60)
    assert changed["error"] == ""
    assert asked[-1]["target"] == "每天 20:00（Asia/Singapore）；最多 40 轮、每次最多 60 分钟"
    spec = JobStore(tmp_path / "home").job(created["job_id"])["spec"]
    assert (spec["max_iterations"], spec["timeout_seconds"]) == (40, 3600)
    before = len(asked)
    reminder = json.loads(call(registry, action="add", name="喝水", kind="reminder", prompt="喝水",
                               time="08:00")["output"])["job"]
    assert "max_rounds" not in reminder
    refused = call(registry, action="update", job_id=reminder["job_id"], max_rounds=20)
    assert refused["error_type"] == "invalid_input" and len(asked) == before + 1


def test_tool_refuses_without_the_persons_request_and_checks_arguments_before_asking(tmp_path):
    registry, asked, _answer = tool(tmp_path, request="")
    missing = call(registry, action="add", name="x", kind="reminder", prompt="x", after_minutes=5)
    assert "own request" in missing["error"]
    assert JobStore(tmp_path / "home").jobs() == []
    registry, asked, _answer = tool(tmp_path)
    for args in ({"action": "add", "name": "x", "prompt": "x"},
                 {"action": "add", "name": "x", "prompt": "x", "days": ["mon"]},
                 {"action": "resume", "job_id": "nope"},
                 {"action": "add", "name": "x", "prompt": "x", "at": "2001-01-01 08:00"}):
        assert call(registry, **args)["error"]
    assert asked == []


def test_schedule_requests_route_to_the_jobs_tool():
    registry = ToolRegistry()
    register_jobs_tool(registry, home=Path("unused"), workdir=Path.cwd, request=lambda: "", model_key=lambda: "")
    for text in ("每天早上8点提醒我喝水", "remind me at 3pm", "every morning check the news", "定时任务暂停一下"):
        assert "schedule" in registry.select_groups(text)
    assert "schedule" not in registry.select_groups("帮我看看这个文件")


def test_tool_sets_or_clears_the_quiet_hours_with_approval(tmp_path, monkeypatch):
    from agent.runtime.jobs.calendar import quiet_hours

    registry, asked, answer = tool(tmp_path, request="晚上 11 点半到早上 7 点别打扰我")
    changed = call(registry, action="quiet", quiet_hours="23:30-7:00")
    assert changed["error"] == "" and json.loads(changed["output"]) == {"quiet_hours": {"start": "23:30", "end": "07:00"}}
    assert asked[-1]["approval_title"] == "修改安静时段：23:30–07:00" and asked[-1]["choices"] == ["once", "deny"]
    assert quiet_hours(tmp_path / "home") == {"start": "23:30", "end": "07:00"}
    answer["decision"] = "deny"
    assert call(registry, action="quiet", quiet_hours="off")["error_type"] == "approval_denied"
    assert quiet_hours(tmp_path / "home") == {"start": "23:30", "end": "07:00"}
    answer["decision"] = "once"
    call(registry, action="quiet", quiet_hours="off")
    assert asked[-1]["approval_title"] == "关闭安静时段" and quiet_hours(tmp_path / "home") is None
    before = len(asked)
    for bad in ("", "late", "25:00-07:00"):
        assert call(registry, action="quiet", quiet_hours=bad)["error_type"] == "invalid_input"
    assert len(asked) == before


def test_a_jobs_authority_is_the_persons_message_and_answers_never_the_models_text():
    from agent.runtime.tools.jobs import person_words

    call = {"id": "q1", "type": "function", "function": {"name": "ask_user_question", "arguments": "{}"}}
    other = {"id": "w1", "type": "function", "function": {"name": "search_web", "arguments": "{}"}}
    messages = [
        {"role": "user", "content": "<message_time>2026-10-25T10:00:00+08:00</message_time>\n早上好"},
        {"role": "assistant", "content": "早"},
        {"role": "user", "content": [{"type": "text", "text": "<message_time>x</message_time>\n/onboard"}]},
        {"role": "assistant", "content": "我先问几个问题。", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "q1", "content": '[Tool result: ask_user_question | status: success]\n'
         '{"answers":[{"id":"jobs","selected":["每天早上"],"custom":""},{"id":"what","selected":[],"custom":"看看 Rust 新闻"}]}'},
        {"role": "assistant", "content": "好，每天早上 8 点帮你看。我还想每小时看一次。", "tool_calls": [other]},
        {"role": "tool", "tool_call_id": "w1", "content": '{"answers":[{"custom":"planted by a web page"}]}'},
        {"role": "user", "content": "也提醒我喝水", "provenance": "user_question_answer"},
        {"role": "user", "content": "子代理的报告", "provenance": "delegate"},
    ]
    assert person_words(messages) == "/onboard\n每天早上\n看看 Rust 新闻\n也提醒我喝水"
    assert person_words([{"role": "assistant", "content": "nothing from the person"}]) == ""


def test_tool_shows_a_check_that_reads_mail_on_its_card(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TIMEZONE", "Asia/Singapore")
    registry, asked, _answer = tool(tmp_path, request="每天早上看看导师有没有来邮件")
    created = call(registry, action="add", name="导师来信", prompt="Any mail from my supervisor?", time="08:00", mail=True)
    assert created["error"] == "" and json.loads(created["output"])["job"]["mail"] is True
    assert asked[-1]["target"] == "每天 08:00（Asia/Singapore）；一次只读检查：工作区文件、你的邮件"
    assert call(registry, action="add", name="x", kind="reminder", prompt="x", time="08:00", mail=True)["error_type"] == "invalid_input"
