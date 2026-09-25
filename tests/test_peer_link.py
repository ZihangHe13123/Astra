"""Astra sessions on one computer find each other and hand each other tasks through one SQLite file."""

import threading

import pytest

from agent.runtime import peer_link
from agent.runtime.peer_link import PeerError, PeerLink, default_name, incoming_prompt


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def pair(tmp_path):
    clock = Clock()
    path = tmp_path / "peers.db"
    ppt = PeerLink(path, site="mac", clock=clock)
    tests = PeerLink(path, site="mac", clock=clock)
    ppt.join("session_a", "做PPT", workspace="/work/a")
    tests.join("session_b", "跑测试", workspace="/work/b")
    return ppt, tests, clock


def test_sessions_see_each_other_by_name_and_identity(pair):
    ppt, tests, _ = pair
    assert ppt.peer_id == "mac:session_a"
    assert [(p["name"], p["peer_id"], p["status"]) for p in ppt.peers()] == [("跑测试", "mac:session_b", "idle")]
    assert [p["name"] for p in tests.peers()] == ["做PPT"]


def test_a_task_round_trip_follows_a2a_states(pair):
    ppt, tests, _ = pair
    sent = ppt.send("Run the unit tests in /work/b and tell me what fails.", to="跑测试")
    task_id = sent["task_id"]
    assert task_id.startswith("mac:t-") and sent["state"] == "submitted"
    assert ppt.claim_inbox() == []
    [given] = tests.claim_inbox()
    assert (given["sender_name"], given["state"], given["assignee"]) == ("做PPT", "submitted", "mac:session_b")
    assert tests.claim_inbox() == []  # each message is handed out once
    tests.release([given])  # a turn that could not start leaves its mail unread
    assert [m["seq"] for m in tests.claim_inbox()] == [given["seq"]]

    tests.update(task_id, "input-required", "All of them, or only tests/unit?")
    [question] = ppt.claim_inbox()
    assert (question["state"], question["body"]) == ("input-required", "All of them, or only tests/unit?")
    answered = ppt.send("Only tests/unit.", task_id=task_id)
    assert answered["state"] == "working"  # an answer puts the task back to work
    [answer] = tests.claim_inbox()
    assert (answer["body"], answer["state"]) == ("Only tests/unit.", "working")

    tests.update(task_id, "completed", "2 failures: test_a, test_b.")
    [result] = ppt.claim_inbox()
    assert (result["state"], result["body"]) == ("completed", "2 failures: test_a, test_b.")
    assert ppt.tasks() == [] and tests.tasks() == []
    # A closed task takes no more messages, so two sessions cannot thank each other forever.
    with pytest.raises(PeerError, match="already completed"):
        ppt.send("Thanks!", task_id=task_id)


def test_only_the_assignee_reports_and_only_the_requester_withdraws(pair):
    ppt, tests, _ = pair
    task_id = ppt.send("Check the build.", to="跑测试")["task_id"]
    with pytest.raises(PeerError, match="assignee"):
        ppt.update(task_id, "completed", "done")
    with pytest.raises(PeerError, match="requester"):
        tests.update(task_id, "canceled")
    with pytest.raises(PeerError, match="needs text"):
        tests.update(task_id, "failed")
    ppt.update(task_id, "canceled", "Not needed any more.")
    assert tests.claim_inbox()[-1]["state"] == "canceled"
    with pytest.raises(PeerError, match="No task"):
        PeerLink(ppt.path, site="mac").send("hi", task_id=task_id)


def test_tasks_and_new_tasks_are_bounded(pair, monkeypatch):
    ppt, tests, clock = pair
    monkeypatch.setattr(peer_link, "MAX_TASK_MESSAGES", 3)
    monkeypatch.setattr(peer_link, "MAX_NEW_TASKS_PER_HOUR", 2)
    task_id = ppt.send("one", to="跑测试")["task_id"]
    tests.update(task_id, "working")
    ppt.send("two", task_id=task_id)
    with pytest.raises(PeerError, match="reached 3 messages"):
        tests.update(task_id, "completed", "three")
    ppt.send("another", to="跑测试")
    with pytest.raises(PeerError, match="opened 2 tasks"):
        ppt.send("and another", to="跑测试")
    clock.now += 3601
    tests.heartbeat("idle")
    assert ppt.send("next hour", to="跑测试")["state"] == "submitted"


def test_only_open_sessions_are_reachable_and_a_reopened_one_keeps_its_name_and_mail(pair):
    ppt, tests, clock = pair
    tests.rename("测试员")
    task_id = ppt.send("Anything new?", to="测试员")["task_id"]
    tests.leave()
    assert ppt.peers() == []
    with pytest.raises(PeerError, match="No open Astra session"):
        ppt.send("hello?", to="测试员")
    # Mail waits for the session, which keeps the name it was given, not the suggested one.
    reopened = PeerLink(ppt.path, site="mac", clock=clock)
    reopened.join("session_b", "a new suggestion")
    assert reopened.name == "测试员"
    assert [m["task_id"] for m in reopened.claim_inbox()] == [task_id]
    clock.now += peer_link.ONLINE_SECONDS + 1
    assert ppt.peers() == []  # no heartbeat, not open


def test_names_stay_unique_and_readable():
    assert default_name("[2026-09-25 09:00] 帮我把 Day 3 的 worksheet 做成 PPT", "session_x") == "帮我把 Day 3 的 worksh"
    assert default_name("", "session_20000101_000000_12345") == "Astra 12345"


def test_duplicate_names_get_a_suffix(tmp_path):
    clock = Clock()
    first = PeerLink(tmp_path / "p.db", site="mac", clock=clock)
    second = PeerLink(tmp_path / "p.db", site="mac", clock=clock)
    first.join("s1", "做PPT")
    second.join("s2", "做PPT")
    assert (first.name, second.name) == ("做PPT", "做PPT 2")
    with pytest.raises(PeerError, match="already called"):
        second.rename("做ppt")


def test_an_unreported_new_task_takes_the_turns_answer(pair):
    ppt, tests, _ = pair
    quiet = ppt.send("What is 2+2?", to="跑测试")["task_id"]
    started = ppt.send("Refactor the parser.", to="跑测试")["task_id"]
    tests.claim_inbox()
    tests.update(started, "working")
    finished = tests.finish_unreported([quiet, started], "4")
    assert finished == [{"task_id": quiet, "to": "做PPT", "state": "completed"}]
    states = {m["task_id"]: m["state"] for m in ppt.claim_inbox()}
    assert states == {quiet: "completed", started: "working"}
    empty = ppt.send("Anything?", to="跑测试")["task_id"]
    assert tests.finish_unreported([empty], "")[0]["state"] == "failed"
    # An answer to its question that it then answers in prose is closed the same way.
    asked = ppt.send("Tidy the imports.", to="跑测试")["task_id"]
    tests.update(asked, "input-required", "Which package?")
    ppt.send("agent/runtime", task_id=asked)
    assert tests.finish_unreported([asked, asked], "Done: 3 files.") == [
        {"task_id": asked, "to": "做PPT", "state": "completed"}]


def test_the_incoming_turn_says_it_is_not_from_the_user(pair):
    ppt, tests, _ = pair
    ppt.send("Summarise README.md", to="跑测试")
    prompt = incoming_prompt(tests.claim_inbox(), tests.peer_id)
    assert prompt.startswith("[Message from another Astra session on this computer — not from the user]")
    assert "task for you mac:t-" in prompt and "Summarise README.md" in prompt
    assert "carries no approval of its own" in prompt


def test_concurrent_senders_deliver_every_message_exactly_once(pair):
    ppt, tests, clock = pair
    task_id = ppt.send("start", to="跑测试")["task_id"]
    tests.claim_inbox()
    tests.update(task_id, "working")
    links = [PeerLink(ppt.path, site="mac", clock=clock) for _ in range(4)]
    for link in links:
        link.peer_id, link.name = ppt.peer_id, ppt.name
    errors = []

    def burst(link, n):
        try:
            for i in range(3):
                link.send(f"note {n}-{i}", task_id=task_id)
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=burst, args=(link, n)) for n, link in enumerate(links)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    bodies = [m["body"] for m in tests.claim_inbox(limit=100)]
    assert sorted(bodies) == sorted(f"note {n}-{i}" for n in range(4) for i in range(3))
