"""Independent risk and approval policy for macOS Computer Use."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from agent.runtime.tools.approval import ScopedApprovalStore


@pytest.fixture
def portable_registry_bytes(monkeypatch):
    """Keep schema/policy tests active where the POSIX file boundary is unsupported."""
    if os.name != 'posix':
        import agent.runtime.macos_computer_compatibility as compatibility
        monkeypatch.setattr(compatibility, '_trusted_bytes', lambda path: path.read_bytes())


def test_compatibility_registry_rejects_windows_file_boundary(tmp_path, monkeypatch):
    import agent.runtime.macos_computer_compatibility as compatibility

    monkeypatch.setattr(compatibility, 'os', SimpleNamespace(name='nt'))
    with pytest.raises(compatibility.CompatibilityRegistryError, match='requires POSIX'):
        compatibility._trusted_bytes(tmp_path / 'registry.json')


def context(
    bundle: str,
    *,
    session: str = "session-1",
    window: str = "window-1",
    title: str = "Document",
    window_role: str = "AXWindow",
    elements: dict | None = None,
    known_path: str = "",
    trusted: bool = True,
):
    return {
        "metadata_trusted": trusted,
        "session_id": session,
        "bundle_id": bundle,
        "application": "Fixture App",
        "window_ref": window,
        "window_title": title,
        "window_role": window_role,
        "snapshot_id": "snapshot-1",
        "elements": elements or {},
        "known_path": known_path,
    }


@pytest.mark.parametrize(
    ("bundle", "element", "action", "risk"),
    [
        (
            "com.kingsoft.wpsoffice.mac",
            {"body": {"label": "正文", "role": "AXTextArea"}},
            {"type": "click", "element_ref": "body"},
            "ordinary",
        ),
        (
            "com.kingsoft.wpsoffice.mac",
            {"export": {"label": "导出 PDF", "role": "AXButton"}},
            {"type": "click", "element_ref": "export"},
            "high_impact",
        ),
        (
            "com.apple.finder",
            {"trash": {"label": "移到废纸篓", "role": "AXMenuItem"}},
            {"type": "click", "element_ref": "trash"},
            "high_impact",
        ),
        (
            "com.apple.Terminal",
            {"shell": {"label": "Shell", "role": "AXTextArea"}},
            {"type": "keypress", "key": "ENTER", "element_ref": "shell"},
            "high_impact",
        ),
        (
            "com.kingsoft.wpsoffice.mac",
            {"editor": {"label": "编辑器", "role": "AXTextArea"}},
            {"type": "keypress", "key": "return", "element_ref": "editor"},
            "ordinary",
        ),
        (
            "com.apple.systempreferences",
            {},
            {"type": "click", "x": 20, "y": 20},
            "high_impact",
        ),
    ],
)
def test_policy_matrix(bundle, element, action, risk):
    from agent.runtime.computer_policy import classify_computer_batch

    assert classify_computer_batch(context(bundle, elements=element), [action]).risk.value == risk


def test_enabled_nil_elements_are_not_policy_rejected():
    """Python side of the enabled==nil semantics: the policy classifier reads
    label/role only and never consults an `enabled` attribute, so elements that
    omit it (Finder rows, scroll regions) must classify ordinarily instead of
    being rejected or escalated to high_impact by a missing enabled value."""
    from agent.runtime.computer_policy import classify_computer_batch

    decision = classify_computer_batch(
        context(
            "com.apple.finder",
            elements={
                "row": {
                    "label": "Day 1 AM",
                    "role": "AXOutlineRow",
                    # no `enabled` field: AX does not expose it for these rows
                }
            },
        ),
        [{"type": "click", "element_ref": "row"}],
    )
    assert decision.risk.value == "ordinary"
    assert not decision.handoff_required


@pytest.mark.parametrize(
    "metadata",
    [
        {"label": "Password", "role": "AXTextField"},
        {"label": "验证码", "role": "AXTextField"},
        {"help": "Enter OTP", "role": "AXTextField"},
        {"label": "Touch ID", "role": "AXButton"},
        {"label": "Confirm payment", "role": "AXButton"},
        {"label": "银行卡", "role": "AXTextField"},
        {"label": "安全码", "role": "AXTextField"},
        {"label": "PIN", "role": "AXTextField"},
        {"label": "CAPTCHA", "role": "AXTextField"},
        {"label": "Log in", "role": "AXButton"},
        {"label": "Checkout", "role": "AXButton"},
        {"label": "ordinary", "subrole": "AXSecureTextField"},
    ],
)
def test_secure_authentication_and_payment_targets_are_prohibited(metadata):
    from agent.runtime.computer_policy import classify_computer_batch

    decision = classify_computer_batch(
        context("com.kingsoft.wpsoffice.mac", elements={"target": metadata}),
        [{"type": "click", "element_ref": "target"}],
    )

    assert decision.risk.value == "prohibited"
    assert decision.handoff_required is True


@pytest.mark.parametrize(
    "label",
    ["Send", "Submit", "Upload", "Export", "Share", "Delete", "Move to Trash", "Install", "Uninstall", "Save", "Rename", "Run"],
)
def test_external_side_effect_targets_are_high_impact(label):
    from agent.runtime.computer_policy import classify_computer_batch

    decision = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            elements={"target": {"label": label, "role": "AXButton"}},
        ),
        [{"type": "click", "element_ref": "target"}],
    )

    assert decision.risk.value == "high_impact"


def test_keyboard_action_uses_backend_focused_element_and_fails_closed_without_it():
    from agent.runtime.computer_policy import classify_computer_batch

    secure = context(
        "com.kingsoft.wpsoffice.mac",
        elements={"secure": {"label": "Password", "subrole": "AXSecureTextField"}},
    )
    secure["focused_element_ref"] = "secure"
    protected = classify_computer_batch(secure, [{"type": "type", "text": "redacted"}])
    unknown_focus = classify_computer_batch(
        context("com.kingsoft.wpsoffice.mac"),
        [{"type": "type", "text": "ordinary text"}],
    )

    assert protected.risk.value == "prohibited"
    assert unknown_focus.risk.value == "high_impact"


def test_only_trusted_metadata_can_make_an_action_ordinary():
    from agent.runtime.computer_policy import classify_computer_batch

    untrusted_self_report = classify_computer_batch(
        context("com.unknown.app", trusted=False),
        [{"type": "click", "element_ref": "missing", "element_label": "正文", "risk_hint": "ordinary"}],
    )
    unknown_reference = classify_computer_batch(
        context("com.kingsoft.wpsoffice.mac"),
        [{"type": "click", "element_ref": "missing", "element_label": "正文"}],
    )
    unknown_bundle = classify_computer_batch(
        context(
            "com.example.unprofiled",
            elements={"body": {"label": "正文", "role": "AXTextArea"}},
        ),
        [{"type": "click", "element_ref": "body"}],
    )
    trusted_reference = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            elements={"body": {"label": "正文", "role": "AXTextArea"}},
        ),
        [{"type": "click", "element_ref": "body", "element_label": "删除账户"}],
    )
    incomplete_reference = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            elements={"body": {"label": "正文"}},
        ),
        [{"type": "click", "element_ref": "body"}],
    )
    incomplete_window = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            title="",
            window_role="",
            elements={"body": {"label": "正文", "role": "AXTextArea"}},
        ),
        [{"type": "click", "element_ref": "body"}],
    )
    malformed_window_root = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            window_role="AXButton",
            elements={"body": {"label": "正文", "role": "AXTextArea"}},
        ),
        [{"type": "click", "element_ref": "body"}],
    )

    assert untrusted_self_report.risk.value == "high_impact"
    assert unknown_reference.risk.value == "high_impact"
    assert unknown_bundle.risk.value == "high_impact"
    assert incomplete_reference.risk.value == "high_impact"
    assert incomplete_window.risk.value == "high_impact"
    assert malformed_window_root.risk.value == "high_impact"
    # Model-authored labels are not treated as backend facts.
    assert trusted_reference.risk.value == "ordinary"


def test_model_risk_hint_can_raise_but_never_lower_trusted_risk():
    from agent.runtime.computer_policy import classify_computer_batch

    trusted = context(
        "com.kingsoft.wpsoffice.mac",
        elements={"export": {"label": "Export PDF", "role": "AXButton"}},
    )

    cannot_lower = classify_computer_batch(
        trusted,
        [{"type": "click", "element_ref": "export", "risk_hint": "observe"}],
    )
    can_raise = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            elements={"body": {"label": "正文", "role": "AXTextArea"}},
        ),
        [{"type": "click", "element_ref": "body", "risk_hint": "high_impact"}],
    )

    assert cannot_lower.risk.value == "high_impact"
    assert can_raise.risk.value == "high_impact"


@pytest.mark.parametrize(
    ("bundle", "key", "modifiers"),
    [
        ("com.apple.finder", "delete", ["command"]),
        ("com.apple.finder", "c", ["command"]),
        ("com.apple.finder", "v", ["command"]),
        ("com.apple.finder", "d", ["command"]),
        ("com.kingsoft.wpsoffice.mac", "s", ["command"]),
        ("com.kingsoft.wpsoffice.mac", "p", ["command"]),
    ],
)
def test_side_effect_keyboard_shortcuts_are_high_impact(bundle, key, modifiers):
    from agent.runtime.computer_policy import classify_computer_batch

    decision = classify_computer_batch(
        context(
            bundle,
            elements={"target": {"label": "Document", "role": "AXTextArea"}},
        ),
        [{"type": "keypress", "key": key, "modifiers": modifiers, "element_ref": "target"}],
    )

    assert decision.risk.value == "high_impact"


def test_batch_risk_is_monotonic_maximum_and_empty_batch_fails_closed():
    from agent.runtime.computer_policy import classify_computer_batch

    trusted = context(
        "com.kingsoft.wpsoffice.mac",
        elements={
            "body": {"label": "正文", "role": "AXTextArea"},
            "pay": {"label": "确认付款", "role": "AXButton"},
        },
    )
    decision = classify_computer_batch(
        trusted,
        [
            {"type": "wait", "duration_ms": 1},
            {"type": "click", "element_ref": "body"},
            {"type": "click", "element_ref": "pay"},
        ],
    )

    assert decision.risk.value == "prohibited"
    with pytest.raises(ValueError, match="at least one"):
        classify_computer_batch(trusted, [])


@pytest.mark.parametrize(
    ("bundle", "expected"),
    [
        ("com.kingsoft.wpsoffice.mac", "ordinary"),
        ("com.apple.finder", "high_impact"),
        ("com.example.unknown", "high_impact"),
    ],
)
def test_coordinate_clicks_are_ordinary_only_for_explicit_local_editing_allowlist(
    bundle,
    expected,
):
    from agent.runtime.computer_policy import classify_computer_batch

    decision = classify_computer_batch(context(bundle), [{"type": "click", "x": 10, "y": 20}])

    assert decision.risk.value == expected


def test_coordinate_hit_uses_trusted_ax_bounds_for_prohibited_and_high_impact_targets():
    from agent.runtime.computer_policy import classify_computer_batch

    trusted = context(
        "com.kingsoft.wpsoffice.mac",
        elements={
            "secure": {
                "label": "Password",
                "role": "AXSecureTextField",
                "bounds": {"x": 10, "y": 10, "width": 100, "height": 20},
            },
            "secure-icon": {
                "label": "Icon",
                "role": "AXImage",
                "bounds": {"x": 15, "y": 12, "width": 10, "height": 10},
            },
            "export": {
                "label": "Export PDF",
                "role": "AXButton",
                "bounds": {"x": 10, "y": 50, "width": 100, "height": 20},
            },
            "export-icon": {
                "label": "Icon",
                "role": "AXImage",
                "bounds": {"x": 15, "y": 52, "width": 10, "height": 10},
            },
        },
    )

    secure = classify_computer_batch(trusted, [{"type": "click", "x": 20, "y": 15}])
    export = classify_computer_batch(trusted, [{"type": "click", "x": 20, "y": 55}])

    assert secure.risk.value == "prohibited"
    assert export.risk.value == "high_impact"


def test_coordinate_pointer_approval_uses_declared_target_element_not_geometry():
    from agent.runtime.computer_policy import classify_computer_batch

    trusted = context(
        "com.kingsoft.wpsoffice.mac",
        elements={
            "body": {
                "label": "Document body",
                "role": "AXGroup",
                "bounds": {"x": 0, "y": 0, "width": 500, "height": 500},
            },
            "export": {
                "label": "Export PDF",
                "role": "AXButton",
                "bounds": {"x": 400, "y": 400, "width": 50, "height": 20},
            },
        },
    )

    decision = classify_computer_batch(trusted, [{
        "type": "click",
        "x": 20,
        "y": 20,
        "target_element_ref": "export",
    }])

    assert decision.risk.value == "high_impact"


def test_coordinate_pointer_approval_cannot_launder_child_risk_through_broad_target():
    from agent.runtime.computer_policy import classify_computer_batch

    trusted = context(
        "com.kingsoft.wpsoffice.mac",
        elements={
            "body": {
                "label": "Document body",
                "role": "AXGroup",
                "bounds": {"x": 0, "y": 0, "width": 500, "height": 500},
            },
            "export": {
                "label": "Export PDF",
                "role": "AXButton",
                "bounds": {"x": 400, "y": 400, "width": 50, "height": 20},
            },
        },
    )

    decision = classify_computer_batch(trusted, [{
        "type": "click",
        "x": 420,
        "y": 410,
        "target_element_ref": "body",
    }])

    assert decision.risk.value == "high_impact"


def test_scopes_are_bound_to_session_bundle_window_and_exact_high_impact_batch():
    from agent.runtime.computer_policy import classify_computer_batch

    ordinary = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            elements={"body": {"label": "正文", "role": "AXTextArea"}},
        ),
        [{"type": "click", "element_ref": "body"}],
    )
    high = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            elements={"export": {"label": "Export PDF", "role": "AXButton"}},
        ),
        [{"type": "click", "element_ref": "export"}],
    )
    different_window = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            window="window-2",
            elements={"export": {"label": "Export PDF", "role": "AXButton"}},
        ),
        [{"type": "click", "element_ref": "export"}],
    )

    assert ordinary.scope == "computer-write:session-1:com.kingsoft.wpsoffice.mac"
    assert ordinary.choices == ("once", "session", "deny")
    assert high.scope.startswith("computer-write-batch:session-1:")
    assert high.choices == ("once", "deny")
    assert high.scope != different_window.scope
    assert len(high.scope.rsplit(":", 1)[-1]) == 64


def test_high_impact_batch_hash_distinguishes_exact_typed_payload_without_retaining_it():
    from agent.runtime.computer_policy import classify_computer_batch

    terminal = context(
        "com.apple.Terminal",
        elements={"shell": {"label": "Shell", "role": "AXTextArea"}},
    )
    first = classify_computer_batch(
        terminal,
        [{"type": "type", "text": "printf first", "element_ref": "shell"}],
    )
    second = classify_computer_batch(
        terminal,
        [{"type": "type", "text": "printf second", "element_ref": "shell"}],
    )

    assert first.scope != second.scope
    assert "printf" not in first.scope
    assert "printf" not in first.operation


@pytest.mark.parametrize("text", ["", "new 中文 value"], ids=["clear", "text"])
@pytest.mark.parametrize("secret", [None, b"fixture-only-key"])
def test_replacement_intent_is_bound_to_batch_hash_and_exact_approval_scope(text, secret):
    from agent.runtime.computer_policy import classify_computer_batch
    from agent.runtime.computer_backend import ComputerActionPlan
    from agent.runtime.computer_protocol import ComputerInteractionMode

    target = context(
        "com.apple.Terminal",
        elements={"field": {"label": "Shell", "role": "AXTextArea"}},
    )
    if secret:
        target["_scope_secret"] = secret
    plan = ComputerActionPlan(
        plan_ref="same-plan",
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        requires_takeover=True,
        reason="foreground_takeover_required",
        action_classes=("text",),
        pid_action_classes=(),
    )
    action = {"type": "type", "text": text, "element_ref": "field"}
    ordinary = classify_computer_batch(target, [action], interaction_plan=plan)
    replacement = classify_computer_batch(target, [{**action, "replace": True}], interaction_plan=plan)

    assert ordinary.risk.value == replacement.risk.value == "high_impact"
    assert ordinary.batch_hash != replacement.batch_hash
    assert ordinary.scope != replacement.scope


def test_legacy_type_hash_is_unchanged_when_replacement_flag_is_absent():
    import hashlib

    from agent.runtime.computer_policy import classify_computer_batch

    target = context("com.apple.Terminal")
    action = {"type": "type", "text": "", "element_ref": "field"}
    canonical = {
        "session_id": "session-1", "bundle_id": "com.apple.Terminal",
        "window_ref": "window-1", "snapshot_id": "snapshot-1",
        "interaction_mode": "background", "requires_takeover": False,
        "action_classes": ["text"], "actions": [action],
    }
    expected = hashlib.sha256(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    assert classify_computer_batch(target, [action]).batch_hash == expected


def test_batch_hash_supports_an_ephemeral_secret_to_avoid_offline_text_guessing():
    from agent.runtime.computer_policy import classify_computer_batch

    first_context = context(
        "com.apple.Terminal",
        elements={"shell": {"label": "Shell", "role": "AXTextArea"}},
    )
    second_context = dict(first_context)
    first_context["_scope_secret"] = b"first ephemeral secret"
    second_context["_scope_secret"] = b"second ephemeral secret"
    action = [{"type": "type", "text": "short-sensitive-value", "element_ref": "shell"}]

    first = classify_computer_batch(first_context, action)
    second = classify_computer_batch(second_context, action)

    assert first.scope != second.scope


def test_permission_request_is_complete_but_does_not_retain_typed_text_or_private_paths():
    from agent.runtime.computer_policy import classify_computer_batch, permission_request

    secret = "DO-NOT-RETAIN-THIS-TEXT"
    private_cache = "/private/cache/request-local/snapshot.png"
    decision = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            title="Quarterly report",
            elements={"body": {"label": "正文", "role": "AXTextArea"}},
            known_path="/Users/me/Documents/report.docx",
        ),
        [{"type": "type", "text": secret, "request_local_path": private_cache}],
    )
    request = permission_request(ScopedApprovalStore(), decision)
    encoded = json.dumps(request, ensure_ascii=False)

    assert request is not None
    assert request["operation"]
    assert "Fixture App" in request["target"]
    assert "Quarterly report" in request["target"]
    assert request["approval_effect"]
    assert request["approval_boundary"]
    assert request["approval_question"].endswith("?")
    assert secret not in encoded
    assert private_cache not in encoded
    assert "/Users/me/Documents/report.docx" not in encoded
    assert decision.batch_hash not in encoded
    assert "actions" not in request.get("arguments", {})


def test_permission_grants_are_scoped_and_high_impact_session_choice_is_only_once():
    from agent.runtime.computer_policy import (
        classify_computer_batch,
        permission_grant,
        permission_request,
    )

    store = ScopedApprovalStore()
    decision = classify_computer_batch(
        context(
            "com.kingsoft.wpsoffice.mac",
            elements={"export": {"label": "Export", "role": "AXButton"}},
        ),
        [{"type": "click", "element_ref": "export"}],
    )
    request = permission_request(store, decision)
    assert request is not None

    cleanup = permission_grant(store, {}, request, "session")
    assert decision.scope in store.approved_scopes
    assert cleanup is not None
    cleanup()
    assert decision.scope not in store.approved_scopes

    with pytest.raises(ValueError, match="decision"):
        permission_grant(store, {}, request, "unexpected")


def test_snapshot_detail_approval_is_once_only_and_bound_to_every_capture_dimension():
    import agent.runtime.computer_policy as policy

    build = getattr(policy, "snapshot_permission_request", None)
    assert callable(build)
    context = {
        "session_id": "session-1",
        "app_ref": "app-1",
        "window_ref": "window-1",
        "application": "Fixture",
        "window_title": "Document",
    }
    store = ScopedApprovalStore()
    request = build(
        store,
        context,
        capture_scope="target_window",
        text_detail_mode="on",
        target_binding="binding-1",
    )

    assert request is not None
    assert request["kind"] == "computer_text_detail_capture"
    assert request["choices"] == ["once", "deny"]
    assert request["arguments"] == {
        "application": "Fixture",
        "window": "Document",
        "scope": "target_window",
        "text_detail": "on",
    }
    assert "scope" not in request
    first_scope = request.private_scope
    assert first_scope.startswith("computer-observe-snapshot:")

    variants = [
        ({**context, "session_id": "session-2"}, "target_window", "on", "binding-1"),
        ({**context, "app_ref": "app-2"}, "target_window", "on", "binding-1"),
        ({**context, "window_ref": "window-2"}, "target_window", "on", "binding-1"),
        (context, "display", "on", "binding-1"),
        (context, "display", "off", "binding-1"),
        (context, "target_window", "off", "binding-1"),
    ]
    scopes = {first_scope}
    for variant_context, capture_scope, mode, binding in variants:
        candidate = build(
            store,
            variant_context,
            capture_scope=capture_scope,
            text_detail_mode=mode,
            target_binding=binding,
        )
        if capture_scope == "target_window" and mode == "off":
            assert candidate is None
        else:
            assert candidate is not None
            scopes.add(candidate.private_scope)
    assert len(scopes) == 6

    store.approved_scopes.add(first_scope)
    assert build(
        store,
        context,
        capture_scope="target_window",
        text_detail_mode="on",
        target_binding="binding-1",
    ) is None


def test_snapshot_display_and_text_detail_use_one_combined_approval_copy():
    import agent.runtime.computer_policy as policy

    build = getattr(policy, "snapshot_permission_request", None)
    assert callable(build)
    request = build(
        ScopedApprovalStore(),
        {
            "session_id": "session-1",
            "app_ref": "app-1",
            "window_ref": "window-1",
            "application": "Fixture",
            "window_title": "Document",
        },
        capture_scope="display",
        text_detail_mode="on",
        target_binding="binding-1",
    )

    assert request is not None
    assert request["kind"] == "computer_display_and_text_detail_capture"
    assert request["choices"] == ["once", "deny"]
    assert "display" in request["approval_summary"].casefold()
    assert "accessibility text" in request["approval_summary"].casefold()


def test_app_state_approval_scope_binds_exact_catalog_target_and_capture_dimensions():
    import agent.runtime.computer_policy as policy

    build = getattr(policy, "app_state_permission_request", None)
    assert callable(build)
    context = {
        "session_id": "session-1",
        "application": "Fixture",
        "window_title": "Document",
    }
    store = ScopedApprovalStore()
    request = build(
        store,
        context,
        catalog_generation=7,
        app_ref="app-1",
        window_ref="window-1",
        capture_scope="target_window",
        text_detail_mode="on",
    )

    assert request is not None
    assert "session-1:7:app-1:window-1:target_window:on" in request.private_scope
    assert request.private_catalog_generation == 7
    assert request.private_app_ref == "app-1"
    assert request.private_window_ref == "window-1"
    assert "session-1:7:app-1:window-1:target_window:on" not in json.dumps(request)

    variants = [
        (8, "app-1", "window-1", "target_window", "on"),
        (7, "app-2", "window-1", "target_window", "on"),
        (7, "app-1", "window-2", "target_window", "on"),
        (7, "app-1", "window-1", "display", "on"),
        (7, "app-1", "window-1", "display", "off"),
    ]
    scopes = {request.private_scope}
    for generation, app_ref, window_ref, scope, mode in variants:
        candidate = build(
            store,
            context,
            catalog_generation=generation,
            app_ref=app_ref,
            window_ref=window_ref,
            capture_scope=scope,
            text_detail_mode=mode,
        )
        assert candidate is not None
        scopes.add(candidate.private_scope)
    assert len(scopes) == 6

    assert build(
        store,
        context,
        catalog_generation=7,
        app_ref="app-1",
        window_ref="window-1",
        capture_scope="target_window",
        text_detail_mode="off",
    ) is None

    store.approved_scopes.add(request.private_scope)
    assert build(
        store,
        context,
        catalog_generation=7,
        app_ref="app-1",
        window_ref="window-1",
        capture_scope="target_window",
        text_detail_mode="on",
    ) is None


def test_app_state_approval_scope_canonical_hash_prevents_delimiter_collision():
    from agent.runtime.computer_policy import app_state_approval_scope

    context = {"session_id": "session-1"}
    first_plaintext = "session-1:7:a:b:c:target_window:on"
    second_plaintext = "session-1:7:a:b:c:target_window:on"
    assert first_plaintext == second_plaintext

    first = app_state_approval_scope(
        context,
        catalog_generation=7,
        app_ref="a:b",
        window_ref="c",
        capture_scope="target_window",
        text_detail_mode="on",
    )
    second = app_state_approval_scope(
        context,
        catalog_generation=7,
        app_ref="a",
        window_ref="b:c",
        capture_scope="target_window",
        text_detail_mode="on",
    )

    assert first != second

@pytest.mark.usefixtures('portable_registry_bytes')
def test_compatibility_registry_is_exact_versioned_and_default_deny(tmp_path, monkeypatch):
    import agent.runtime.macos_computer_compatibility as compatibility

    registry_path = tmp_path / "macos-computer-compatibility.json"
    registry_path.write_text(json.dumps({
        "schema_version": 2,
        "applications": [{
            "bundle_id": "com.kingsoft.wpsoffice.mac",
            "app_version": "12.2.0",
            "capabilities": [
                {
                    "backend": "pid_pointer",
                    "enabled_actions": ["scroll", "click"],
                },
                {
                    "backend": "foreground_keyboard",
                    "enabled_actions": ["text"],
                    "allowed_key_chords": ["command+a", "command+shift+s", "return"],
                    "allow_text_entry": True,
                },
            ],
        }],
    }))
    registry_path.chmod(0o600)
    monkeypatch.setattr(compatibility, "_REGISTRY_PATH", registry_path)

    assert compatibility.enabled_pid_actions(
        "com.kingsoft.wpsoffice.mac", "12.2.0"
    ) == frozenset({"scroll", "click"})
    assert compatibility.enabled_pid_actions(
        "com.kingsoft.wpsoffice.mac", "12.2.1"
    ) == frozenset()
    assert compatibility.enabled_pid_actions(
        "com.example.unknown", "12.2.0"
    ) == frozenset()
    assert compatibility.exact_dispatch_compatibility(
        "com.kingsoft.wpsoffice.mac", "12.2.0", "foreground_keyboard"
    ) == compatibility.MacDispatchCompatibility(
        backend="foreground_keyboard",
        enabled_actions=frozenset({"text"}),
        allowed_key_chords=frozenset({"command+a", "command+shift+s", "return"}),
        allow_text_entry=True,
    )
    assert compatibility.exact_dispatch_compatibility(
        "com.kingsoft.wpsoffice.mac", "12.2.1", "foreground_keyboard"
    ) is None
    assert compatibility.exact_dispatch_compatibility(
        "com.kingsoft.wpsoffice.mac", "12.2.0", []  # type: ignore[arg-type]
    ) is None


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 1, "applications": []},
        {"schema_version": 2.0, "applications": []},
        {"schema_version": True, "applications": []},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [{"backend": "pid_pointer", "enabled_actions": ["click"]}],
        }, {
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [{"backend": "pid_pointer", "enabled_actions": ["scroll"]}],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [{"backend": "unknown", "enabled_actions": ["click"]}],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [{"backend": "pid_pointer", "enabled_actions": ["launch"]}],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [
                {"backend": "pid_pointer", "enabled_actions": ["click"]},
                {"backend": "pid_pointer", "enabled_actions": ["scroll"]},
            ],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [{
                "backend": "foreground_keyboard",
                "enabled_actions": ["text"],
                "allowed_key_chords": ["shift+command+s"],
                "allow_text_entry": True,
            }],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [{
                "backend": "foreground_keyboard",
                "enabled_actions": ["text"],
                "allowed_key_chords": ["return", "return"],
                "allow_text_entry": True,
            }],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [{
                "backend": "foreground_keyboard",
                "enabled_actions": ["text"],
                "allowed_key_chords": ["command+v"],
                "allow_text_entry": False,
            }],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [{
                "backend": "foreground_keyboard",
                "enabled_actions": ["text"],
                "allowed_key_chords": ["return"],
                "allow_text_entry": 1,
            }],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example." + "x" * 300,
            "app_version": "1.0.0",
            "capabilities": [{"backend": "pid_pointer", "enabled_actions": ["scroll"]}],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "*",
            "capabilities": [{"backend": "pid_pointer", "enabled_actions": ["scroll"]}],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1." + "0" * 64,
            "capabilities": [{"backend": "pid_pointer", "enabled_actions": ["scroll"]}],
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [{"backend": "pid_pointer", "enabled_actions": ["click"]}],
            "unexpected": True,
        }]},
        {"schema_version": 2, "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [
                {"backend": "pid_pointer", "enabled_actions": ["click"]},
                {
                    "backend": "foreground_keyboard",
                    "enabled_actions": ["text"],
                    "allowed_key_chords": [],
                    "allow_text_entry": True,
                },
                {"backend": "pid_pointer", "enabled_actions": ["scroll"]},
            ],
        }]},
    ],
)
@pytest.mark.usefixtures('portable_registry_bytes')
def test_compatibility_registry_rejects_duplicate_unknown_unbounded_and_invalid_cells(
    tmp_path,
    monkeypatch,
    document,
):
    import agent.runtime.macos_computer_compatibility as compatibility

    registry_path = tmp_path / "macos-computer-compatibility.json"
    registry_path.write_text(json.dumps(document))
    registry_path.chmod(0o600)
    monkeypatch.setattr(compatibility, "_REGISTRY_PATH", registry_path)

    with pytest.raises(compatibility.CompatibilityRegistryError):
        compatibility.enabled_pid_actions("com.example.app", "1.0.0")


@pytest.mark.skipif(os.name != 'posix', reason='registry file authority uses POSIX ownership and modes')
def test_compatibility_registry_rejects_symlink_and_group_writable_paths(tmp_path, monkeypatch):
    import agent.runtime.macos_computer_compatibility as compatibility

    real_path = tmp_path / "real.json"
    real_path.write_text('{"schema_version":2,"applications":[]}')
    real_path.chmod(0o620)
    monkeypatch.setattr(compatibility, "_REGISTRY_PATH", real_path)
    with pytest.raises(compatibility.CompatibilityRegistryError, match="writable"):
        compatibility.enabled_pid_actions("com.example.app", "1.0.0")

    real_path.chmod(0o600)
    linked_path = tmp_path / "linked.json"
    os.symlink(real_path, linked_path)
    monkeypatch.setattr(compatibility, "_REGISTRY_PATH", linked_path)
    with pytest.raises(compatibility.CompatibilityRegistryError, match="symlink"):
        compatibility.enabled_pid_actions("com.example.app", "1.0.0")


@pytest.mark.usefixtures('portable_registry_bytes')
def test_compatibility_registry_rejects_duplicate_json_fields_and_oversized_chord_sets(
    tmp_path, monkeypatch
):
    import agent.runtime.macos_computer_compatibility as compatibility

    registry_path = tmp_path / "macos-computer-compatibility.json"
    monkeypatch.setattr(compatibility, "_REGISTRY_PATH", registry_path)

    registry_path.write_text('{"schema_version":2,"schema_version":2,"applications":[]}')
    registry_path.chmod(0o600)
    with pytest.raises(compatibility.CompatibilityRegistryError, match="duplicate"):
        compatibility.enabled_pid_actions("com.example.app", "1.0.0")

    registry_path.write_text(json.dumps({
        "schema_version": 2,
        "applications": [{
            "bundle_id": "com.example.app",
            "app_version": "1.0.0",
            "capabilities": [{
                "backend": "foreground_keyboard",
                "enabled_actions": ["text"],
                "allowed_key_chords": [
                    f"command+{key}" for key in "abcdefghijklmnopqrstuvwxyz0123456"
                ],
                "allow_text_entry": False,
            }],
        }],
    }))
    with pytest.raises(compatibility.CompatibilityRegistryError):
        compatibility.exact_dispatch_compatibility(
            "com.example.app", "1.0.0", "foreground_keyboard"
        )


@pytest.mark.usefixtures('portable_registry_bytes')
def test_compatibility_registry_retains_depth_and_collection_bounds(
    tmp_path, monkeypatch
):
    import agent.runtime.macos_computer_compatibility as compatibility

    registry_path = tmp_path / "macos-computer-compatibility.json"
    monkeypatch.setattr(compatibility, "_REGISTRY_PATH", registry_path)

    nested = "[]"
    for _ in range(34):
        nested = f"[{nested}]"
    registry_path.write_text(
        f'{{"schema_version":2,"applications":[],"unexpected":{nested}}}'
    )
    registry_path.chmod(0o600)
    with pytest.raises(compatibility.CompatibilityRegistryError, match="nesting"):
        compatibility.enabled_pid_actions("com.example.app", "1.0.0")

    applications = [
        {
            "bundle_id": f"com.example.app{index}",
            "app_version": "1.0.0",
            "capabilities": [{"backend": "pid_pointer", "enabled_actions": ["click"]}],
        }
        for index in range(129)
    ]
    registry_path.write_text(json.dumps({"schema_version": 2, "applications": applications}))
    with pytest.raises(compatibility.CompatibilityRegistryError, match="applications"):
        compatibility.enabled_pid_actions("com.example.app0", "1.0.0")

@pytest.mark.skipif(os.name != 'posix', reason='registry file authority uses POSIX ownership and modes')
def test_compatibility_registry_rejects_oversized_private_file(tmp_path, monkeypatch):
    import agent.runtime.macos_computer_compatibility as compatibility

    registry_path = tmp_path / 'registry.json'
    registry_path.write_bytes(b' ' * (64 * 1024 + 1))
    registry_path.chmod(0o600)
    monkeypatch.setattr(compatibility, '_REGISTRY_PATH', registry_path)
    with pytest.raises(compatibility.CompatibilityRegistryError, match="unbounded"):
        compatibility.enabled_pid_actions("com.example.app", "1.0.0")


@pytest.mark.usefixtures('portable_registry_bytes')
def test_compatibility_registry_rejects_non_integer_exponential_schema(
    tmp_path, monkeypatch
):
    import agent.runtime.macos_computer_compatibility as compatibility

    registry_path = tmp_path / "macos-computer-compatibility.json"
    registry_path.write_text('{"schema_version":2e0,"applications":[]}')
    registry_path.chmod(0o600)
    monkeypatch.setattr(compatibility, "_REGISTRY_PATH", registry_path)

    with pytest.raises(compatibility.CompatibilityRegistryError, match="schema version"):
        compatibility.enabled_pid_actions("com.example.app", "1.0.0")


@pytest.mark.parametrize(
    ("encoding", "payload"),
    [
        ("utf-8-bom", b"\xef\xbb\xbf" + b'{"schema_version":2,"applications":[]}'),
        ("utf-16le", '{"schema_version":2,"applications":[]}'.encode("utf-16le")),
        ("utf-16be", '{"schema_version":2,"applications":[]}'.encode("utf-16be")),
        ("utf-32le", '{"schema_version":2,"applications":[]}'.encode("utf-32le")),
        ("utf-32be", '{"schema_version":2,"applications":[]}'.encode("utf-32be")),
    ],
)
@pytest.mark.usefixtures('portable_registry_bytes')
def test_compatibility_registry_rejects_non_plain_utf8_documents(
    tmp_path, monkeypatch, encoding, payload
):
    import agent.runtime.macos_computer_compatibility as compatibility

    registry_path = tmp_path / f"macos-computer-compatibility-{encoding}.json"
    registry_path.write_bytes(payload)
    registry_path.chmod(0o600)
    monkeypatch.setattr(compatibility, "_REGISTRY_PATH", registry_path)

    with pytest.raises(compatibility.CompatibilityRegistryError, match="not strict JSON"):
        compatibility.enabled_pid_actions("com.example.app", "1.0.0")


def test_foreground_fragment_approval_is_once_only_combines_risk_and_is_redacted():
    from agent.runtime.computer_backend import ComputerActionPlan
    from agent.runtime.computer_policy import (
        build_computer_approval,
        classify_computer_batch,
    )
    from agent.runtime.computer_protocol import ComputerInteractionMode

    policy_context = context(
        "com.kingsoft.wpsoffice.mac",
        title="/Users/private/report.docx",
        known_path="/Users/private/report.docx",
        elements={"body": {"label": "正文", "role": "AXTextArea"}},
    )
    policy_context.update({
        "application": "WPS Office",
        "app_ref": "private-app-ref",
        "pid": 4242,
        "marker": "private-marker",
    })
    action = {
        "type": "scroll",
        "delta_y": 300,
        "element_ref": "private-element-ref",
        "text": "DO-NOT-RETAIN-TYPED-TEXT",
    }
    background_plan = ComputerActionPlan(
        plan_ref="private-background-plan-token",
        interaction_mode=ComputerInteractionMode.BACKGROUND,
        requires_takeover=True,
        reason="foreground_takeover_required",
        action_classes=("scroll",),
        pid_action_classes=("scroll",),
    )
    foreground_plan = ComputerActionPlan(
        plan_ref="private-foreground-plan-token",
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        requires_takeover=True,
        reason="/Users/private/reason private-plan-token",
        action_classes=("scroll",),
        pid_action_classes=("scroll",),
    )

    background = classify_computer_batch(
        policy_context, [action], interaction_plan=background_plan
    )
    foreground = classify_computer_batch(
        policy_context, [action], interaction_plan=foreground_plan
    )
    request = build_computer_approval(foreground)
    encoded = json.dumps(request, ensure_ascii=False)

    assert background.batch_hash != foreground.batch_hash
    assert request["choices"] == ["once", "deny"]
    assert request["kind"] == "computer_foreground_takeover"
    assert request["arguments"]["application"] == "WPS Office"
    assert request["arguments"]["reason"]
    assert request["arguments"]["action_classes"] == ["scroll"]
    assert request["arguments"]["expected_effect"]
    assert request["approval_boundary"] == foreground.boundary
    for private in (
        "DO-NOT-RETAIN-TYPED-TEXT",
        "/Users/private/report.docx",
        "4242",
        "private-marker",
        "private-app-ref",
        "private-element-ref",
        "private-background-plan-token",
        "private-foreground-plan-token",
        "/Users/private/reason",
        background.batch_hash,
        foreground.batch_hash,
    ):
        assert private not in encoded


def test_foreground_approval_uses_fixed_labels_when_only_hostile_refs_exist():
    from agent.runtime.computer_backend import ComputerActionPlan
    from agent.runtime.computer_policy import (
        build_computer_approval,
        classify_computer_batch,
    )
    from agent.runtime.computer_protocol import ComputerInteractionMode

    hostile_app_ref = "raw-private-app-ref"
    hostile_window_ref = "raw-private-window-ref"
    hostile_element_ref = "raw-private-element-ref"
    hostile_label = f"/Users/private/{hostile_element_ref}"
    policy_context = context(
        "com.example.editor",
        window=hostile_window_ref,
        title="",
        elements={hostile_element_ref: {"label": hostile_label, "role": "AXTextArea"}},
    )
    policy_context.pop("application")
    policy_context["app_ref"] = hostile_app_ref
    plan = ComputerActionPlan(
        plan_ref="private-plan-ref",
        interaction_mode=ComputerInteractionMode.FOREGROUND_TAKEOVER,
        requires_takeover=True,
        reason="foreground_takeover_required",
        action_classes=("click",),
        pid_action_classes=("click",),
    )

    request = build_computer_approval(classify_computer_batch(
        policy_context,
        [{"type": "click", "element_ref": hostile_element_ref}],
        interaction_plan=plan,
    ))
    encoded = json.dumps(request, ensure_ascii=False)

    assert request["arguments"]["application"] == "Selected application"
    assert request["arguments"]["window"] == "Selected window"
    for raw_ref in (
        hostile_app_ref,
        hostile_window_ref,
        hostile_element_ref,
        hostile_label,
        "private-plan-ref",
    ):
        assert raw_ref not in encoded


@pytest.mark.usefixtures('portable_registry_bytes')
def test_enabled_pid_actions_covers_every_pid_backend(tmp_path, monkeypatch) -> None:
    """`enabled_pid_actions` 必须看得见**全部 pid_\\* 后端**的已评审授权。

    实机（2026-09-03 15:12）：Safari 26.6.2 在表里有 pid_keyboard→text（`allow_text_entry`），
    而该函数只查 pid_pointer ⇒ 返回 {click, scroll}，调用方那句
    `set(plan.pid_action_classes).issubset(enabled)` 对 text 必然失败 —— 表里三家
    （Safari / WPS / 微信）的键盘文本授权对读取侧永久不可见，type 从来没有过机会。

    同时锁住另一半语义：`foreground_keyboard` 是**另一条**投递路径，由
    exact_dispatch_compatibility(..., "foreground_keyboard") 单独把关，不得被并进来。
    """
    import agent.runtime.macos_computer_compatibility as compatibility

    registry_path = tmp_path / "macos-computer-compatibility.json"
    registry_path.write_text(json.dumps({
        "schema_version": 2,
        "applications": [
            {
                "bundle_id": "com.example.both",
                "app_version": "1.0",
                "capabilities": [
                    {"backend": "pid_pointer", "enabled_actions": ["click", "scroll"]},
                    {
                        "backend": "pid_keyboard",
                        "enabled_actions": ["text"],
                        "allowed_key_chords": [],
                        "allow_text_entry": True,
                    },
                ],
            },
            {
                "bundle_id": "com.example.pointeronly",
                "app_version": "1.0",
                "capabilities": [
                    {"backend": "pid_pointer", "enabled_actions": ["click"]},
                    {
                        "backend": "foreground_keyboard",
                        "enabled_actions": ["text"],
                        "allowed_key_chords": [],
                        "allow_text_entry": True,
                    },
                ],
            },
        ],
    }))
    registry_path.chmod(0o600)
    monkeypatch.setattr(compatibility, "_REGISTRY_PATH", registry_path)

    assert compatibility.enabled_pid_actions("com.example.both", "1.0") == frozenset(
        {"click", "scroll", "text"}
    )
    # foreground_keyboard 的 text 不算 pid 授权（另一条路径，别的调用点负责核对）。
    assert compatibility.enabled_pid_actions("com.example.pointeronly", "1.0") == frozenset({"click"})
    # 默认拒绝不变：版本不匹配、app 不存在都拿不到任何许可。
    assert compatibility.enabled_pid_actions("com.example.both", "1.1") == frozenset()
    assert compatibility.enabled_pid_actions("com.example.absent", "1.0") == frozenset()
