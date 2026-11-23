"""Save As workflow approvals exercised through the Computer Use tool registry."""

import json
import sys

import pytest

from agent.runtime import approval_time
from tests import test_computer_tools as cu
from tests.test_computer_wps_replace import body

backend, manager, registry = cu.backend, cu.manager, cu.registry
pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS local destination proof")


def snapshot(result):
    assert not result["error"], result
    return json.loads(result["fresh_output"])["snapshot_id"]


@pytest.fixture
def workflow(registry, manager, backend, monkeypatch, tmp_path):
    source = tmp_path.resolve() / "original.docx"
    target = source.with_name("copy.docx")
    source.touch()
    clock = [1000.0]
    monkeypatch.setattr(approval_time, "continuous_now", lambda: clock[0])
    backend.catalog_bundle_id = "com.kingsoft.wpsoffice.mac"
    backend.catalog_app_version = "12.1.26026"
    backend.catalog_window_title = source.name
    backend.catalog_window_identity_ref = "source"
    backend.focus_loan = True
    backend.plan_pid_action_classes = ()
    backend.snapshot_ax_tree_override = {
        "role": "AXWindow", "subrole": "AXStandardWindow", "title": source.name,
        "document_path": str(source),
        "observation_capabilities": ["focus_consent_v1", "focus_loan_v3"],
        "children": [{"role": "AXMenuItem", "title": "另存为", "element_ref": "snapshot-1:open"}],
    }
    cards = []

    async def approve(card):
        cards.append(card)
        return "once"

    cu.register(registry, manager)
    registry.set_approval_handler(approve)
    cu.focus(registry)
    initial = snapshot(cu.run(registry.execute("computer_snapshot", {})))

    def act(state, action, **args):
        return cu.run(registry.execute("computer_act", {"snapshot_id": state, "actions": [action], **args}))

    def open_panel(path=target, *, opens_dialog=True):
        return act(initial, {"type": "click", "element_ref": "snapshot-1:open"},
                   opens_dialog=opens_dialog, save_as_intent=str(path))

    old_apps = backend.apps

    def bind_panel(*, other=False):
        panel = cu._wps_new_copy_panel(target.parent)
        panel["observation_capabilities"] += ["focus_consent_v1", "focus_loan_v3"]
        panel["children"].append({"role": "AXButton", "label": "Export PDF", "element_ref": "snapshot-1:export"})
        backend.snapshot_ax_tree_override = panel
        backend.catalog_window_title = "另存为"
        backend.catalog_window_identity_ref = "panel"

        async def apps():
            catalog = await old_apps()
            current = dict(catalog.apps[0])
            panel_window = current["windows"][0]
            current["windows"] = [panel_window, {
                **panel_window, "title": source.name, "window_ref": "source-ref",
                "window_identity_ref": "source", "document_path": str(source),
            }, {
                **panel_window, "title": "Other window", "window_ref": "other-ref",
                "window_identity_ref": "other",
            }]
            return cu.ComputerAppCatalog(catalog.generation, (current,))

        monkeypatch.setattr(backend, "apps", apps)
        cu.run(registry.execute("computer_apps", {}))
        return snapshot(cu.run(registry.execute("computer_get_app_state", {
            "app_ref": "app-1", "window_ref": "other-ref" if other else "window-1",
        })))

    return source, target, clock, cards, act, open_panel, bind_panel


def test_approved_save_as_covers_bound_fields_and_guarded_commit(workflow, backend, registry, monkeypatch):
    source, target, _, cards, act, open_panel, bind_panel = workflow
    assert not open_panel()["error"]
    assert len(cards) == 1 and cards[0]["choices"] == ["once", "deny"]
    assert cards[0]["kind"] == "computer_foreground_takeover"
    assert cards[0]["arguments"]["save_as_intent"] == {
        "source": str(source), "directory": str(target.parent), "filename": target.name,
    }
    state = bind_panel()
    edited = act(state, {"type": "type", "element_ref": "snapshot-1:name", "text": target.name})
    state = snapshot(edited)
    saved = act(state, {"type": "click", "element_ref": "snapshot-1:save", "save_new_copy_to": str(target)})
    assert not saved["error"]
    assert len(cards) == 1
    assert sum(name == "act" for name, _ in backend.calls) == 3
    # The normal reservation blocks another Save while this one settles.
    duplicate = act(snapshot(saved), {"type": "click", "element_ref": "snapshot-1:save", "save_new_copy_to": str(target)})
    assert duplicate["code"] == "effect_pending"

    body(target, ["The new document"])
    backend.catalog_window_title = target.name
    backend.catalog_window_identity_ref = "source"
    backend.snapshot_ax_tree_override = {
        "role": "AXWindow", "subrole": "AXStandardWindow", "title": target.name,
        "document_path": str(target), "children": [],
    }
    native_write = backend.act

    async def document_write(*args, **kwargs):
        result = await native_write(*args, **kwargs)
        body(target, ["The new document", "Saved edit"])
        return result

    monkeypatch.setattr(backend, "act", document_write)
    cu.focus(registry)
    state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
    result = act(state, {"type": "keypress", "key": "s", "modifiers": ["command"]})
    assert not result["error"], result
    evidence = json.loads(result["fresh_output"])["saved_document"]
    assert evidence["status"] == "read_back"
    assert evidence["paragraphs"] == ["The new document", "Saved edit"]


@pytest.mark.parametrize("scenario", ["expired", "different_window", "keyword_element", "different_path", "foreground"])
def test_save_as_intent_scope_boundaries(workflow, backend, scenario):
    _, target, clock, cards, act, open_panel, bind_panel = workflow
    assert not open_panel()["error"]
    if scenario == "expired":
        clock[0] += 300
    state = bind_panel(other=scenario == "different_window")
    action = {"type": "type", "element_ref": "snapshot-1:name", "text": target.name}
    mode = "foreground_takeover" if scenario == "foreground" else "background"
    if scenario == "keyword_element":
        action = {"type": "click", "element_ref": "snapshot-1:export"}
    if scenario == "different_path":
        action = {"type": "click", "element_ref": "snapshot-1:save",
                  "save_new_copy_to": str(target.with_name("different.docx"))}
    result = act(state, action, interaction_mode=mode)
    if scenario == "different_path":
        assert result["code"] == "save_destination_mismatch"
        assert len(cards) == 1
    else:
        assert not result["error"], result
        assert len(cards) == 2
        assert cards[-1]["choices"] == (["once", "session", "deny"] if scenario == "foreground" else ["once", "deny"])
    if scenario not in {"foreground", "different_window"}:
        state = bind_panel()
        assert not act(state, {"type": "type", "element_ref": "snapshot-1:name", "text": target.name})["error"]
        assert len(cards) == (2 if scenario == "different_path" else 3)
    if scenario == "different_window":
        state = bind_panel()
        assert not act(state, {"type": "type", "element_ref": "snapshot-1:name", "text": target.name})["error"]
        assert len(cards) == 3


@pytest.mark.parametrize("scenario,code", [
    ("relative", "invalid_arguments"), ("no_extension", "invalid_arguments"),
    ("no_opener", "invalid_arguments"), ("missing_directory", "save_destination_unverified"),
    ("not_directory", "save_destination_unverified"), ("unknown_source", "save_destination_unverified"),
    ("format_mismatch", "save_destination_unverified"), ("exists", "save_destination_exists"),
])
def test_invalid_save_as_intent_stops_before_approval(workflow, backend, registry, scenario, code):
    source, target, _, cards, _, open_panel, _ = workflow
    path = {
        "relative": "copy.docx", "no_extension": target.with_suffix(""),
        "missing_directory": target.parent / "missing" / target.name,
        "not_directory": source / target.name, "format_mismatch": target.with_suffix(".pdf"),
    }.get(scenario, target)
    if scenario == "exists":
        target.touch()
    if scenario == "unknown_source":
        backend.snapshot_ax_tree_override.pop("document_path")
        state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
        result = cu.run(registry.execute("computer_act", {
            "snapshot_id": state, "opens_dialog": True, "save_as_intent": str(path),
            "actions": [{"type": "click", "element_ref": "snapshot-1:open"}],
        }))
    else:
        result = open_panel(path, opens_dialog=scenario != "no_opener")
    assert result["code"] == code
    assert not cards
    assert not any(name in {"act", "takeover_begin", "plan_actions"} for name, _ in backend.calls)


def test_save_as_card_persistence_keeps_only_complete_visible_paths(workflow):
    from agent.runtime.approval_inbox import safe_approval_request

    _, _, _, cards, _, open_panel, _ = workflow
    assert not open_panel()["error"]
    card = cards[0]
    clean = safe_approval_request(card)
    assert clean["arguments"]["save_as_intent"] == card["arguments"]["save_as_intent"]
    assert clean["approval_boundary"] == card["approval_boundary"]
    assert clean["choices"] == ["once", "deny"]
    card["arguments"]["save_as_intent"] = {"source": "/some/file"}
    scrubbed = safe_approval_request(card)
    assert scrubbed["arguments"]["save_as_intent"] == {}
    assert scrubbed["choices"] == ["deny"]


def test_save_as_intent_does_not_wrap_an_unrelated_command(workflow, registry):
    _, target, _, cards, _, _, _ = workflow
    state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
    result = cu.run(registry.execute("computer_act", {
        "snapshot_id": state, "opens_dialog": True, "save_as_intent": str(target),
        "actions": [{"type": "wait", "duration_ms": 20}],
    }))
    assert result["code"] == "invalid_arguments"
    assert cards == []


@pytest.mark.parametrize("cutoff", ["focus_switch", "second_panel", "close_panel", "handoff"])
def test_save_as_intent_does_not_survive_panel_identity_changes(workflow, backend, registry, monkeypatch, cutoff):
    _, target, _, cards, act, open_panel, bind_panel = workflow
    assert not open_panel()["error"]
    bind_panel()
    if cutoff == "focus_switch":
        result = cu.run(registry.execute("computer_focus", {"app_ref": "app-1", "window_ref": "other-ref"}))
        assert not result["error"]
    elif cutoff == "second_panel":
        with monkeypatch.context() as another:
            app_list = backend.apps

            async def two_panels():
                catalog = await app_list()
                app = dict(catalog.apps[0])
                app["windows"] = [*app["windows"], {
                    **app["windows"][0], "window_ref": "another", "window_identity_ref": "another-panel",
                }]
                return cu.ComputerAppCatalog(catalog.generation, (app,))

            another.setattr(backend, "apps", two_panels)
            assert not cu.run(registry.execute("computer_apps", {}))["error"]
    elif cutoff == "close_panel":
        with monkeypatch.context() as another:
            app_list = backend.apps

            async def panel_gone():
                catalog = await app_list()
                app = dict(catalog.apps[0])
                app["windows"] = app["windows"][1:]
                return cu.ComputerAppCatalog(catalog.generation, (app,), ("panel",))

            another.setattr(backend, "apps", panel_gone)
            assert not cu.run(registry.execute("computer_apps", {}))["error"]
    else:
        assert not cu.run(registry.execute("computer_handoff", {}))["error"]
        assert not cu.run(registry.execute("computer_resume", {"app_ref": "app-1", "window_ref": "window-1"}))["error"]
    state = bind_panel()
    assert not act(state, {"type": "type", "element_ref": "snapshot-1:name", "text": target.name})["error"]
    assert len(cards) == 2


def test_save_as_intent_does_not_authorize_cloud_save(workflow, backend, registry):
    _, _, _, cards, act, open_panel, bind_panel = workflow
    assert not open_panel()["error"]
    bind_panel()
    backend.snapshot_ax_tree_override["children"].append({
        "role": "AXButton", "label": "保存至云文档", "element_ref": "snapshot-1:cloud",
    })
    state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
    assert not act(state, {"type": "click", "element_ref": "snapshot-1:cloud"})["error"]
    assert len(cards) == 2 and cards[-1]["choices"] == ["once", "deny"]


def test_save_as_intent_rejects_nonlocal_directory_before_approval(workflow, monkeypatch):
    from agent.runtime import computer_save_as_intent

    _, _, _, cards, _, open_panel, _ = workflow
    monkeypatch.setattr(computer_save_as_intent, "_local_directory", lambda _: False)
    assert open_panel()["code"] == "save_destination_unverified"
    assert cards == []


@pytest.mark.parametrize("fault", ["native_failure", "unguarded_return", "unguarded_click"])
def test_save_as_stops_after_failure_or_unverified_submit(workflow, backend, monkeypatch, fault):
    _, target, _, cards, act, open_panel, bind_panel = workflow
    assert not open_panel()["error"]
    state = bind_panel()
    edit = {"type": "type", "element_ref": "snapshot-1:name", "text": target.name}
    if fault == "native_failure":
        async def fail_before_input(*_args, **_kwargs):
            raise cu.ComputerSessionError("stale_snapshot")

        with monkeypatch.context() as boundary:
            boundary.setattr(backend, "act", fail_before_input)
            result = act(state, edit)
        assert result["code"] == "stale_snapshot"
    else:
        action = (
            {"type": "keypress", "key": "return"} if fault == "unguarded_return"
            else {"type": "click", "element_ref": "snapshot-1:save"}
        )
        result = act(state, action)
        assert result["code"] == "save_destination_unverified"
    assert len(cards) == 1
    state = bind_panel()
    assert not act(state, edit)["error"]
    assert len(cards) == 2
