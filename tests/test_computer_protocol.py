import asyncio
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.runtime import computer_protocol
from agent.runtime.computer_backend import ComputerActionPlan, ComputerAppCatalog, ComputerSessionError, ComputerSessionManager, ComputerTarget
from agent.runtime.computer_forms import observation_capabilities
from agent.runtime.computer_protocol import (
    ComputerAction,
    ComputerError,
    ComputerErrorCode,
    ComputerRequest,
    ComputerResponse,
    ComputerSnapshot,
    ForegroundFragmentActionRequirement,
    ForegroundFragmentDeclaration,
    ForegroundFragmentPlanRequest,
    ForegroundFragmentStageDeclaration,
    FragmentStageAuthority,
    FragmentStageCommit,
    decode_response,
    encode_request,
)
from agent.runtime.macos_computer import HelperApplicationError, HelperTransportError, MacComputerBackend


@pytest.mark.parametrize(("key", "canonical"), [("backspace", "delete"), ("Esc", "escape"), ("ArrowLeft", "left"), ("ENTER", "return")])
def test_robustness_key_aliases_are_canonical_on_wire(key, canonical):
    action = ComputerAction.from_mapping({"type": "keypress", "key": key})
    assert action.key == canonical
    assert action.to_mapping()["key"] == canonical


@pytest.mark.parametrize("path", ["", "copy.docx", "/", "/tmp/", "/tmp/../copy.docx", "/tmp//copy.docx",
                                  "/tmp/copy\x00.docx", "/tmp/" + "汉" * 1400])
def test_new_copy_requires_an_absolute_unambiguous_path(path):
    with pytest.raises(ValueError, match="absolute local file path"):
        ComputerAction.from_mapping({"type": "click", "element_ref": "save", "save_new_copy_to": path})


@pytest.mark.parametrize("action", [
    {"type": "click", "x": 30, "y": 24}, {"type": "click", "element_index": 2},
    {"type": "keypress", "key": "s", "modifiers": ["command"]},
    {"type": "keypress", "key": "return", "element_ref": "search"},
    {"type": "double_click", "element_ref": "save"},
])
def test_new_copy_requires_exact_save_control_or_default_action(action):
    with pytest.raises(ValueError, match="standalone Save button"):
        ComputerAction.from_mapping({**action, "save_new_copy_to": "/tmp/copy.docx"})


def test_robustness_bad_key_has_bounded_supported_forms():
    with pytest.raises(ValueError) as failure:
        ComputerAction(type="keypress", key="cmd+a")
    message = str(failure.value)
    assert "delete" in message and "escape" in message and "modifiers separately" in message
    assert len(message) < 512


@pytest.mark.parametrize("flag", [True, False, 1, "true", None])
def test_focus_loan_wire_only_accepts_explicit_true_on_plain_begin(flag):
    payload = {"snapshot_id": "snap-1", "plan_ref": "plan-1", "continued_loan": flag, "loan_deadline_continuous_ms": 420_000}
    if flag is True:
        encoded = encode_request(ComputerRequest(request_id="req", operation="takeover_begin", payload=payload))
        assert json.loads(encoded)["payload"]["continued_loan"] is True
    else:
        with pytest.raises(ValueError):
            encode_request(ComputerRequest(request_id="req", operation="takeover_begin", payload=payload))


@pytest.mark.parametrize("fields", [
    {"continued_loan": True}, {"loan_deadline_continuous_ms": True}, {"loan_deadline_continuous_ms": 0},
    {"loan_deadline_continuous_ms": -1}, {"loan_deadline_continuous_ms": "1"}, {"loan_deadline_continuous_ms": 2**53},
    {"loan_deadline_continuous_ms": 1.5}, {"loan_deadline_continuous_ms": 1.0}, {"loan_deadline_continuous_ms": None},
    {"loan_deadline_uptime_ms": 420_000}, {"loan_deadline_uptime_ms": 420_000, "continued_loan": True},
    {"loan_deadline_uptime_ms": 420_000, "loan_deadline_continuous_ms": 420_000},
])
def test_focus_loan_wire_rejects_unbounded_or_invalid_loan(fields):
    with pytest.raises(ValueError):
        encode_request(ComputerRequest(request_id="req", operation="takeover_begin",
            payload={"snapshot_id": "snap-1", "plan_ref": "plan-1", **fields}))


def test_focus_loan_capability_requires_the_continuous_clock_version():
    assert observation_capabilities({"observation_capabilities": ["focus_loan_v2", "subtree_v1"]}) == ["subtree_v1"]
    assert observation_capabilities({"observation_capabilities": ["focus_loan_v3"]}) == ["focus_loan_v3"]


def test_presentation_catalog_sheet_roles_requires_negotiated_relations():
    base = {"takeover_ref": "bound"}
    extended = {**base, "window_catalog_relations": True, "sheet_roles": True}
    for payload in ({**base, "sheet_roles": True}, {**extended, "sheet_roles": False}):
        with pytest.raises(ValueError):
            ComputerRequest("req", "presentation_apps", payload)
    assert json.loads(encode_request(ComputerRequest("req", "presentation_apps", extended)))["payload"] == extended


_FIXTURE_FOCUS_CONSENT = {"frontmost_pid": 37, "decided_at_continuous_us": 45_125_000}
_BEGIN_FRAGMENT = ForegroundFragmentDeclaration(
    "a" * 64, (ForegroundFragmentStageDeclaration(
        "b" * 64, 1, (ForegroundFragmentActionRequirement("wait", None, None),),
    ),), 1, 1000, True,
)


@pytest.mark.parametrize("extra", [{}, {"loan_deadline_continuous_ms": 165_125}, {"fragment": _BEGIN_FRAGMENT}])
def test_fresh_begin_requires_focus_consent(extra):
    payload = {"snapshot_id": "s", "plan_ref": "p", **extra}
    with pytest.raises(ValueError, match="focus_consent"):
        ComputerRequest("r", "takeover_begin", payload)
    marked = {**payload, "focus_consent": _FIXTURE_FOCUS_CONSENT}
    expected = {**marked, "fragment": _BEGIN_FRAGMENT.to_mapping()} if "fragment" in extra else marked
    assert json.loads(encode_request(ComputerRequest("r", "takeover_begin", marked)))["payload"] == expected


def test_focus_consent_strict_wire_agrees_for_oneshot_and_first_bounded_begin():
    assert observation_capabilities({'observation_capabilities': ['focus_consent_v1']}) == ['focus_consent_v1']
    base = {'snapshot_id': 's', 'plan_ref': 'p'}
    receipt = {'frontmost_pid': 1, 'decided_at_continuous_us': 45_125_000}
    for extra in [{}, {'loan_deadline_continuous_ms': 165_125}]:
        payload = {**base, **extra, 'focus_consent': receipt}
        assert json.loads(encode_request(ComputerRequest('r', 'takeover_begin', payload)))['payload'] == payload
    for extra in [{'continued_loan': True, 'loan_deadline_continuous_ms': 165_125}, {'fragment': {}},
                  {'fragment': _BEGIN_FRAGMENT, 'loan_deadline_continuous_ms': 165_125}]:
        with pytest.raises(ValueError):
            ComputerRequest('r', 'takeover_begin', {**base, **extra, 'focus_consent': receipt})
    for malformed in [False, None, {}, {'frontmost_pid': 1}, {**receipt, 'other': 0},
                      *({**receipt, 'frontmost_pid': bad} for bad in [None, True, 1.0, 0, -1, 2**31]),
                      *({**receipt, 'decided_at_continuous_us': bad} for bad in [None, True, 1.0, 0, -1, 2**53])]:
        with pytest.raises(ValueError):
            ComputerRequest('r', 'takeover_begin', {**base, 'focus_consent': malformed})


@pytest.mark.parametrize(("decided_us", "deadline_ms", "accepted"), [
    (100_000_000, 310_000, False),  # No new 120-second window on arrival at t=200s.
    (100_000_000, 220_000, True), (100_000_000, 220_001, False),
    (99_999_999, 219_999, True), (99_999_999, 220_000, False),  # 1 microsecond too long
    (9_007_199_254_739_999, 9_007_199_374_740, False),
])
def test_fresh_loan_deadline_cap_on_wire(decided_us, deadline_ms, accepted):
    payload = {
        "snapshot_id": "s", "plan_ref": "p", "loan_deadline_continuous_ms": deadline_ms,
        "focus_consent": {"frontmost_pid": 37, "decided_at_continuous_us": decided_us},
    }
    if accepted:
        assert json.loads(encode_request(ComputerRequest("r", "takeover_begin", payload)))["payload"] == payload
    else:
        with pytest.raises(ValueError, match="deadline"):
            ComputerRequest("r", "takeover_begin", payload)


@pytest.mark.skipif(os.name != "posix", reason="session manager requires POSIX artifact authority")
def test_fresh_loan_deadline_cap_stops_manager_before_backend(tmp_path):
    mode = computer_protocol.ComputerInteractionMode.FOREGROUND_TAKEOVER
    backend = SimpleNamespace(
        select=AsyncMock(return_value=ComputerTarget("app", "window")),
        snapshot=AsyncMock(return_value=ComputerSnapshot("snap")),
        plan_actions=AsyncMock(return_value=ComputerActionPlan("plan", mode, True, "foreground_takeover_required", (), ())),
        begin_takeover=AsyncMock(return_value="must-not-begin"), close=AsyncMock(),
    )

    async def scenario():
        manager = ComputerSessionManager(backend, cache_root=tmp_path)
        try:
            await manager.select("app", "window")
            snap = await manager.snapshot()
            plan = await manager.plan_actions(snap.snapshot_id, [{"type": "wait", "duration_ms": 0}], interaction_mode=mode)
            with pytest.raises(ComputerSessionError):
                await manager.begin_takeover(snap.snapshot_id, plan.plan_ref, loan_deadline=lambda: 310_000,
                                             focus_consent={"frontmost_pid": 37, "decided_at_continuous_us": 100_000_000})
            backend.begin_takeover.assert_not_awaited()
        finally:
            await manager.close()

    asyncio.run(scenario())


def test_focus_consent_rejection_never_retries_without_a_receipt():
    rejection = ComputerError(ComputerErrorCode.PROTOCOL_MISMATCH, 'takeover_begin has an invalid payload')
    backend, frames = _presentation_backend({'ok': False, 'error': rejection})
    receipt = {'decided_at_continuous_us': 45_125_000, 'frontmost_pid': 37}
    with pytest.raises(HelperApplicationError) as failure:
        asyncio.run(backend.begin_takeover('s', 'p', focus_consent=receipt))
    assert failure.value.error.code is ComputerErrorCode.PROTOCOL_MISMATCH and 'Update' in failure.value.error.message
    assert frames == [('takeover_begin', {'snapshot_id': 's', 'plan_ref': 'p', 'focus_consent': receipt})]


@pytest.mark.parametrize("continued", [False, True])
def test_focus_loan_new_deadline_rejected_by_strict_old_v4_without_downgrade(continued):
    frames = []
    rejection = ComputerError(ComputerErrorCode.PROTOCOL_MISMATCH, "takeover_begin has an invalid payload")

    async def strict_old_v4(req):
        frame = json.loads(encode_request(req))
        frames.append((frame["operation"], frame["payload"]))
        base = {"snapshot_id", "plan_ref"}
        if set(frame["payload"]) in (base, base | {"loan_deadline_uptime_ms"}, base | {"loan_deadline_uptime_ms", "continued_loan"}):
            pytest.fail("continuous-clock loan reached the mock legacy takeover observer")
        return ComputerResponse(req.request_id, ok=False, error=rejection)

    backend = MacComputerBackend(SimpleNamespace(request=strict_old_v4))
    with pytest.raises(HelperApplicationError) as failure:
        asyncio.run(backend.begin_takeover("snap", "plan", loan_deadline_continuous_ms=165_125, continued_loan=continued,
                                           focus_consent=None if continued else _FIXTURE_FOCUS_CONSENT))

    assert failure.value.error.code is ComputerErrorCode.PROTOCOL_MISMATCH
    if continued:
        assert failure.value.error == rejection
    else:
        assert "Update" in failure.value.error.message
    assert frames == [("takeover_begin", {
        "snapshot_id": "snap", "plan_ref": "plan", "loan_deadline_continuous_ms": 165_125,
        **({"continued_loan": True} if continued else {"focus_consent": _FIXTURE_FOCUS_CONSENT}),
    })]


def test_presentation_capability_is_explicit_and_independent_of_focus_loans():
    assert observation_capabilities({"observation_capabilities": ["focus_presentation_v1"]}) == ["focus_presentation_v1"]
    assert "focus_presentation_v1" not in observation_capabilities({"observation_capabilities": ["focus_loan_v3"]})


@pytest.mark.parametrize("operation", ["presentation_apps", "cancel_presentation"])
def test_presentation_wire_requires_exactly_one_bounded_takeover_ref(operation):
    payload = {"takeover_ref": "é" * 256}
    assert json.loads(encode_request(ComputerRequest("r", operation, payload)))["payload"] == payload
    for invalid in ({}, {"takeover_ref": ""}, {"takeover_ref": 1}, {"takeover_ref": "é" * 257},
                    {**payload, "restore_previous_focus": False}, {**payload, "fragment": {}}):
        with pytest.raises(ValueError):
            ComputerRequest("r", operation, invalid)


def _presentation_backend(*responses):
    frames = []
    replies = iter(responses)

    async def request(req):
        frame = json.loads(encode_request(req))
        frames.append((frame["operation"], frame["payload"]))
        return ComputerResponse(req.request_id, **next(replies))

    return MacComputerBackend(SimpleNamespace(request=request)), frames


def test_presentation_apps_publishes_only_a_valid_catalog():
    backend, frames = _presentation_backend(
        {"ok": True, "result": {"catalog_generation": 7, "apps": [], "confirmed_absent_window_identity_refs": ["gone"]}},
        {"ok": True, "result": {"catalog_generation": 8, "apps": [{"windows": []}]}},
    )
    catalog = asyncio.run(backend.presentation_apps("bound"))
    assert catalog == ComputerAppCatalog(7, (), ("gone",))
    with pytest.raises(HelperTransportError, match="malformed app catalog"):
        asyncio.run(backend.presentation_apps("bound"))
    assert backend._catalog is catalog
    assert frames == [("presentation_apps", {"takeover_ref": "bound"})] * 2


def test_cancel_presentation_validates_the_normal_terminal_outcome():
    outcome = {"started": True, "restoration": "preserved_user_focus"}
    backend, frames = _presentation_backend(
        {"ok": True, "result": outcome},
        {"ok": True, "result": {"started": False, "restoration": "not_started"}},
    )
    assert asyncio.run(backend.cancel_presentation("bound")) == outcome
    with pytest.raises(HelperTransportError, match="malformed takeover outcome"):
        asyncio.run(backend.cancel_presentation("bound"))
    assert frames == [("cancel_presentation", {"takeover_ref": "bound"})] * 2


def test_presentation_v2_end_marker_only_accepts_literal_true_with_explicit_keep():
    assert observation_capabilities({'observation_capabilities': ['focus_presentation_v2']}) == ['focus_presentation_v2']
    base = {'takeover_ref': 'bound', 'restore_previous_focus': False, 'require_presentation_status': True}
    assert json.loads(encode_request(ComputerRequest('end', 'takeover_end', base)))['payload'] == base
    for payload in ({'takeover_ref': 'bound', 'require_presentation_status': True}, {**base, 'restore_previous_focus': True},
                    *({**base, 'require_presentation_status': v} for v in [None, False, 1, 'true'])):
        with pytest.raises(ValueError):
            ComputerRequest('end', 'takeover_end', payload)


@pytest.mark.parametrize('presented', [True, False])
def test_presentation_v2_replies_are_bound_to_the_requested_end_only(presented):
    outcome = {'started': True, 'restoration': 'preserved_user_focus', 'presented': presented}
    backend, frames = _presentation_backend(*[{'ok': True, 'result': outcome} for _ in range(3)])
    assert asyncio.run(backend.end_takeover('bound', restore_previous_focus=False, require_presentation_status=True)) == outcome
    assert frames[0] == ('takeover_end', {'takeover_ref': 'bound', 'restore_previous_focus': False, 'require_presentation_status': True})
    with pytest.raises(HelperTransportError, match='malformed takeover outcome'):
        asyncio.run(backend.end_takeover('bound', restore_previous_focus=False))
    with pytest.raises(HelperTransportError, match='malformed takeover outcome'):
        asyncio.run(backend.cancel_presentation('bound'))


@pytest.mark.parametrize("operation", ["presentation_apps", "cancel_presentation"])
def test_presentation_rejection_does_not_fall_back_to_apps_or_end(operation):
    error = ComputerError(ComputerErrorCode.PROTOCOL_MISMATCH, "operation is not supported")
    backend, frames = _presentation_backend({"ok": False, "error": error})
    with pytest.raises(HelperApplicationError) as failure:
        asyncio.run(getattr(backend, operation)("bound"))
    assert failure.value.error == error
    assert frames == [(operation, {"takeover_ref": "bound"})]


def test_terminal_receipt_with_unexpected_snapshot_is_not_result_only_and_never_proves_display():
    outcome = {'started': True, 'restoration': 'preserved_user_focus', 'presented': True}
    backend, frames = _presentation_backend({'ok': True, 'result': outcome, 'snapshot': ComputerSnapshot('unexpected', {})})
    with pytest.raises(HelperTransportError, match='unexpected snapshot'):
        asyncio.run(backend.end_takeover('bound', restore_previous_focus=False, require_presentation_status=True))
    assert len(frames) == 1


def test_request_has_stable_version_and_id():
    request = ComputerRequest(
        request_id="req-1",
        operation="act",
        payload={
            "interaction_mode": "background",
            "snapshot_id": "snap-1",
            "plan_ref": "plan-1",
            "actions": [{"type": "click", "element_ref": "ax-1"}],
        },
    )
    encoded = encode_request(request)
    assert '"protocol_version":4' in encoded
    assert '"request_id":"req-1"' in encoded


def test_get_app_state_accepts_only_the_two_exact_catalog_bound_payload_variants():
    off = {
        "app_ref": "app_a",
        "window_ref": "win_a",
        "catalog_generation": 7,
        "scope": "target_window",
        "artifact_name": "snapshot-token.png",
    }
    on = {
        **off,
        "text_detail": "on",
        "text_detail_artifact_name": "snapshot-token.ax.json",
    }
    display = {**off, "scope": "display"}

    assert "get_app_state" in computer_protocol.COMPUTER_OPERATIONS
    assert json.loads(encode_request(ComputerRequest("get-off", "get_app_state", off)))["payload"] == off
    assert json.loads(encode_request(ComputerRequest("get-on", "get_app_state", on)))["payload"] == on
    assert json.loads(encode_request(ComputerRequest("get-display", "get_app_state", display)))["payload"] == display


@pytest.mark.parametrize(
    "payload",
    [
        {"window_ref": "win_a", "catalog_generation": 7, "scope": "target_window", "artifact_name": "snapshot-token.png"},
        {"app_ref": "app_a", "catalog_generation": 7, "scope": "target_window", "artifact_name": "snapshot-token.png"},
        {"app_ref": "app_a", "window_ref": "win_a", "scope": "target_window", "artifact_name": "snapshot-token.png"},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": 7, "artifact_name": "snapshot-token.png"},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": 7, "scope": "target_window"},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": 0, "scope": "target_window", "artifact_name": "snapshot-token.png"},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": -1, "scope": "target_window", "artifact_name": "snapshot-token.png"},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": 7.0, "scope": "target_window", "artifact_name": "snapshot-token.png"},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": True, "scope": "target_window", "artifact_name": "snapshot-token.png"},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": 7, "scope": "target_window", "artifact_name": "snapshot-token.png", "unexpected": True},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": 7, "scope": "target_window", "artifact_name": "snapshot-token.png", "text_detail": "off", "text_detail_artifact_name": "snapshot-token.ax.json"},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": 7, "scope": "target_window", "artifact_name": "snapshot-token.png", "text_detail": "on", "text_detail_artifact_name": "snapshot-other.ax.json"},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": 7, "scope": "target_window", "artifact_name": "../snapshot-token.png"},
        {"app_ref": "app_a", "window_ref": "win_a", "catalog_generation": 7, "scope": "target_window", "artifact_name": "snapshot-token.png", "text_detail": "on", "text_detail_artifact_name": "../snapshot-token.ax.json"},
    ],
)
def test_get_app_state_rejects_every_noncanonical_catalog_bound_payload(payload):
    with pytest.raises(ValueError, match="get_app_state|catalog_generation|artifact"):
        ComputerRequest("bad-get", "get_app_state", payload)


@pytest.mark.parametrize("code", ["stale_target", "snapshot_failed"])
def test_get_app_state_bounded_native_errors_decode_as_distinct_codes(code):
    response = decode_response(json.dumps({
        "protocol_version": 4,
        "request_id": "get-error",
        "ok": False,
        "error": {"code": code, "message": "bounded failure"},
    }))

    assert response.error is not None
    assert response.error.code.value == code


@pytest.mark.parametrize("fields", [{}, {"require_read_only_focus": True}])
def test_read_only_focus_plan_marker_is_optional(fields):
    payload = {"snapshot_id": "snap", "interaction_mode": "background",
               "actions": [{"type": "type", "text": "text", "element_ref": "field"}], **fields}
    assert json.loads(encode_request(ComputerRequest("plan", "plan_actions", payload)))["payload"] == payload


@pytest.mark.parametrize("fields", [
    {"require_read_only_focus": False}, {"require_read_only_focus": 1},
    {"require_read_only_focus": "true"}, {"require_read_only_focus": None},
    {"require_read_only_focus": True, "unexpected": True},
])
def test_read_only_focus_plan_marker_does_not_relax_exact_shape(fields):
    with pytest.raises(ValueError):
        ComputerRequest("plan", "plan_actions", {
            "snapshot_id": "snap", "interaction_mode": "background", "actions": [], **fields,
        })


def test_protocol_v4_round_trips_strict_fragment_plan_stage_and_commit_payloads():
    requirement = ForegroundFragmentActionRequirement(
        backend="pid_pointer",
        action_class="click",
        intent="pointer:click",
    )
    declaration = ForegroundFragmentDeclaration(
        fragment_hash="a" * 64,
        stages=(ForegroundFragmentStageDeclaration("0" * 64, 1, (requirement,)),),
        max_actions=1,
        wall_clock_limit_ms=1_000,
        restore_previous_focus=True,
    )
    initial = ComputerRequest(
        "fragment-initial",
        "plan_actions",
        {
            "interaction_mode": "foreground_takeover",
            "snapshot_id": "snapshot-0",
            "actions": [{"type": "click", "element_ref": "button"}],
            "fragment": ForegroundFragmentPlanRequest.stage(
                FragmentStageAuthority("a" * 64, 0, "0" * 64, "snapshot-0")
            ),
        },
    )
    stage = FragmentStageAuthority("a" * 64, 0, "0" * 64, "snapshot-0")
    act = ComputerRequest(
        "fragment-act",
        "act",
        {
            "interaction_mode": "foreground_takeover",
            "snapshot_id": "snapshot-0",
            "plan_ref": "plan-0",
            "takeover_ref": "takeover-0",
            "fragment_hash": stage.fragment_hash,
            "stage_index": stage.stage_index,
            "stage_hash": stage.stage_hash,
            "actions": [{"type": "click", "element_ref": "button"}],
        },
    )
    commit = FragmentStageCommit(
        takeover_ref="takeover-0",
        fragment_hash="a" * 64,
        stage_index=0,
        stage_hash="0" * 64,
        plan_ref="plan-0",
        fresh_snapshot_id="snapshot-1",
        postcondition_verified=True,
    )
    commit_request = ComputerRequest(
        "fragment-commit", "fragment_stage_commit", commit.to_mapping()
    )
    begin = ComputerRequest(
        "fragment-begin",
        "takeover_begin",
        {"snapshot_id": "snapshot-0", "plan_ref": "plan-0", "fragment": declaration, "focus_consent": _FIXTURE_FOCUS_CONSENT},
    )

    assert json.loads(encode_request(initial))["payload"]["fragment_hash"] == "a" * 64
    assert json.loads(encode_request(act))["payload"]["stage_index"] == 0
    assert json.loads(encode_request(commit_request))["payload"] == commit.to_mapping()
    assert json.loads(encode_request(begin))["payload"]["fragment"] == declaration.to_mapping()


def test_fragment_keyboard_requirements_match_native_canonical_key_chord_vectors():
    vector_path = (
        Path(__file__).resolve().parents[1]
        / "native/macos-computer-helper/Tests/Fixtures/key_chord_vectors.json"
    )
    vectors = json.loads(vector_path.read_text(encoding="utf-8"))

    for chord in vectors["valid"]:
        requirement = ForegroundFragmentActionRequirement(
            "foreground_keyboard", "text", f"key_chord:{chord}"
        )
        assert requirement.intent == f"key_chord:{chord}"
    for chord in vectors["invalid"]:
        with pytest.raises(ValueError, match="fragment requirement"):
            ForegroundFragmentActionRequirement(
                "foreground_keyboard", "text", f"key_chord:{chord}"
            )


@pytest.mark.parametrize("backend", ["ax_increment", "ax_decrement"])
def test_fragment_accepts_exact_background_ax_scroll_backends(backend):
    requirement = ForegroundFragmentActionRequirement(backend, "scroll", None)

    assert requirement.to_mapping() == {
        "backend": backend,
        "action_class": "scroll",
        "intent": None,
    }


@pytest.mark.parametrize(
    ("backend", "action_class", "intent"),
    [
        ("ax_increment", "press", None),
        ("ax_decrement", "scroll", "pointer:scroll"),
    ],
)
def test_fragment_rejects_inconsistent_background_ax_scroll_backends(
    backend,
    action_class,
    intent,
):
    with pytest.raises(ValueError, match="fragment requirement"):
        ForegroundFragmentActionRequirement(backend, action_class, intent)


def test_protocol_v4_keeps_ordinary_foreground_act_without_fragment_binding():
    request = ComputerRequest("ordinary-foreground", "act", {
        "interaction_mode": "foreground_takeover",
        "snapshot_id": "snapshot",
        "plan_ref": "plan",
        "takeover_ref": "takeover",
        "actions": [],
    })

    assert json.loads(encode_request(request))["payload"] == request.payload


@pytest.mark.parametrize(
    "payload",
    [
        {
            "interaction_mode": "background",
            "snapshot_id": "snapshot",
            "plan_ref": "plan",
            "actions": [],
            "fragment_hash": "a" * 64,
            "stage_index": 0,
            "stage_hash": "0" * 64,
        },
    ],
)
def test_protocol_v4_rejects_fragment_stage_on_background_act(payload):
    with pytest.raises(ValueError, match="fragment|exactly"):
        ComputerRequest("fragment-shape", "act", payload)


@pytest.mark.parametrize(
    "declaration",
    [
        {"fragment_hash": "A" * 64, "stages": [], "max_actions": 1, "wall_clock_limit_ms": 1, "restore_previous_focus": True},
        {"fragment_hash": "a" * 64, "stages": [], "max_actions": 65, "wall_clock_limit_ms": 1, "restore_previous_focus": True},
        {"fragment_hash": "a" * 64, "stages": [], "max_actions": 1, "wall_clock_limit_ms": 120_001, "restore_previous_focus": True},
    ],
)
def test_protocol_v4_rejects_malformed_or_excessive_fragment_declaration(declaration):
    with pytest.raises(ValueError, match="fragment|stage|actions|wall"):
        ForegroundFragmentDeclaration.from_mapping(declaration)


@pytest.mark.parametrize("operation", ["focus", "unknown", "ACT"])
def test_request_rejects_operations_outside_protocol_v2_allowlist(operation):
    with pytest.raises(ValueError, match="operation"):
        ComputerRequest("req-unknown", operation, {})


def test_wrong_version_becomes_typed_failure():
    result = decode_response(
        '{"protocol_version":1,"request_id":"req-1","ok":false,'
        '"error":{"code":"bad","message":"bad"}}'
    )
    assert result.error.code is ComputerErrorCode.PROTOCOL_MISMATCH


@pytest.mark.parametrize("version", [True, False, 1.0, 1, 3, "1", None])
def test_non_integer_or_wrong_response_version_becomes_typed_failure(version):
    encoded = json.dumps({"protocol_version": version, "request_id": "req-1", "ok": True})
    result = decode_response(encoded)
    assert result.ok is False
    assert result.error.code is ComputerErrorCode.PROTOCOL_MISMATCH


@pytest.mark.parametrize(
    ("action_type", "fields"),
    [
        ("click", {"x": 1, "y": 2, "target_element_ref": "snapshot-1:canvas"}),
        ("double_click", {"x": 1, "y": 2, "target_element_ref": "snapshot-1:canvas"}),
        ("type", {"text": "hello"}),
        ("keypress", {"key": "ENTER"}),
        ("scroll", {"delta_x": 1, "delta_y": -2, "element_ref": "snapshot-1:canvas"}),
        (
            "drag",
            {
                "x": 1,
                "y": 2,
                "end_x": 3,
                "end_y": 4,
                "target_element_ref": "snapshot-1:canvas",
            },
        ),
        ("wait", {"duration_ms": 25}),
    ],
)
def test_action_accepts_every_supported_variant(action_type, fields):
    assert ComputerAction(type=action_type, **fields).type == action_type


def test_action_serializes_modifiers_as_a_wire_array():
    action = ComputerAction.from_mapping(
        {"type": "keypress", "key": "d", "modifiers": ["command", "shift"]}
    )

    serialized = action.to_mapping()

    assert serialized["modifiers"] == ["command", "shift"]
    ComputerRequest(
        request_id="req-modifiers",
        operation="act",
        payload={
            "interaction_mode": "background",
            "snapshot_id": "snap-modifiers",
            "plan_ref": "plan-modifiers",
            "actions": [serialized],
        },
    )


@pytest.mark.parametrize("encoded", ["not-json", "[]", '"text"'])
def test_decode_response_rejects_malformed_or_non_object_payload(encoded):
    with pytest.raises(ValueError):
        decode_response(encoded)


def test_decode_response_rejects_missing_request_id():
    with pytest.raises(ValueError, match="request_id"):
        decode_response('{"protocol_version":1,"ok":true}')


@pytest.mark.parametrize("result", [[], False, "status", 0])
def test_decode_response_never_returns_success_for_non_object_result(result):
    frame = json.dumps({"protocol_version": 4, "request_id": "matched", "ok": True, "result": result})
    with pytest.raises(ValueError, match="result must be an object"):
        decode_response(frame)


def test_decode_response_rejects_duplicate_and_unknown_fields():
    duplicate = ('{"protocol_version":1,"protocol_version":1,'
                 '"request_id":"req-1","ok":true}')
    with pytest.raises(ValueError, match="duplicate"):
        decode_response(duplicate)
    with pytest.raises(ValueError, match="unknown"):
        decode_response('{"protocol_version":1,"request_id":"req-1",'
                        '"ok":true,"extra":false}')


@pytest.mark.parametrize("coordinate", [math.inf, -math.inf, math.nan])
def test_action_rejects_non_finite_coordinates(coordinate):
    with pytest.raises(ValueError, match="finite"):
        ComputerAction(type="click", x=coordinate, y=0)


def test_action_rejects_oversized_text():
    with pytest.raises(ValueError, match="20,000"):
        ComputerAction(type="type", text="x" * 20_001)


def test_action_rejects_lone_surrogate_scalar():
    with pytest.raises(ValueError, match="20,000"):
        ComputerAction(type="type", text="\ud800")


def test_python_action_schema_matches_native_vectors():
    vector_path = Path(__file__).resolve().parents[1] / "native/macos-computer-helper/Tests/Fixtures/action_vectors.json"
    vectors = json.loads(vector_path.read_text(encoding="utf-8"))

    def expand(vector):
        action = dict(vector["action"])
        if repeat := vector.get("repeat"):
            action[repeat["field"]] = repeat["scalar"] * repeat["count"]
        return action

    for vector in vectors["valid"]:
        ComputerAction.from_mapping(expand(vector))
    for vector in vectors["invalid"]:
        with pytest.raises(ValueError, match=".+"):
            ComputerAction.from_mapping(expand(vector))


@pytest.mark.parametrize("element_ref", ["", 42, False])
def test_action_rejects_empty_or_non_string_element_ref(element_ref):
    with pytest.raises(ValueError, match="element_ref"):
        ComputerAction(type="click", x=1, y=2, element_ref=element_ref)


@pytest.mark.parametrize(
    "action",
    [
        {"type": "click", "x": 1, "y": 2},
        {"type": "double_click", "x": 1, "y": 2},
        {"type": "scroll", "x": 1, "y": 2, "delta_y": 3},
        {"type": "drag", "x": 1, "y": 2, "end_x": 3, "end_y": 4},
    ],
)
def test_raw_pointer_coordinates_defer_region_authority_to_native_foreground_plan(action):
    assert ComputerAction.from_mapping(action).target_element_ref is None


def test_protocol_v2_raw_pointer_uses_independent_target_element_ref():
    action = ComputerAction.from_mapping({
        "type": "click",
        "x": 10,
        "y": 20,
        "target_element_ref": "snapshot-1:canvas",
    })

    assert action.to_mapping() == {
        "type": "click",
        "x": 10,
        "y": 20,
        "target_element_ref": "snapshot-1:canvas",
    }


@pytest.mark.parametrize(
    "action",
    [
        {
            "type": "click",
            "x": 10,
            "y": 20,
            "element_ref": "snapshot-1:canvas",
        },
        {
            "type": "click",
            "x": 10,
            "y": 20,
            "element_ref": "snapshot-1:semantic",
            "target_element_ref": "snapshot-1:safe-region",
        },
    ],
)
def test_protocol_v2_rejects_coordinate_semantic_ref_and_mixed_refs(action):
    with pytest.raises(ValueError, match="target_element_ref|mixed"):
        ComputerAction.from_mapping(action)


def test_protocol_v2_preserves_element_ref_semantic_click():
    action = ComputerAction.from_mapping({
        "type": "click",
        "element_ref": "snapshot-1:button",
    })

    assert action.to_mapping() == {
        "type": "click",
        "element_ref": "snapshot-1:button",
    }


def test_coordinate_less_reference_less_scroll_is_rejected():
    with pytest.raises(ValueError, match="element_ref|target_element_ref"):
        ComputerAction.from_mapping({"type": "scroll", "delta_y": 10})


@pytest.mark.parametrize(
    "action",
    [
        {"type": "click", "x": 1, "element_ref": "snapshot-1:canvas"},
        {"type": "double_click", "y": 2, "element_ref": "snapshot-1:canvas"},
        {
            "type": "scroll",
            "x": 1,
            "delta_y": 3,
            "element_ref": "snapshot-1:canvas",
        },
    ],
)
def test_pointer_coordinate_pair_is_all_or_nothing_even_with_safe_region(action):
    with pytest.raises(ValueError, match="x/y"):
        ComputerAction.from_mapping(action)


@pytest.mark.parametrize("key", ["", 42, False])
def test_action_rejects_empty_or_non_string_key(key):
    with pytest.raises(ValueError, match="key"):
        ComputerAction(type="keypress", key=key)


def test_request_rejects_more_than_twenty_actions():
    with pytest.raises(ValueError, match="20 actions"):
        ComputerRequest(
            request_id="req-1",
            operation="act",
            payload={
                "interaction_mode": "background",
                "snapshot_id": "snap-1",
                "plan_ref": "plan-1",
                "actions": [{"type": "wait", "duration_ms": 1}] * 21,
            },
        )


def test_protocol_v4_validates_cooperative_act_and_takeover_outcome():
    request = ComputerRequest(
        "r1",
        "act",
        {
            "interaction_mode": "foreground_takeover",
            "snapshot_id": "s1",
            "plan_ref": "p1",
            "takeover_ref": "t1",
            "fragment_hash": "a" * 64,
            "stage_index": 0,
            "stage_hash": "0" * 64,
            "actions": [{
                "type": "click",
                "x": 10,
                "y": 20,
                "target_element_ref": "snapshot-1:canvas",
            }],
        },
    )

    assert json.loads(encode_request(request))["protocol_version"] == 4
    assert computer_protocol.ComputerInteractionMode.FOREGROUND_TAKEOVER.value == "foreground_takeover"
    assert computer_protocol.TakeoverRestoration.RESTORED.value == "restored"


@pytest.mark.parametrize("mode", ["", "foreground", "BACKGROUND", 1, None])
def test_protocol_v2_rejects_unknown_interaction_mode(mode):
    with pytest.raises(ValueError, match="interaction_mode"):
        ComputerRequest("r1", "act", {
            "interaction_mode": mode,
            "snapshot_id": "s1",
            "actions": [],
        })


@pytest.mark.parametrize("coordinate", [math.inf, -math.inf, math.nan, 1_000_001])
def test_protocol_v2_rejects_invalid_virtual_pointer_coordinates(coordinate):
    with pytest.raises(ValueError, match="finite"):
        ComputerAction(type="click", x=coordinate, y=0)


@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        ("select", {"app_ref": "a1", "window_ref": "w1"}),
        ("plan_actions", {
            "interaction_mode": "background",
            "snapshot_id": "s1",
            "actions": [{
                "type": "click",
                "x": 10,
                "y": 20,
                "target_element_ref": "snapshot-1:canvas",
            }],
        }),
        ("plan_actions", {
            "interaction_mode": "foreground_takeover",
            "snapshot_id": "s1",
            "actions": [{"type": "wait", "duration_ms": 0}],
        }),
        ("takeover_begin", {"snapshot_id": "s1", "plan_ref": "p1", "focus_consent": _FIXTURE_FOCUS_CONSENT}),
        ("takeover_begin", {
            "snapshot_id": "s1",
            "plan_ref": "p1",
            "focus_consent": _FIXTURE_FOCUS_CONSENT,
            "fragment": ForegroundFragmentDeclaration(
                "a" * 64,
                (ForegroundFragmentStageDeclaration(
                    "0" * 64,
                    1,
                    (ForegroundFragmentActionRequirement("wait", None, None),),
                ),),
                1,
                1_000,
                True,
            ),
        }),
        ("act", {
            "interaction_mode": "background",
            "snapshot_id": "s1",
            "plan_ref": "p1",
            "actions": [],
        }),
        ("act", {
            "interaction_mode": "foreground_takeover",
            "snapshot_id": "s1",
            "plan_ref": "p1",
            "takeover_ref": "t1",
            "fragment_hash": "a" * 64,
            "stage_index": 0,
            "stage_hash": "0" * 64,
            "actions": [],
        }),
        ("takeover_end", {"takeover_ref": "t1"}),
    ],
)
def test_protocol_v2_accepts_only_cooperative_request_shapes(operation, payload):
    assert ComputerRequest("r1", operation, payload).payload == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"interaction_mode": "background", "snapshot_id": "s1", "plan_ref": "p1", "takeover_ref": "t1", "actions": []},
        {"interaction_mode": "foreground_takeover", "snapshot_id": "s1", "plan_ref": "p1", "actions": []},
        {"interaction_mode": "background", "snapshot_id": "s1", "plan_ref": "p1", "actions": [], "unexpected": True},
    ],
)
def test_protocol_v2_rejects_invalid_act_request_shape(payload):
    with pytest.raises(ValueError, match="act"):
        ComputerRequest("r1", "act", payload)


def test_protocol_v2_rejects_legacy_act_without_cooperative_authority():
    with pytest.raises(ValueError, match="interaction_mode"):
        ComputerRequest("legacy", "act", {
            "app_ref": "app-1",
            "window_ref": "window-1",
            "snapshot_id": "snapshot-1",
            "actions": [],
        })


def test_protocol_v2_act_result_excludes_takeover_member_and_serialization():
    result = computer_protocol.ComputerActResult.from_mapping(
        {
            "outcomes": [],
            "last_acknowledged_action": -1,
        },
        action_count=0,
        response_ok=True,
        response_error=None,
        interaction_mode=computer_protocol.ComputerInteractionMode.FOREGROUND_TAKEOVER,
    )

    assert not hasattr(result, "takeover")
    assert result.to_mapping() == {
        "outcomes": [],
        "last_acknowledged_action": -1,
    }


def test_act_result_preserves_only_known_per_action_effect_verification():
    result = computer_protocol.ComputerActResult.from_mapping(
        {
            "outcomes": [
                {"index": 0, "ok": True, "effect_verification": "verified"},
                {"index": 1, "ok": True, "effect_verification": "noop"},
            ],
            "last_acknowledged_action": 1,
        },
        action_count=2,
        response_ok=True,
        response_error=None,
    )

    assert [outcome.effect_verification for outcome in result.outcomes] == [
        computer_protocol.ActionEffectVerification.VERIFIED,
        computer_protocol.ActionEffectVerification.NOOP,
    ]
    assert result.to_mapping()["outcomes"] == [
        {"index": 0, "ok": True, "effect_verification": "verified"},
        {"index": 1, "ok": True, "effect_verification": "noop"},
    ]


@pytest.mark.parametrize("effect", ["changed", "VERIFIED", "", 1, True, None])
def test_act_result_rejects_unknown_or_untyped_effect_verification(effect):
    with pytest.raises(ValueError, match="effect_verification"):
        computer_protocol.ComputerActResult.from_mapping(
            {
                "outcomes": [
                    {"index": 0, "ok": True, "effect_verification": effect},
                ],
                "last_acknowledged_action": 0,
            },
            action_count=1,
            response_ok=True,
            response_error=None,
        )


def test_failed_action_outcome_rejects_effect_verification():
    with pytest.raises(ValueError, match="effect_verification"):
        computer_protocol.ComputerActResult.from_mapping(
            {
                "outcomes": [{
                    "index": 0,
                    "ok": False,
                    "error_code": "unknown_outcome",
                    "effect_verification": "unverified",
                }],
                "last_acknowledged_action": -1,
            },
            action_count=1,
            response_ok=False,
            response_error=ComputerError(ComputerErrorCode.UNKNOWN_OUTCOME, "unknown"),
        )


def test_input_focus_required_is_a_strict_failed_action_wire_error():
    result = computer_protocol.ComputerActResult.from_mapping(
        {
            "outcomes": [
                {"index": 0, "ok": False, "error_code": "input_focus_required"},
            ],
            "last_acknowledged_action": -1,
        },
        action_count=1,
        response_ok=False,
        response_error=ComputerError(
            ComputerErrorCode.INPUT_FOCUS_REQUIRED,
            "focus mismatch",
        ),
    )

    assert result.outcomes[0].error_code is ComputerErrorCode.INPUT_FOCUS_REQUIRED


@pytest.mark.parametrize(
    ("response_ok", "response_error"),
    [
        (True, None),
        (False, ComputerError(ComputerErrorCode.HELPER_FAILED, "failed")),
    ],
)
def test_protocol_v2_foreground_act_rejects_premature_restoration(response_ok, response_error):
    with pytest.raises(ValueError, match="unknown fields"):
        computer_protocol.ComputerActResult.from_mapping(
            {
                "outcomes": [],
                "last_acknowledged_action": -1,
                "takeover": {"started": False, "restoration": "not_started"},
            },
            action_count=0,
            response_ok=response_ok,
            response_error=response_error,
            interaction_mode=computer_protocol.ComputerInteractionMode.FOREGROUND_TAKEOVER,
        )


@pytest.mark.parametrize("restoration", ["restored", "preserved_user_focus", "restore_failed"])
def test_protocol_v2_takeover_end_accepts_final_restoration(restoration):
    outcome = computer_protocol.TakeoverOutcome.from_mapping({
        "started": True,
        "restoration": restoration,
    })
    assert outcome.restoration.value == restoration


def test_user_activity_pause_preserves_unknown_boundary_after_posted_input():
    result = computer_protocol.ComputerActResult.from_mapping(
        {
            "outcomes": [{"index": 0, "ok": False, "error_code": "unknown_outcome"}],
            "last_acknowledged_action": -1,
        },
        action_count=1,
        response_ok=False,
        response_error=ComputerError(ComputerErrorCode.USER_ACTIVITY_PAUSED, "paused"),
        interaction_mode=computer_protocol.ComputerInteractionMode.FOREGROUND_TAKEOVER,
    )
    assert result.outcomes[0].error_code is ComputerErrorCode.UNKNOWN_OUTCOME


@pytest.mark.parametrize("label", ["keypress", "wait", "unknown", "PRESS"])
def test_protocol_v2_rejects_unknown_dispatch_capability_labels(label):
    with pytest.raises(ValueError, match="action_classes"):
        computer_protocol.DispatchPlanSummary.from_mapping({
            "plan_ref": "p1",
            "interaction_mode": "background",
            "requires_takeover": False,
            "reason": "reason",
            "action_classes": [label],
            "pid_action_classes": [],
            "last_acknowledged_action": -1,
        })


def test_protocol_v2_requires_strict_bounded_pid_action_classes():
    valid = {
        "plan_ref": "p1",
        "interaction_mode": "foreground_takeover",
        "requires_takeover": True,
        "reason": "foreground_takeover_required",
        "action_classes": ["press", "scroll"],
        "pid_action_classes": ["scroll"],
        "last_acknowledged_action": -1,
    }
    summary = computer_protocol.DispatchPlanSummary.from_mapping(valid)
    assert summary.pid_action_classes == ("scroll",)
    assert summary.to_mapping() == valid

    missing = dict(valid)
    missing.pop("pid_action_classes")
    with pytest.raises(ValueError, match="missing or unknown"):
        computer_protocol.DispatchPlanSummary.from_mapping(missing)

    extra = {**valid, "extra": True}
    with pytest.raises(ValueError, match="missing or unknown"):
        computer_protocol.DispatchPlanSummary.from_mapping(extra)

    for labels in (["scroll", "scroll"], ["unknown"], ["scroll"] * 7, ["click"]):
        with pytest.raises(ValueError, match="pid_action_classes"):
            computer_protocol.DispatchPlanSummary.from_mapping({
                **valid,
                "pid_action_classes": labels,
            })


def test_decode_response_rejects_oversized_input():
    with pytest.raises(ValueError, match="4 MiB"):
        decode_response(" " * (4 * 1024 * 1024 + 1))


def test_unknown_error_code_with_success_becomes_typed_failure():
    result = decode_response(
        '{"protocol_version":1,"request_id":"req-1","ok":true,'
        '"error":{"code":"not-real","message":"bad"}}'
    )
    assert result.ok is False
    assert result.error.code is ComputerErrorCode.PROTOCOL_MISMATCH


def test_failed_response_without_error_becomes_typed_failure():
    result = decode_response('{"protocol_version":1,"request_id":"req-1","ok":false}')
    assert result.ok is False
    assert result.error.code is ComputerErrorCode.PROTOCOL_MISMATCH


@pytest.mark.parametrize("version", [True, 1.0, 1, 3, "1"])
def test_public_response_rejects_non_integer_or_wrong_protocol_version(version):
    with pytest.raises(ValueError, match="protocol_version"):
        ComputerResponse(request_id="req-1", ok=True, protocol_version=version)


@pytest.mark.parametrize("snapshot", ["snapshot", {"snapshot_id": "snap-1"}])
def test_public_response_rejects_non_snapshot_snapshot(snapshot):
    with pytest.raises(ValueError, match="snapshot"):
        ComputerResponse(request_id="req-1", ok=True, snapshot=snapshot)


@pytest.mark.parametrize("error", ["error", {"code": "helper_failed"}])
def test_public_response_rejects_non_error_error(error):
    with pytest.raises(ValueError, match="error"):
        ComputerResponse(request_id="req-1", ok=False, error=error)


def test_public_response_accepts_typed_snapshot_and_error():
    response = ComputerResponse(
        request_id="req-1",
        ok=False,
        snapshot=ComputerSnapshot("snap-1"),
        error=ComputerError(ComputerErrorCode.HELPER_FAILED, "failed"),
    )
    assert response.snapshot.snapshot_id == "snap-1"


def test_encode_request_is_compact_utf8_json():
    encoded = encode_request(ComputerRequest("请求", "select", {"app_ref": "é", "window_ref": "w"}))
    assert json.loads(encoded)["request_id"] == "请求"
    assert " " not in encoded
    assert "é" in encoded


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {
            "image_artifact": "capture.png",
            "logical_size": {"width": 1, "height": 1},
            "pixel_size": {"width": 1, "height": 1},
            "backing_scale": 1,
            "visible_bounds": {"x": 0, "y": 0, "width": 1, "height": 1},
            "ax_tree": {},
        },
        {
            "image_artifact": "capture.png",
            "logical_size": {"width": 1, "height": 1},
            "pixel_size": {"width": 1, "height": 1},
            "backing_scale": 1,
            "capture_bounds": {"x": 0, "y": 0, "width": 1, "height": 0},
            "ax_tree": {},
        },
        {
            "image_artifact": "capture.png",
            "logical_size": {"width": 1, "height": 1},
            "pixel_size": {"width": 1, "height": 1},
            "backing_scale": 1,
            "capture_bounds": {"x": 0, "y": 0, "width": 1, "height": 1},
            "ax_tree": {},
            "unexpected": True,
        },
    ],
)
def test_wire_snapshot_rejects_missing_legacy_malformed_and_unknown_payload_fields(payload):
    encoded = json.dumps({
            "protocol_version": 4,
        "request_id": "snapshot",
        "ok": True,
        "snapshot": {"snapshot_id": "snap-1", "payload": payload},
    })

    with pytest.raises(ValueError, match="snapshot payload"):
        decode_response(encoded)


def test_wire_snapshot_accepts_strict_capture_bounds_schema():
    payload = {
        "image_artifact": "capture.png",
        "logical_size": {"width": 2, "height": 3},
        "pixel_size": {"width": 4, "height": 6},
        "backing_scale": 2,
        "capture_bounds": {"x": 0, "y": 0, "width": 2, "height": 3},
        "ax_tree": {"role": "AXWindow"},
    }
    response = decode_response(json.dumps({
        "protocol_version": 4,
        "request_id": "snapshot",
        "ok": True,
        "snapshot": {"snapshot_id": "snap-1", "payload": payload},
    }))

    assert response.snapshot.payload == payload


@pytest.mark.parametrize(
    "display_metadata",
    [
        {"display_id": 7},
        {"target_window_bounds": {"x": 10, "y": 20, "width": 100, "height": 80}},
    ],
)
def test_wire_snapshot_rejects_partial_display_metadata(display_metadata):
    payload = {
        "image_artifact": "capture.png",
        "logical_size": {"width": 200, "height": 100},
        "pixel_size": {"width": 400, "height": 200},
        "backing_scale": 2,
        "capture_bounds": {"x": 0, "y": 0, "width": 200, "height": 100},
        "ax_tree": {"role": "AXWindow"},
        **display_metadata,
    }
    encoded = json.dumps({
        "protocol_version": 4,
        "request_id": "snapshot",
        "ok": True,
        "snapshot": {"snapshot_id": "snap-display", "payload": payload},
    })

    with pytest.raises(ValueError, match="display_id.*target_window_bounds"):
        decode_response(encoded)


def test_wire_snapshot_accepts_cursor_visibility_hint():
    payload = {
        "image_artifact": "capture.png",
        "logical_size": {"width": 200, "height": 100},
        "pixel_size": {"width": 400, "height": 200},
        "backing_scale": 2,
        "capture_bounds": {"x": 0, "y": 0, "width": 200, "height": 100},
        "ax_tree": {"role": "AXWindow"},
        "cursor_visible": False,
    }
    encoded = json.dumps({
        "protocol_version": 4,
        "request_id": "snapshot",
        "ok": True,
        "snapshot": {"snapshot_id": "snap-cursor", "payload": payload},
    })

    response = decode_response(encoded)
    assert response.snapshot is not None
    assert response.snapshot.payload["cursor_visible"] is False


def test_wire_snapshot_accepts_only_registered_default_button_disclosure():
    payload = _snapshot_payload(
        ax_tree={
            "role": "AXWindow",
            "element_ref": "snapshot_detail:window",
            "children": [{
                "role": "AXButton",
                "element_ref": "snapshot_detail:default",
            }],
        },
        has_default_button=True,
        default_button_element_ref="snapshot_detail:default",
    )

    response = decode_response(_encoded_snapshot(payload))

    assert response.snapshot is not None
    assert response.snapshot.payload["has_default_button"] is True
    assert response.snapshot.payload["default_button_element_ref"] == "snapshot_detail:default"


@pytest.mark.parametrize(
    "extra",
    [
        {"has_default_button": "yes"},
        {"default_button_element_ref": "snapshot_detail:default"},
        {"has_default_button": False, "default_button_element_ref": "snapshot_detail:default"},
        {"has_default_button": True, "default_button_element_ref": "snapshot_detail:missing"},
    ],
)
def test_wire_snapshot_rejects_untrusted_default_button_disclosure(extra):
    with pytest.raises(ValueError, match="default button"):
        decode_response(_encoded_snapshot(_snapshot_payload(**extra)))


def test_wire_snapshot_accepts_native_registered_default_button_ref_beyond_public_ax_depth():
    reference = "snapshot_detail:deep-default"
    tree = {"role": "AXButton", "element_ref": reference}
    for _ in range(8):
        tree = {"role": "AXGroup", "children": [tree]}

    response = decode_response(_encoded_snapshot(_snapshot_payload(
        ax_tree=tree,
        has_default_button=True,
        default_button_element_ref=reference,
    )))

    assert response.snapshot is not None
    assert response.snapshot.payload["default_button_element_ref"] == reference


def test_wire_snapshot_accepts_native_registered_default_button_ref_beyond_public_ax_node_budget():
    reference = "snapshot_detail:late-default"
    tree = {
        "role": "AXWindow",
        "children": [
            {"role": "AXGroup"}
            for _ in range(900)
        ] + [{"role": "AXButton", "element_ref": reference}],
    }

    response = decode_response(_encoded_snapshot(_snapshot_payload(
        ax_tree=tree,
        has_default_button=True,
        default_button_element_ref=reference,
    )))

    assert response.snapshot is not None
    assert response.snapshot.payload["default_button_element_ref"] == reference


def test_wire_snapshot_rejects_default_button_ref_outside_ax_children_topology():
    reference = "snapshot_detail:metadata-default"
    tree = {
        "role": "AXWindow",
        "metadata": {"element_ref": reference},
    }

    with pytest.raises(ValueError, match="default button"):
        decode_response(_encoded_snapshot(_snapshot_payload(
            ax_tree=tree,
            has_default_button=True,
            default_button_element_ref=reference,
        )))


def _snapshot_payload(**extra):
    return {
        "image_artifact": "snapshot-0123456789abcdef0123456789abcdef.png",
        "logical_size": {"width": 2, "height": 3},
        "pixel_size": {"width": 4, "height": 6},
        "backing_scale": 2,
        "capture_bounds": {"x": 0, "y": 0, "width": 2, "height": 3},
        "ax_tree": {"role": "AXWindow"},
        **extra,
    }


def _text_detail_metadata(**overrides):
    return {
        "schema_version": 1,
        "snapshot_id": "snapshot_detail",
        "coverage": "reported_ax_subtree",
        "node_count": 1,
        "max_depth_observed": 0,
        "byte_count": 128,
        "sha256": "a" * 64,
        "truncated": False,
        "truncation_reasons": [],
        **overrides,
    }


def _encoded_snapshot(payload, *, version=4):
    return json.dumps({
        "protocol_version": version,
        "request_id": "snapshot",
        "ok": True,
        "snapshot": {"snapshot_id": "snapshot_detail", "payload": payload},
    })


_SNAPSHOT_FILENAME_VECTORS = json.loads((
    Path(__file__).resolve().parents[1]
    / "native/macos-computer-helper/Tests/Fixtures/snapshot_artifact_filename_vectors.json"
).read_text(encoding="utf-8"))


def _filename_vector_value(vector):
    if "json_string" in vector:
        return json.loads(vector["json_string"])
    if "repeat" in vector:
        return vector["repeat"] * vector["count"] + vector.get("suffix", "")
    return vector["name"]


@pytest.mark.parametrize(
    "vector",
    _SNAPSHOT_FILENAME_VECTORS["plain_names"],
    ids=lambda vector: vector["label"],
)
def test_snapshot_plain_artifact_filename_matches_shared_adversarial_vectors(vector):
    artifact_name = _filename_vector_value(vector)
    payload = {
        "app_ref": "app",
        "window_ref": "window",
        "scope": "target_window",
        "artifact_name": artifact_name,
    }

    if vector["valid"]:
        ComputerRequest("filename", "snapshot", payload)
        decode_response(_encoded_snapshot(_snapshot_payload(image_artifact=artifact_name)))
    else:
        with pytest.raises(ValueError, match="plain filename"):
            ComputerRequest("filename", "snapshot", payload)
        with pytest.raises(ValueError, match="plain filename"):
            decode_response(_encoded_snapshot(_snapshot_payload(image_artifact=artifact_name)))


@pytest.mark.parametrize(
    "vector",
    _SNAPSHOT_FILENAME_VECTORS["detail_pairs"],
    ids=lambda vector: vector["label"],
)
def test_snapshot_detail_filename_matches_shared_adversarial_vectors(vector):
    request_payload = {
        "app_ref": "app",
        "window_ref": "window",
        "scope": "target_window",
        "artifact_name": vector["image"],
        "text_detail": "on",
        "text_detail_artifact_name": vector["detail"],
    }
    response_payload = _snapshot_payload(
        image_artifact=vector["image"],
        text_detail_artifact=vector["detail"],
        text_detail_metadata=_text_detail_metadata(),
    )

    if vector["valid"]:
        ComputerRequest("detail-filename", "snapshot", request_payload)
        decode_response(_encoded_snapshot(response_payload))
    else:
        with pytest.raises(ValueError, match="text detail artifact"):
            ComputerRequest("detail-filename", "snapshot", request_payload)
        with pytest.raises(ValueError, match="text detail artifact"):
            decode_response(_encoded_snapshot(response_payload))


@pytest.mark.parametrize(
    "vector",
    _SNAPSHOT_FILENAME_VECTORS["truncation_reason_vectors"],
    ids=lambda vector: vector["label"],
)
def test_snapshot_truncation_reasons_match_shared_canonical_order_vectors(vector):
    payload = _snapshot_payload(
        text_detail_artifact="snapshot-0123456789abcdef0123456789abcdef.ax.json",
        text_detail_metadata=_text_detail_metadata(
            truncated=vector["truncated"],
            truncation_reasons=vector["reasons"],
        ),
    )

    if vector["valid"]:
        decode_response(_encoded_snapshot(payload))
    else:
        with pytest.raises(ValueError, match="truncation_reasons"):
            decode_response(_encoded_snapshot(payload))


def test_snapshot_default_off_request_preserves_the_exact_old_wire_shape():
    payload = {
        "app_ref": "app",
        "window_ref": "window",
        "scope": "target_window",
        "artifact_name": "snapshot-0123456789abcdef0123456789abcdef.png",
    }

    encoded = json.loads(encode_request(ComputerRequest("off", "snapshot", payload)))

    assert encoded["payload"] == payload
    with pytest.raises(ValueError, match="snapshot requires exactly"):
        ComputerRequest("explicit-off", "snapshot", {**payload, "text_detail": "off"})


def test_snapshot_text_detail_on_requires_the_exact_plain_artifact_filename():
    payload = {
        "app_ref": "app",
        "window_ref": "window",
        "scope": "target_window",
        "artifact_name": "snapshot-0123456789abcdef0123456789abcdef.png",
        "text_detail": "on",
        "text_detail_artifact_name": "snapshot-0123456789abcdef0123456789abcdef.ax.json",
    }

    assert json.loads(encode_request(ComputerRequest("on", "snapshot", payload)))["payload"] == payload

    for filename in (
        "snapshot-0123456789abcdef0123456789abcdef.json",
        "../snapshot-0123456789abcdef0123456789abcdef.ax.json",
        "snapshot-.ax.json",
        "snapshot-fedcba9876543210fedcba9876543210.ax.json",
    ):
        with pytest.raises(ValueError, match="text detail artifact"):
            ComputerRequest("bad", "snapshot", {**payload, "text_detail_artifact_name": filename})


@pytest.mark.parametrize(
    "mutation",
    [
        {"text_detail_artifact": "snapshot-0123456789abcdef0123456789abcdef.ax.json"},
        {"text_detail_metadata": _text_detail_metadata()},
        {
            "text_detail_artifact": "snapshot-0123456789abcdef0123456789abcdef.ax.json",
            "text_detail_metadata": _text_detail_metadata(unexpected=True),
        },
    ],
)
def test_snapshot_text_detail_response_rejects_partial_or_unknown_fields(mutation):
    with pytest.raises(ValueError, match="text detail"):
        decode_response(_encoded_snapshot(_snapshot_payload(**mutation)))


def test_snapshot_text_detail_response_accepts_paired_bounded_schema_one_metadata():
    detail_name = "snapshot-0123456789abcdef0123456789abcdef.ax.json"
    payload = _snapshot_payload(
        text_detail_artifact=detail_name,
        text_detail_metadata=_text_detail_metadata(),
    )

    response = decode_response(_encoded_snapshot(payload))

    assert response.snapshot is not None
    assert response.snapshot.payload["text_detail_artifact"] == detail_name
    assert response.snapshot.payload["text_detail_metadata"]["schema_version"] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("node_count", True),
        ("node_count", 1.0),
        ("node_count", 4001),
        ("max_depth_observed", 21),
        ("byte_count", 8 * 1024 * 1024 + 1),
        ("snapshot_id", "x" * 257),
        ("sha256", "A" * 64),
        ("truncation_reasons", ["depth_limit"] * 8),
        ("truncation_reasons", ["x" * 65]),
    ],
)
def test_snapshot_text_detail_metadata_enforces_strict_types_and_bounds(field, value):
    payload = _snapshot_payload(
        text_detail_artifact="snapshot-0123456789abcdef0123456789abcdef.ax.json",
        text_detail_metadata=_text_detail_metadata(**{field: value}),
    )

    with pytest.raises(ValueError, match="text detail"):
        decode_response(_encoded_snapshot(payload))


def test_snapshot_text_detail_metadata_rejects_duplicate_keys():
    encoded = _encoded_snapshot(_snapshot_payload(
        text_detail_artifact="snapshot-0123456789abcdef0123456789abcdef.ax.json",
        text_detail_metadata=_text_detail_metadata(),
    )).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')

    with pytest.raises(ValueError, match="duplicate"):
        decode_response(encoded)


def _detail_envelope(snapshot_id="snapshot_detail", **overrides):
    value = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "coverage": "reported_ax_subtree",
        "limits": {
            "maximum_depth": 20,
            "maximum_nodes": 4_000,
            "maximum_structural_string_bytes": 4_096,
            "maximum_value_bytes": 262_144,
            "maximum_aggregate_text_bytes": 4_194_304,
            "maximum_final_bytes": 8_388_608,
            "wall_clock_ms": 5_000,
        },
        "stats": {
            "node_count": 1,
            "max_depth_observed": 0,
            "truncated": False,
            "truncation_reasons": [],
        },
        "root": {
            "node_id": "node_0",
            "role": "AXWindow",
            "subrole": "AXStandardWindow",
            "title": "Document",
        },
    }
    return {**value, **overrides}


def _decode_detail_root(root):
    data = json.dumps(_detail_envelope(root=root), separators=(",", ":")).encode()
    return computer_protocol.decode_text_detail_envelope(
        data,
        snapshot_id="snapshot_detail",
        metadata=_text_detail_metadata(byte_count=len(data), sha256="b" * 64),
        sha256="b" * 64,
    )


def test_strict_text_detail_envelope_decoder_accepts_exact_schema_and_cross_checks_metadata():
    envelope = json.dumps(_detail_envelope(), separators=(",", ":")).encode()
    metadata = _text_detail_metadata(
        byte_count=len(envelope),
        sha256="b" * 64,
    )

    decoded = computer_protocol.decode_text_detail_envelope(
        envelope,
        snapshot_id="snapshot_detail",
        metadata=metadata,
        sha256="b" * 64,
    )

    assert decoded["root"]["title"] == "Document"


@pytest.mark.parametrize(
    "identity",
    [
        {"role": "", "subrole": "AXTextField"},
        {"role": "AXTextField", "subrole": ""},
        {"role": "AXUnknown", "subrole": "AXTextField"},
        {"role": "AXTextField", "subrole": "AXUnknown"},
        {"role": "AXTextField"},
    ],
)
def test_strict_text_detail_indeterminate_identity_requires_true_redaction_without_value(
    identity,
):
    root = {"node_id": "node_0", **identity}

    decoded = _decode_detail_root({**root, "redacted": True})
    assert decoded["root"]["redacted"] is True
    assert "value" not in decoded["root"]

    with pytest.raises(ValueError, match="redacted|redaction"):
        _decode_detail_root(root)
    with pytest.raises(ValueError, match="redacted|redaction"):
        _decode_detail_root({**root, "redacted": False})
    with pytest.raises(ValueError, match="value"):
        _decode_detail_root({**root, "redacted": True, "value": "secret"})


@pytest.mark.parametrize("subrole", ["AXStandardTextField", "AXSearchField"])
def test_strict_text_detail_complete_nonsecure_identity_rejects_redacted_marker(subrole):
    root = {
        "node_id": "node_0",
        "role": "AXTextField",
        "subrole": subrole,
    }

    decoded = _decode_detail_root({**root, "value": "ordinary"})
    assert decoded["root"]["value"] == "ordinary"
    with pytest.raises(ValueError, match="redacted|redaction"):
        _decode_detail_root({**root, "redacted": True})


@pytest.mark.parametrize(
    "envelope",
    [
        b'{"schema_version":1,"schema_version":1}',
        json.dumps(_detail_envelope(root={"element_ref": "actionable"})).encode(),
        json.dumps(_detail_envelope(root={
            "node_id": "node_0", "role": "AXSecureTextField",
            "subrole": "AXSecureTextField", "value": "secret",
        })).encode(),
        json.dumps(_detail_envelope(stats={
            "node_count": 1.0, "max_depth_observed": 0,
            "truncated": False, "truncation_reasons": [],
        })).encode(),
    ],
)
def test_strict_text_detail_envelope_decoder_rejects_duplicate_refs_redaction_and_noninteger(envelope):
    with pytest.raises(ValueError):
        computer_protocol.decode_text_detail_envelope(
            envelope,
            snapshot_id="snapshot_detail",
            metadata=_text_detail_metadata(byte_count=len(envelope), sha256="b" * 64),
            sha256="b" * 64,
        )


@pytest.mark.parametrize(
    "envelope",
    [
        _detail_envelope(limits={
            "maximum_depth": True,
            "maximum_nodes": 4_000,
            "maximum_structural_string_bytes": 4_096,
            "maximum_value_bytes": 262_144,
            "maximum_aggregate_text_bytes": 4_194_304,
            "maximum_final_bytes": 8_388_608,
            "wall_clock_ms": 5_000,
        }),
        _detail_envelope(stats={
            "node_count": 2, "max_depth_observed": 0,
            "truncated": False, "truncation_reasons": [],
        }),
        _detail_envelope(stats={
            "node_count": 1, "max_depth_observed": 1,
            "truncated": False, "truncation_reasons": [],
        }),
        _detail_envelope(stats={
            "node_count": 1, "max_depth_observed": 0,
            "truncated": True, "truncation_reasons": ["node_limit", "depth_limit"],
        }),
        _detail_envelope(root={
            "node_id": "node_0", "role": "AXWindow", "subrole": "AXWindow",
            "bounds": {"x": False, "y": 0, "width": 1, "height": 1},
        }),
        _detail_envelope(root={
            "node_id": "node_0", "role": "AXWindow", "subrole": "AXWindow",
            "title": "x" * 4_097,
        }),
        _detail_envelope(root={
            "node_id": "node_0", "role": "AXWindow", "subrole": "AXWindow",
            "redacted": False,
        }),
        _detail_envelope(root={
            "node_id": "node_0", "role": "AXWindow", "subrole": "AXWindow",
            "children": [],
        }),
    ],
)
def test_strict_text_detail_envelope_decoder_enforces_limits_stats_budgets_and_geometry(envelope):
    data = json.dumps(envelope, separators=(",", ":")).encode()
    stats = envelope["stats"]
    metadata = _text_detail_metadata(
        node_count=stats["node_count"],
        max_depth_observed=stats["max_depth_observed"],
        truncated=stats["truncated"],
        truncation_reasons=stats["truncation_reasons"],
        byte_count=len(data),
        sha256="b" * 64,
    )
    with pytest.raises(ValueError):
        computer_protocol.decode_text_detail_envelope(
            data,
            snapshot_id="snapshot_detail",
            metadata=metadata,
            sha256="b" * 64,
        )


@pytest.mark.parametrize(
    "metadata_override",
    [
        {"byte_count": 1},
        {"sha256": "c" * 64},
        {"node_count": 0},
        {"max_depth_observed": 1},
    ],
)
def test_strict_text_detail_envelope_decoder_cross_checks_all_authoritative_metadata(
    metadata_override,
):
    data = json.dumps(_detail_envelope(), separators=(",", ":")).encode()
    metadata = _text_detail_metadata(**{
        "byte_count": len(data),
        "sha256": "b" * 64,
        **metadata_override,
    })
    with pytest.raises(ValueError, match="mismatch|match"):
        computer_protocol.decode_text_detail_envelope(
            data,
            snapshot_id="snapshot_detail",
            metadata=metadata,
            sha256="b" * 64,
        )


def test_snapshot_text_detail_v3_mismatch_fails_closed_before_variant_decoding():
    response = decode_response(_encoded_snapshot(
        _snapshot_payload(text_detail_artifact="ignored-without-metadata.ax.json"),
        version=2,
    ))

    assert response.ok is False
    assert response.error is not None
    assert response.error.code is ComputerErrorCode.PROTOCOL_MISMATCH


@pytest.mark.parametrize("cursor_visible", [0, 1, "false", None])
def test_snapshot_cursor_visible_requires_a_strict_boolean(cursor_visible):
    with pytest.raises(ValueError, match="cursor_visible"):
        decode_response(_encoded_snapshot(_snapshot_payload(cursor_visible=cursor_visible)))


@pytest.mark.parametrize(
    "reasons",
    [
        ["unknown_limit"],
        ["depth_limit", "depth_limit"],
        [],
    ],
)
def test_truncated_text_detail_requires_nonempty_unique_known_reasons(reasons):
    payload = _snapshot_payload(
        text_detail_artifact="snapshot-0123456789abcdef0123456789abcdef.ax.json",
        text_detail_metadata=_text_detail_metadata(truncated=True, truncation_reasons=reasons),
    )
    with pytest.raises(ValueError, match="truncation_reasons"):
        decode_response(_encoded_snapshot(payload))


def test_untruncated_text_detail_requires_empty_reasons_and_matching_snapshot_id():
    for metadata in (
        _text_detail_metadata(truncation_reasons=["depth_limit"]),
        _text_detail_metadata(snapshot_id="different_snapshot"),
    ):
        payload = _snapshot_payload(
            text_detail_artifact="snapshot-0123456789abcdef0123456789abcdef.ax.json",
            text_detail_metadata=metadata,
        )
        with pytest.raises(ValueError, match="text detail"):
            decode_response(_encoded_snapshot(payload))


def test_generic_foreground_coordinate_action_and_fragment_backend():
    action = ComputerAction(type="click", x=20, y=30)
    assert action.target_element_ref is None
    requirement = ForegroundFragmentActionRequirement(
        backend="foreground_pointer", action_class="click", intent="pointer:click"
    )
    assert requirement.backend == "foreground_pointer"


@pytest.mark.parametrize("restore", [True, False])
def test_takeover_end_can_release_input_without_restoring_focus(restore):
    payload = {"takeover_ref": "t1", "restore_previous_focus": restore}
    assert ComputerRequest("r1", "takeover_end", payload).payload == payload


@pytest.mark.parametrize("restore", [0, 1, "false", None, {}])
def test_takeover_end_rejects_non_boolean_restore_flag(restore):
    with pytest.raises((ValueError, TypeError)):
        ComputerRequest("r1", "takeover_end", {"takeover_ref": "t1", "restore_previous_focus": restore})


def test_native_subtree_request_preserves_exact_snapshot_anchor():
    from agent.runtime.computer_protocol import ComputerRequest, encode_request
    payload={'app_ref':'app','window_ref':'window','scope':'target_window','artifact_name':'capture.png','snapshot_id':'snapshot-a','subtree_ref':'ref-a'}
    encoded=encode_request(ComputerRequest('subtree','snapshot_subtree',payload))
    assert 'snapshot_subtree' in encoded and 'snapshot-a' in encoded
    for bad in ({**payload,'subtree_ref':''},{**payload,'scope':'display'},{**payload,'unexpected':True}):
        with pytest.raises(ValueError):
            encode_request(ComputerRequest('subtree','snapshot_subtree',bad))


@pytest.mark.parametrize("scope,extra", [
    ("display", {}),
    ("target_window", {"text_detail": "on", "text_detail_artifact_name": "state.ax.json"}),
])
def test_automatic_dialog_read_cannot_escalate_capture(scope, extra):
    with pytest.raises(ValueError, match="default target-window"):
        ComputerRequest("r1", "get_app_state", {"app_ref": "app", "window_ref": "dialog", "catalog_generation": 1,
                        "scope": scope, "artifact_name": "state.png", "expected_focused_dialog_document_identity_ref": "doc", **extra})
