"""Use real registry/approval/session entry points and a noninteractive native fixture."""

import json

import pytest

from agent.runtime import approval_time
from agent.runtime.tools.grants import grant_space
from tests import test_computer_tools as cu

backend, manager, registry = cu.backend, cu.manager, cu.registry
BOUNDARY = (
    "Allow once, or allow ordinary background edits in this window for up to 2 minutes. "
    "Ends when the window changes, someone takes over, a high-impact action is requested, or an action fails."
)


def snapshot(result):
    assert not result["error"], result
    return json.loads(result["fresh_output"])["snapshot_id"]


def click(reg, snapshot_id):
    return cu.run(
        reg.execute(
            "computer_act",
            {
                "snapshot_id": snapshot_id,
                "interaction_mode": "background",
                "actions": [{"type": "click", "element_ref": f"{snapshot_id}:1"}],
            },
        )
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "observe",
        "refresh_catalog",
        "expired",
        "switch_window",
        "high_impact",
        "handoff",
        "failed_act",
        "other_conversation",
        "lost_stable_identity",
        "expired_without_observing",
    ],
)
def test_background_session_is_a_bounded_write_lease(registry, manager, backend, monkeypatch, scenario):
    requests = []
    clock = [1000.0]
    monkeypatch.setattr(approval_time, "continuous_now", lambda: clock[0])

    async def answer(card):
        if card.get("kind") != "computer_write":
            return "once"
        requests.append(card)
        return "session" if "session" in card["choices"] else "deny"

    cu.register(registry, manager)
    registry.set_approval_handler(answer)
    if scenario == "lost_stable_identity":
        backend.catalog_window_identity_ref = ""
    cu.focus(registry)
    state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
    first = click(registry, state)
    if scenario == "lost_stable_identity":
        assert requests[0]["choices"] == ["once", "deny"]
        assert first["code"] == "approval_denied"
        return
    state = snapshot(first)
    assert len(requests) == 1
    if scenario in {"observe", "refresh_catalog", "expired"}:
        clock[0] += 119.999
        if scenario == "refresh_catalog":
            assert not cu.run(registry.execute("computer_apps", {}))["error"]
        state = snapshot(
            cu.run(registry.execute("computer_get_app_state", {"app_ref": "app-1", "window_ref": "window-1"}))
        )
        state = snapshot(click(registry, state))
        assert len(requests) == 1
        if scenario == "expired":
            clock[0] += 0.002
            assert not click(registry, state)["error"]
            assert len(requests) == 2
    elif scenario == "expired_without_observing":
        clock[0] += 120.01
        assert not click(registry, state)["error"] and len(requests) == 2
    elif scenario == "high_impact":
        high = cu.run(
            registry.execute(
                "computer_act",
                {"snapshot_id": state, "actions": [{"type": "keypress", "key": "s", "modifiers": ["command"]}]},
            )
        )
        assert high["code"] == "approval_denied" and requests[-1]["choices"] == ["once", "deny"]
        state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
        assert not click(registry, state)["error"] and len(requests) == 3
    elif scenario == "switch_window":
        for window in ("window-2", "window-1"):
            assert not cu.run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": window}))["error"]
        state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
        assert not click(registry, state)["error"] and len(requests) == 2
    elif scenario == "handoff":
        assert not cu.run(registry.execute("computer_handoff", {}))["error"]
        cu.run(registry.execute("computer_apps", {}))
        assert not cu.run(registry.execute("computer_resume", {"app_ref": "app-1", "window_ref": "window-1"}))["error"]
        state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
        assert not click(registry, state)["error"] and len(requests) == 2
    elif scenario == "failed_act":
        failed = click(registry, "stale-but-well-formed")
        assert failed["error"]
        state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
        assert not click(registry, state)["error"] and len(requests) == 2
    elif scenario == "other_conversation":
        with grant_space("channel:other"):
            assert not click(registry, state)["error"]
        assert len(requests) == 2
    assert requests[0]["approval_boundary"] == BOUNDARY


def test_raw_scope_without_window_lease_cannot_authorize_an_edit(registry, manager, backend):
    requests = []

    async def deny(card):
        requests.append(card)
        return "deny"

    cu.register(registry, manager)
    registry.set_approval_handler(deny)
    cu.focus(registry)
    state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
    scope = f"computer-write:{manager.session_id}:{backend.catalog_bundle_id}"
    registry.approved_permission_scopes.add(scope)
    result = click(registry, state)
    assert result.get("code") == "approval_denied"
    assert len(requests) == 1 and scope not in registry.approved_permission_scopes
    assert not any(name == "act" for name, _ in backend.calls)


def test_audited_background_decision_cannot_restart_expired_lease(registry, manager, backend, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(approval_time, "continuous_now", lambda: now[0])
    cu.register(registry, manager)
    cu.focus(registry)
    state = snapshot(cu.run(registry.execute("computer_snapshot", {})))

    async def approve(_card):
        answer = approval_time.TimedApprovalDecision("session")
        now[0] += 121
        return answer

    registry.set_approval_handler(approve)
    assert click(registry, state).get("code") == "approval_required"
    assert not any(n == "act" for n, _ in backend.calls)


def test_reused_write_lease_expiring_before_native_dispatch_generates_no_input(registry, manager, backend, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(approval_time, "continuous_now", lambda: now[0])

    async def approve(_card):
        return "session"

    cu.register(registry, manager)
    registry.set_approval_handler(approve)
    cu.focus(registry)
    state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
    state = snapshot(click(registry, state))
    before = sum(n == "act" for n, _ in backend.calls)
    original = manager.act

    async def late(*args, **kwargs):
        now[0] += 121
        return await original(*args, **kwargs)

    monkeypatch.setattr(manager, "act", late)
    assert click(registry, state).get("code") == "approval_required"
    assert sum(n == "act" for n, _ in backend.calls) == before


@pytest.mark.parametrize("blocker", ["foreground_save", "prohibited"])
def test_requesting_higher_authority_releases_existing_background_lease(registry, manager, backend, blocker):
    prompts = []

    async def choose(card):
        prompts.append(card)
        return "session" if len(prompts) == 1 else "deny"

    cu.register(registry, manager)
    registry.set_approval_handler(choose)
    cu.focus(registry)
    state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
    state = snapshot(click(registry, state))
    if blocker == "foreground_save":
        backend.force_takeover = True
        backend.plan_pid_action_classes = ()
        args = {
            "interaction_mode": "foreground_takeover",
            "actions": [{"type": "keypress", "key": "s", "modifiers": ["command"]}],
        }
        error = "approval_denied"
    else:
        args = {"actions": [{"type": "click", "x": 20, "y": 510}]}
        error = "computer_handoff_required"
    grants_at_handler_entry = []
    result = cu.run(registry.execute(
        "computer_act", {"snapshot_id": state, **args},
        on_start=lambda: grants_at_handler_entry.append(set(registry.approved_permission_scopes)),
    ))
    assert result.get("code") == error
    if blocker == "prohibited":
        assert len(grants_at_handler_entry) == 1
        assert not any(scope.startswith("computer-write:") for scope in grants_at_handler_entry[0])
    backend.force_takeover = False
    state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
    assert click(registry, state).get("code") == "approval_denied"
    assert sum(x["kind"] == "computer_write" for x in prompts) == 2
    assert sum(name == "act" for name, _ in backend.calls) == 1
