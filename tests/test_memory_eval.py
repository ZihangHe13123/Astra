"""Harness properties of the memory evaluation (agent/evals/memory), not quality thresholds."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent.evals.memory.fixture import CATEGORIES, NO_EVIDENCE_CATEGORIES, LiveSpec, TurnSpec, load_fixture
from agent.evals.memory.harness import OfflineLLM, RequestRecorder, build_agent, run_offline, run_turn
from agent.evals.memory.injection import diff_request, fresh_segments, message_text, prefix_stability
from agent.evals.memory.isolation import isolated_state, path_guard, resolved_state_locations
from agent.evals.memory.live import score_answer
from agent.evals.memory.scoring import aggregate_probes, analyze_requests, score_probe
from agent.evals.memory.world import SeededItem, SeededWorld, scenario_time, seed_world

NOW = datetime(2026, 10, 15, 12, 0).astimezone()


def _scenario(scenario_id: str):
    fixture = load_fixture()
    return next(scenario for scenario in fixture.scenarios if scenario.id == scenario_id)


def test_fixture_covers_every_category_and_every_reference_resolves():
    fixture = load_fixture()
    assert {probe.category for probe in fixture.probes} == set(CATEGORIES)
    for scenario in fixture.scenarios:
        items = scenario.item_contents()
        for probe in scenario.probes:
            refs = [ref for group in probe.expect for ref in group]
            assert all(ref.startswith("text:") or ref in items for ref in refs)
            assert bool(refs) == (probe.category not in NO_EVIDENCE_CATEGORIES)
    assert fixture.write_path and all(spec.live.must for spec in fixture.write_path)


def test_seeding_goes_through_runtime_storage_with_simulated_times(tmp_path):
    from agent.runtime.memory import MemoryStore
    from agent.runtime.session_recall import SessionRecall

    scenario = _scenario("dev_env")
    with isolated_state(tmp_path / "state") as paths:
        world = seed_world(scenario, now=NOW, workspace=paths.workspace)
        store = MemoryStore()
        assert [item["content"] for item in store.list_core("user")] == list(scenario.core_user)
        assert world.core_entries == scenario.core_memory + scenario.core_user

        old = store.record_store.get(world.items["rec:editor_old"].identity)
        new = store.record_store.get(world.items["rec:editor_new"].identity)
        assert old is not None and new is not None
        assert (old.status, new.status, new.supersedes_id) == ("superseded", "active", old.record_id)
        assert datetime.fromisoformat(new.created_at) == scenario_time(NOW, 13, "22:45").astimezone(timezone.utc)
        assert world.items["rec:editor_old"].status == "superseded"
        assert old.record_id not in {record.record_id for record in store.recall_records("VS Code")}

        recall = SessionRecall()
        try:
            recall.init_db()
            hits = recall.search("Zed", limit=10)
            switched = next(hit for hit in hits if hit["message_id"] == int(world.items["msg:s2.u1"].identity))
            assert switched["timestamp"] == f"{scenario_time(NOW, 13, '22:40'):%Y-%m-%d %H:%M}"
            assert {session["session_id"] for session in recall.browse(limit=20)} == set(world.archive_sessions.values())
        finally:
            recall.close()

        root = paths.root
        for location in (paths.memory_db, paths.session_recall_db, *resolved_state_locations().values()):
            assert Path(location).resolve().is_relative_to(root)
    # The previous environment is restored once the block ends.
    assert os.environ.get("AGENT_MEMORY_PATH", "") != str(paths.memory_db)


def test_injection_diff_separates_whole_messages_added_text_and_drops():
    canonical = [
        {"role": "system", "content": "BASE"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "OK."},
        {"role": "user", "content": "next"},
    ]
    request = [
        {"role": "system", "content": "BASE\n\nRUNTIME BLOCK"},
        {"role": "user", "content": "hi"},
        {"role": "user", "content": "SNAP-1"},
        {"role": "assistant", "content": "OK."},
        {"role": "user", "content": "[CTX] next"},
        {"role": "user", "content": [{"type": "text", "text": "SNAP-2"}]},
    ]
    diff = diff_request(request, canonical)
    assert [(item.index, item.whole, item.text) for item in diff.segments] == [
        (0, False, "\n\nRUNTIME BLOCK"), (2, True, "SNAP-1"), (4, False, "[CTX] "), (5, True, "SNAP-2"),
    ]
    assert (diff.injected_messages, diff.augmented_messages, diff.dropped) == (2, 2, 0)

    dropped = diff_request([canonical[0], canonical[2], canonical[3]], canonical)
    assert (dropped.segments, dropped.dropped) == ((), 1)
    assert fresh_segments(["A", "B", "A"], ["A", "B"]) == ["A"]
    assert prefix_stability(request[:3], request)["stable"] is True
    assert prefix_stability(request[:3], canonical)["first_divergence"] == 0


def _hand_world() -> SeededWorld:
    scenario = _scenario("dev_env")
    when = datetime(2026, 9, 20, 21, 0).astimezone().timestamp()
    world = SeededWorld(scenario=scenario, now=NOW, core_entries=("Core fact about Siyuan.",))
    world.items = {
        "rec:alpha": SeededItem("rec:alpha", "record", "Alpha fact value 42.", "alpha fact value", when, "r1"),
        "rec:beta": SeededItem("rec:beta", "record", "Beta stale value 7.", "beta stale value", when, "r2",
                               status="superseded"),
    }
    return world


def test_metrics_on_a_hand_made_conversation():
    world = _hand_world()
    date = datetime.fromtimestamp(world.items["rec:alpha"].timestamp).astimezone().date().isoformat()
    snapshot = f"<memory>\n- Core fact about Siyuan.\n- [{date}] Alpha fact value 42.\n</memory>"
    base = [{"role": "system", "content": "BASE"}, {"role": "user", "content": "q1"}]
    second = [*base, {"role": "assistant", "content": "OK."}, {"role": "user", "content": "q2"}]
    requests = [
        {"turn": 1, "latency_ms": 4.0, "messages": [*base, {"role": "user", "content": snapshot}], "tools": [],
         "canonical": base},
        {"turn": 2, "latency_ms": 6.0, "tools": [], "canonical": second,
         "messages": [*base, {"role": "user", "content": snapshot}, *second[2:]]},
        {"turn": 3, "latency_ms": 8.0, "tools": [], "canonical": second,
         "messages": [*base, {"role": "user", "content": snapshot}, *second[2:],
                      {"role": "user", "content": "<memory>\n- Core fact about Siyuan.\n- Beta stale value 7.\n</memory>"}]},
    ]
    analyses = analyze_requests(requests, world.core_entries)
    assert [item["fresh_chars"] > 0 for item in analyses] == [True, False, True]
    assert analyses[1]["current_text"] == analyses[0]["current_text"] == snapshot
    assert [item["core_entry_occurrences"] for item in analyses] == [1, 1, 2]
    assert analyses[0]["core_chars"] == len("Core fact about Siyuan.")
    assert [item["prefix_stable"] for item in analyses] == [None, True, True]
    assert analyses[2]["injected_messages"] == 2 and analyses[2]["injected_tokens"] > analyses[1]["injected_tokens"]

    fact = TurnSpec(text="q1", id="t.fact", category="single_fact", expect=(("rec:alpha",),))
    update = TurnSpec(text="q2", id="t.update", category="knowledge_update", expect=(("rec:alpha",),),
                      stale=("Beta stale value",))
    control = TurnSpec(text="q3", id="t.control", category="unrelated")
    first = score_probe(fact, world, analyses[0])
    carried = score_probe(update, world, analyses[1])
    stale = score_probe(update, world, analyses[2])
    unrelated = score_probe(control, world, analyses[2])
    assert (first["recall"], first["dated_hits"], first["unexpected_items"]) == (1.0, [True], [])
    assert (carried["recall"], carried["stale_injected"]) == (1.0, False)
    assert (stale["recall"], stale["recall_visible"], stale["stale_injected"]) == (0.0, 1.0, True)
    assert stale["superseded_items_injected"] == ["rec:beta"]
    assert (unrelated["false_injection"], unrelated["unexpected_items"]) == (True, ["rec:beta"])
    summary = aggregate_probes([first, carried, stale, unrelated])
    assert summary["evidence_recall"] == round((1.0 + 1.0 + 0.0) / 3, 2)
    assert summary["false_injection_rate"] == 1.0 and summary["stale_injection_rate"] == 0.5
    assert (summary["latency_p50_ms"], summary["latency_p95_ms"]) == (6.0, 8.0)


def test_answer_rules_cover_keywords_dates_and_abstention():
    live = LiveSpec(must=(("Zed",), ("date:s2",)), stale=("Solarized",))
    assert score_answer(live, "You switched to Zed on 10月2日.", {"s2": "2026-10-02"})["verdict"] == "pass"
    stale = score_answer(live, "Still Solarized in VS Code.", {"s2": "2026-10-02"})
    assert (stale["verdict"], stale["stale_answer"]) == ("fail", True)
    assert score_answer(LiveSpec(abstain=True), "我没有找到相关记录。", {})["verdict"] == "pass"
    assert score_answer(LiveSpec(), "anything", {})["verdict"] == "unscored"


def test_offline_requests_reconstruct_canonical_history(tmp_path):
    scenario = _scenario("household")

    async def run() -> RequestRecorder:
        recorder = RequestRecorder()
        bundle = build_agent("session", OfflineLLM(recorder), recorder, paths, session_name="session_eval_test")
        try:
            for index, turn in enumerate(scenario.conversation[:3], 1):
                await run_turn(bundle, turn.text, request_id=f"t-{index}", turn=index)
        finally:
            bundle.close()
        return recorder

    with isolated_state(tmp_path / "state") as paths, path_guard(tmp_path / "state") as guard:
        world = seed_world(scenario, now=NOW, workspace=paths.workspace)
        recorder = asyncio.run(run())
    assert guard.violations == []
    assert [request["turn"] for request in recorder.requests] == [1, 2, 3]
    for request in recorder.requests:
        diff = diff_request(request["messages"], request["canonical"])
        assert diff.dropped == 0
        whole = {segment.index for segment in diff.segments if segment.whole}
        rebuilt = []
        for index, message in enumerate(request["messages"]):
            if index in whole:
                continue
            text = message_text(message)
            for segment in diff.segments:
                if segment.index == index:
                    text = text.replace(segment.text, "", 1)
            rebuilt.append((message["role"], text))
        assert rebuilt == [(message["role"], message_text(message)) for message in request["canonical"]]
        assert any(entry in diff.injected_text for entry in world.core_entries)
        assert scenario.conversation[request["turn"] - 1].text not in diff.injected_text


def test_offline_run_reports_every_probe_without_touching_real_state(tmp_path):
    fixture = load_fixture().select(["dev_env"])
    report = run_offline(fixture, ["off", "session"], tmp_path / "eval")
    assert report["isolation"]["violations"] == []
    root = (tmp_path / "eval").resolve()
    assert all(Path(path).resolve().is_relative_to(root) for path in report["isolation"]["resolved_paths"].values())
    expected = [probe.id for probe in fixture.scenarios[0].probes]
    for mode in ("off", "session"):
        scenario = report["modes"][mode]["scenarios"][0]
        assert [probe["id"] for probe in scenario["probes"]] == expected
        assert len(scenario["requests"]) == len(fixture.scenarios[0].conversation)
        assert all(request["core_entry_occurrences"] >= 1 for request in scenario["requests"])
        summary = report["modes"][mode]["summary"]
        assert set(summary["by_category"]) <= set(CATEGORIES)
        assert summary["overall"]["probes"] == len(expected)


def test_path_guard_refuses_installation_state_outside_the_root(tmp_path):
    outside = tmp_path / "outside" / ".astra" / "memory.db"
    outside.parent.mkdir(parents=True)
    allowed = tmp_path / "root" / ".astra" / "memory.db"
    allowed.parent.mkdir(parents=True)
    with path_guard(tmp_path / "root") as guard:
        with pytest.raises(PermissionError):
            outside.write_text("x")
        with pytest.raises(PermissionError):
            sqlite3.connect(outside)
        allowed.write_text("ok")
    assert len(guard.violations) == 2 and allowed.read_text() == "ok"
    assert not outside.exists()


class _ScriptedClient:
    """Stands in for LLMClient: saves "remember" turns with the memory tool, else echoes injected context."""

    def __init__(self, config):
        self.config = config

    async def chat_stream(self, messages, tools, **kwargs):
        import json

        if messages[-1]["role"] == "tool":
            yield {"type": "done", "content": "Saved.", "usage": None}
            return
        texts = [message_text(message) for message in messages if message["role"] == "user"]
        own = [text for text in texts if not text.startswith("[SYSTEM-SUPPLIED")]
        latest = own[-1].split("\n", 1)[-1] if own[-1].startswith("<message_time>") else own[-1]
        if any(marker in latest for marker in ("记", "remember")):
            arguments = json.dumps({"action": "core_add", "scope": "user", "content": latest}, ensure_ascii=False)
            yield {"type": "tool_calls", "calls": [{"id": "call-1", "name": "memory", "arguments": arguments}],
                   "content": "", "reasoning_content": "", "usage": None}
            return
        context = "\n".join(text for text in texts if text.startswith("[SYSTEM-SUPPLIED"))
        answer = f"Context says: {context[-1500:]}" if context else "I could not find that."
        yield {"type": "chunk", "content": answer}
        yield {"type": "done", "content": answer, "usage": None}


def test_live_tier_plumbing_with_a_scripted_provider(tmp_path, monkeypatch):
    from agent.evals.memory import live
    from agent.runtime.llm import LLMConfig

    config = LLMConfig(model="scripted", api_key="offline", base_url="http://127.0.0.1:9/v1")
    monkeypatch.setattr(live, "resolve_live_config", lambda model_key=None: (config, "scripted"))
    monkeypatch.setattr("agent.runtime.llm.LLMClient", _ScriptedClient)
    fixture = load_fixture().select(["household"])
    report = live.run_live(fixture, ["session"], tmp_path / "live")
    mode = report["modes"]["session"]
    assert report["isolation"]["violations"] == [] and mode["failures"] == []
    probes = mode["scenarios"][0]["probes"]
    assert [probe["id"] for probe in probes] == [probe.id for probe in fixture.scenarios[0].probes]
    assert all(probe["answer"]["verdict"] in {"pass", "fail", "unscored"} for probe in probes)
    saved = {item["id"]: item for item in mode["write_path"]["probes"]}
    assert set(saved) == {spec.id for spec in fixture.write_path}
    cat = saved["write.cat_name"]
    assert cat["teach"][0]["tool_calls"] == ["memory"]
    assert any("Mochi" in entry for entry in cat["retained"]["core_entries"])
    assert cat["answer"]["verdict"] == "pass"
