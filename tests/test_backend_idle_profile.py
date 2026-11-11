"""Real backend protocol: what an idle backend does on its timer stays out of the runtime profile."""

import json

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend

SLOW = 0.3
# The real mailbox, with each poll announced once it is over, a heartbeat twice a second instead of
# every five, and the second look for mail taking a while.
POLLS_ANNOUNCED = f"""
import functools, time
from agent.cli import backend
from agent.runtime.peer_link import PeerLink

def announced(name, slow_call=0):
    original, calls = getattr(PeerLink, name), []

    @functools.wraps(original)
    def poll(self, *args):
        calls.append(name)
        if len(calls) == slow_call:
            time.sleep({SLOW})
        try:
            return original(self, *args)
        finally:
            backend._send({{"type": "polled", "poll": name, "count": len(calls)}})

    setattr(PeerLink, name, poll)

announced("has_mail", slow_call=2)
announced("heartbeat")
backend.HEARTBEAT_SECONDS = 0.5
raise SystemExit(backend.run())
"""


def test_an_idle_backend_s_polls_are_in_the_profile_only_when_they_were_slow(tmp_path):
    """Live Astra 2026-11-11: two idle backends, each looking for mail once a second and reporting
    in every five, wrote 99.8% of the runtime profile, so the 15 MB kept of it covered under eight
    hours. A poll that took 2 ms explains no wait; one that held the tick up does."""
    state = tmp_path / "state"
    proc, _, _, wait_for = _start_question_protocol_backend(
        tmp_path, 0, "idle_profile", bootstrap_code=POLLS_ANNOUNCED,
        env_overrides={"ASTRA_PROFILE_QUERY": "1", "ASTRA_HOME": str(state)})
    try:
        wait_for(lambda event: event.get("type") == "model_info")
        # Quick polls on both sides of the slow one, and a few heartbeats.
        for poll, count in (("has_mail", 3), ("heartbeat", 3)):
            wait_for(lambda event, poll=poll, count=count: event.get("type") == "polled"
                     and event["poll"] == poll and event["count"] >= count, timeout=30)
    finally:
        _stop_protocol_backend(proc)

    took: dict[str, list[float]] = {}
    for line in (state / "runtime-profile.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record["kind"] == "io":
            took.setdefault(record["label"], []).append(record["metrics_ms"]["total_ms"])
    # The slow look for mail is there, and nothing on the timer that took under 50 ms.
    assert max(took["PeerLink.has_mail"]) >= SLOW * 1000
    assert min(took["PeerLink.has_mail"] + took.get("PeerLink.heartbeat", [])) >= 50
    # What the backend does once is recorded however quick it was.
    assert took["PeerLink.join"] and took["TaskStore.recover_interrupted"]
