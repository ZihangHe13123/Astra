import pytest

from agent.runtime.session_wakeup import SessionWakeups, visible_wakeup_history


def scheduler():
    now = [0.0]
    return SessionWakeups(clock=lambda: now[0], wall_clock=lambda: 1000 + now[0]), now


def schedule(owner, **kwargs):
    return owner.schedule(session="work", prompt="Check the CI run", original_request="Check this CI every minute", **kwargs)


def test_one_shot_waits_without_claiming_busy_turn():
    owner, now = scheduler()
    plan = schedule(owner, delay_seconds=60)
    assert owner.claim(session="work", busy=False) is None
    now[0] = 60
    assert owner.claim(session="work", busy=True) is None
    claimed = owner.claim(session="work", busy=False)
    assert claimed["id"] == plan["id"]
    assert owner.claim(session="work", busy=False) is None
    owner.finish(plan["id"], outcome="completed", summary="CI passed")
    assert owner.status()["state"] == "completed"
    assert owner.claim(session="work", busy=False) is None


def test_busy_ticks_coalesce_and_next_interval_starts_after_completion():
    owner, now = scheduler()
    plan = schedule(owner, delay_seconds=60, interval_seconds=60)
    now[0] = 240
    assert owner.claim(session="work", busy=True) is None
    assert owner.claim(session="work", busy=False)
    owner.finish(plan["id"], outcome="unchanged", summary="")
    assert owner.status()["next_at"] == "1970-01-01T00:21:40+00:00"
    assert owner.claim(session="work", busy=False) is None
    now[0] = 300
    assert owner.claim(session="work", busy=False)["runs"] == 2


def test_replacement_and_cancel_invalidate_old_completion():
    owner, now = scheduler()
    old = schedule(owner, delay_seconds=60)
    now[0] = 60
    owner.claim(session="work", busy=False)
    new = schedule(owner, delay_seconds=120)
    assert owner.finish(old["id"], outcome="completed", summary="old") is None
    assert owner.status()["id"] == new["id"]
    owner.cancel("user_cancelled")
    now[0] = 500
    assert owner.claim(session="work", busy=False) is None


def test_expiry_and_session_switch_never_replay():
    owner, now = scheduler()
    schedule(owner, delay_seconds=60, lifetime_seconds=120)
    now[0] = 121
    assert owner.claim(session="work", busy=False) is None
    assert owner.status()["state"] == "expired"
    schedule(owner, delay_seconds=60)
    assert owner.claim(session="other", busy=True) is None
    assert owner.status()["state"] == "session_changed"


@pytest.mark.parametrize("kwargs", [
    {"delay_seconds": 0}, {"delay_seconds": 59}, {"delay_seconds": float("nan")},
    {"delay_seconds": float("inf")}, {"interval_seconds": -1},
    {"interval_seconds": 30}, {"lifetime_seconds": 50000},
    {"delay_seconds": 180, "lifetime_seconds": 120},
])
def test_invalid_schedule_does_not_replace_existing_plan(kwargs):
    owner, _ = scheduler()
    original = schedule(owner)
    with pytest.raises(ValueError):
        schedule(owner, **kwargs)
    assert owner.status()["id"] == original["id"]


def test_rejected_times_name_the_schema_parameters():
    owner, _ = scheduler()
    # The default lifetime is one hour, so a one-hour delay needs a longer lifetime.
    with pytest.raises(ValueError) as rejected:
        schedule(owner, delay_seconds=3600)
    message = str(rejected.value)
    assert "lifetime_seconds must be greater than delay_seconds" in message
    assert "interval_seconds must be 0 or at least 60" in message
    assert "expiry" not in message
    assert schedule(owner, delay_seconds=3600, lifetime_seconds=7200)["state"] == "scheduled"


def test_failure_stops_repetition():
    owner, now = scheduler()
    plan = schedule(owner, interval_seconds=60)
    now[0] = 300
    owner.claim(session="work", busy=False)
    owner.finish(plan["id"], outcome="failed", summary="Authentication is required")
    now[0] = 500
    assert owner.claim(session="work", busy=False) is None
    assert owner.status()["state"] == "failed"


def test_claim_is_rechecked_at_turn_entry_and_old_plan_cannot_run():
    owner, now = scheduler()
    plan = schedule(owner, delay_seconds=60, lifetime_seconds=120)
    now[0] = 60
    owner.claim(session="work", busy=False)
    assert owner.remaining(plan["id"], "other") == 0
    assert owner.remaining(plan["id"], "work") == 60
    now[0] = 120
    assert owner.remaining(plan["id"], "work") == 0


def test_silent_tick_history_stays_hidden_but_result_and_real_user_remain():
    messages = [
        {"role": "user", "content": "Monitor CI"},
        {"role": "user", "content": "internal check", "provenance": "session_wakeup"},
        {"role": "assistant", "content": "No changes"},
        {"role": "user", "content": "internal check", "provenance": "session_wakeup"},
        {"role": "assistant", "content": "checked"},
        {"role": "assistant", "content": "CI passed", "provenance": "wakeup_notification"},
        {"role": "user", "content": "Thanks"},
        {"role": "assistant", "content": "Welcome"},
    ]
    assert [m["content"] for m in visible_wakeup_history(messages)] == ["Monitor CI", "CI passed", "Thanks", "Welcome"]
