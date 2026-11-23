"""Save As workflow approvals exercised through the Computer Use tool registry."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

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

    def bind_panel(*, other=False, directory=None, title="另存为"):
        panel = cu._wps_new_copy_panel(directory or target.parent)
        panel["title"] = title
        panel["observation_capabilities"] += ["focus_consent_v1", "focus_loan_v3"]
        panel["children"].append({"role": "AXButton", "label": "Export PDF", "element_ref": "snapshot-1:export"})
        backend.snapshot_ax_tree_override = panel
        backend.catalog_window_title = title
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


@pytest.fixture
def bind_directory_sheet(backend, registry, monkeypatch):
    def bind(*, parent="panel", role="AXSheet", another_sheet=False, another_field=False, with_press=True, help_text=""):
        panel_catalog = backend.apps

        async def apps():
            catalog = await panel_catalog()
            app = dict(catalog.apps[0])
            panel = app["windows"][0]
            sheet = {**panel, "window_ref": "sheet-ref", "window_identity_ref": "sheet",
                     "role": role, "title": "", "parent_window_identity_ref": parent}
            app["windows"] = [*app["windows"], sheet]
            if another_sheet:
                app["windows"].append({**sheet, "window_ref": "other-sheet-ref", "window_identity_ref": "other-sheet"})
            return cu.ComputerAppCatalog(catalog.generation, (app,))

        field = {"role": "AXTextField", "element_ref": "directory", "focused": True, "enabled": True}
        backend.snapshot_ax_tree_override = {
            "role": "AXSheet",
            "observation_capabilities": ["focus_consent_v1", "focus_loan_v3", "focus_loan_sheet_v1", "replace_text_v1"],
            "children": [
                field,
                *([{**field, "element_ref": "extra-field", "focused": False}] if another_field else []),
                {"role": "AXButton", "element_ref": "go", "title": "前往", "actions": ["AXPress"] if with_press else [],
                 "help": help_text, "enabled": True},
                {"role": "AXButton", "element_ref": "cancel", "title": "Cancel", "actions": ["AXPress"], "enabled": True},
                {"role": "AXButton", "element_ref": "other", "title": "Options", "actions": ["AXPress"], "enabled": True},
            ],
        }
        monkeypatch.setattr(backend, "apps", apps)
        cu.run(registry.execute("computer_apps", {}))
        return snapshot(cu.run(registry.execute("computer_get_app_state", {
            "app_ref": "app-1", "window_ref": "sheet-ref",
        })))
    return bind


@pytest.mark.parametrize("operation", ["whole_value", "select_and_type", "return", "go", "cancel", "wait", "foreground"])
def test_save_as_sheet_actions_keep_the_exact_destination_approval(workflow, bind_directory_sheet, registry, operation):
    _, target, _, cards, act, open_panel, bind_panel = workflow
    assert not open_panel()["error"]
    bind_panel()
    state = bind_directory_sheet()
    typed = {"type": "type", "text": str(target.parent), "element_ref": "directory"}
    actions = {
        "whole_value": [dict(typed, replace=True)],
        "select_and_type": [
            {"type": "keypress", "key": "a", "modifiers": ["command"], "element_ref": "directory"}, typed,
        ],
        "return": [{"type": "keypress", "key": "enter"}],
        "go": [{"type": "click", "element_ref": "go"}],
        "cancel": [{"type": "click", "element_ref": "cancel"}],
        "wait": [{"type": "wait", "duration_ms": 10}],
        "foreground": [dict(typed, replace=True)],
    }[operation]
    result = cu.run(registry.execute("computer_act", {
        "snapshot_id": state, "actions": actions,
        "interaction_mode": "foreground_takeover" if operation == "foreground" else "background",
    }))
    assert not result["error"], result
    expected = 2 if operation == "foreground" else 1
    assert len(cards) == expected
    if operation == "foreground":
        assert cards[-1]["choices"] == ["once", "session", "deny"]
    edited = act(bind_panel(), {"type": "type", "element_ref": "snapshot-1:name", "text": target.name})
    saved = act(snapshot(edited), {"type": "click", "element_ref": "snapshot-1:save", "save_new_copy_to": str(target)})
    assert not saved["error"], saved
    assert len(cards) == expected


@pytest.mark.parametrize("fault", [
    "other_directory", "other_parent", "wrong_role", "two_sheets", "two_fields",
    "select_only", "append", "modified_return", "no_axpress", "other_button", "element_keyword",
])
def test_save_as_sheet_refusals_return_to_ordinary_approval(workflow, bind_directory_sheet, fault):
    _, target, _, cards, act, open_panel, bind_panel = workflow
    assert not open_panel()["error"]
    bind_panel()
    state = bind_directory_sheet(
        parent="source" if fault == "other_parent" else "panel",
        role="AXWindow" if fault == "wrong_role" else "AXSheet",
        another_sheet=fault == "two_sheets", another_field=fault == "two_fields",
        with_press=fault != "no_axpress", help_text="Export PDF" if fault == "element_keyword" else "",
    )
    text = {"type": "type", "replace": True, "element_ref": "directory",
            "text": str(target.parent.parent if fault == "other_directory" else target.parent)}
    action = {
        "select_only": {"type": "keypress", "key": "a", "modifiers": ["command"], "element_ref": "directory"},
        "append": {key: value for key, value in text.items() if key != "replace"},
        "modified_return": {"type": "keypress", "key": "return", "modifiers": ["shift"]},
        "no_axpress": {"type": "click", "element_ref": "go"},
        "other_button": {"type": "click", "element_ref": "other"},
        "element_keyword": {"type": "click", "element_ref": "go"},
    }.get(fault, text)
    result = act(state, action, interaction_mode="foreground_takeover")
    assert not result["error"], result
    assert len(cards) == 2 and cards[-1]["choices"] == ["once", "deny"]
    assert not act(bind_panel(), {"type": "type", "element_ref": "snapshot-1:name", "text": target.name})["error"]
    assert len(cards) == 3


@pytest.mark.parametrize(("path_syntax", "deferred"), [
    ("absolute", False), ("home_alias", False), ("tmp_alias", False), ("absolute", True),
])
def test_save_as_panel_loan_keeps_two_cards_across_its_attached_sheet(
    workflow, bind_directory_sheet, registry, backend, monkeypatch, path_syntax, deferred,
):
    source, _, clock, cards, act, open_panel, bind_panel = workflow
    expected_body = ["Shift handover", "Outbound clearance is pending."]
    body(source, expected_body)
    original = source.read_bytes()
    root = Path.home() if path_syntax == "home_alias" else Path("/private/tmp")
    begins = []
    native_begin = backend.begin_takeover

    async def record_begin(*args, **kwargs):
        begins.append(dict(kwargs))
        kwargs.pop("continued_loan_to_sheet", None)
        return await native_begin(*args, **kwargs)

    monkeypatch.setattr(backend, "begin_takeover", record_begin)

    async def answer(card):
        cards.append(card)
        return "session" if len(cards) == 2 and "session" in card["choices"] else "once"

    registry.set_approval_handler(answer)
    with TemporaryDirectory(prefix="astra-save-as-intent-", dir=root) as temporary:
        directory = Path(temporary).resolve(strict=True)
        destination = directory / "copy.docx"
        typed = {
            "absolute": str(directory),
            "home_alias": f"~/{directory.name}",
            "tmp_alias": f"/tmp/{directory.name}",
        }[path_syntax]
        assert not open_panel(destination)["error"]
        opening = act(bind_panel(directory=directory),
                      {"type": "keypress", "key": "g", "modifiers": ["command", "shift"]},
                      interaction_mode="foreground_takeover")
        assert not opening["error"], opening
        assert len(cards) == 2 and cards[1]["choices"] == ["once", "session", "deny"]
        panel_catalog = backend.apps
        panel_tree = backend.snapshot_ax_tree_override
        native_act = backend.act
        native_snapshot = backend.snapshot
        sheet_closed = False
        defer_parent_read = deferred

        async def simulate_sheet_and_save(target, snapshot_id, actions, **kwargs):
            nonlocal sheet_closed
            receipt = await native_act(target, snapshot_id, actions, **kwargs)
            if target.window_ref == "sheet-ref" and actions[0].key == "return":
                sheet_closed = True
                backend.snapshot_ax_tree_override = panel_tree
                monkeypatch.setattr(backend, "apps", panel_catalog)
                return cu.ComputerActionResult(result={"last_acknowledged_action": 0, "outcomes": [{
                    "index": 0, "ok": True, "effect_verification": "unverified", "observation_required": True,
                }]})
            if actions[0].save_new_copy_to:
                assert target.window_ref == "window-1" and sheet_closed
                body(destination, expected_body)
            return receipt

        async def capture(target, *args, **kwargs):
            nonlocal defer_parent_read
            if sheet_closed and target.window_ref == "sheet-ref":
                raise cu.ComputerSessionError("target_not_frontmost")
            if sheet_closed and target.window_ref == "window-1" and defer_parent_read:
                defer_parent_read = False
                raise cu.ComputerSessionError("snapshot_failed")
            return await native_snapshot(target, *args, **kwargs)

        monkeypatch.setattr(backend, "act", simulate_sheet_and_save)
        monkeypatch.setattr(backend, "snapshot", capture)
        clock[0] += 1
        filled = act(bind_directory_sheet(), {"type": "type", "text": typed, "replace": True, "element_ref": "directory"},
                     interaction_mode="foreground_takeover")
        assert not filled["error"], filled
        assert len(cards) == 2
        clock[0] += 1
        accepted = act(snapshot(filled), {"type": "keypress", "key": "return"}, interaction_mode="foreground_takeover")
        assert accepted["computer_receipt"]["dispatch_state"] == "acknowledged"
        if deferred:
            assert accepted["code"] == "post_action_observation_pending", accepted
            assert accepted["committed_no_replay"] is True
            transition = accepted["details"]["window_transition"]
            observe = transition["next_observation"]
            panel_state = snapshot(cu.run(registry.execute(observe["tool"], observe["arguments"])))
        else:
            assert not accepted["error"], accepted
            transition = json.loads(accepted["fresh_output"])["window_transition"]
            panel_state = snapshot(accepted)
        assert transition["observation_reason"] == "sheet_parent"
        edited = act(panel_state, {"type": "type", "element_ref": "snapshot-1:name", "text": destination.name})
        assert not edited["error"], edited
        saved = act(snapshot(edited), {"type": "click", "element_ref": "snapshot-1:save", "save_new_copy_to": str(destination)})
        assert not saved["error"], saved
        with ZipFile(destination) as saved_docx:
            assert saved_docx.testzip() is None
            with ZipFile(source) as source_docx:
                assert saved_docx.read("word/document.xml") == source_docx.read("word/document.xml")
        assert source.read_bytes() == original
        assert len(cards) == 2
        assert len(begins) == 4
        assert [b.get("continued_loan") for b in begins] == [None, False, True, True]
        assert [b.get("continued_loan_to_sheet") for b in begins] == [None, None, True, True]
        assert begins[1]["loan_deadline_continuous_ms"] == begins[2]["loan_deadline_continuous_ms"] == begins[3]["loan_deadline_continuous_ms"]


def test_an_ordinary_panel_loan_does_not_cover_an_attached_sheet_without_intent(
    workflow, bind_directory_sheet, registry,
):
    _, target, _, cards, act, _, bind_panel = workflow

    async def answer(card):
        cards.append(card)
        return "session" if "session" in card["choices"] else "once"

    registry.set_approval_handler(answer)
    opened = act(bind_panel(title="Folder Picker"), {"type": "keypress", "key": "g", "modifiers": ["command", "shift"]},
                 interaction_mode="foreground_takeover")
    assert not opened["error"], opened
    assert cards[0]["choices"] == ["once", "session", "deny"]
    result = act(bind_directory_sheet(), {"type": "type", "text": str(target.parent), "replace": True, "element_ref": "directory"},
                 interaction_mode="foreground_takeover")
    assert not result["error"], result
    assert len(cards) == 2 and cards[1]["choices"] == ["once", "deny"]


def test_local_runtime_forwards_a_sheet_loan_to_the_session_manager(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from agent.runtime.tools.computer import LocalComputerRuntime

    runtime = LocalComputerRuntime(helper_path=None, cache_root=tmp_path / "cache")
    begin = AsyncMock(return_value="takeover")
    monkeypatch.setattr(runtime, "_ensure_manager", AsyncMock(return_value=type("Manager", (), {"begin_takeover": begin})()))

    def deadline():
        return 1000

    assert cu.run(runtime.begin_takeover("snapshot", "plan", loan_deadline=deadline, continued_loan=True,
                                        continued_loan_to_sheet=True)) == "takeover"
    begin.assert_awaited_once_with("snapshot", "plan", loan_deadline=deadline, continued_loan=True,
                                   continued_loan_to_sheet=True, focus_consent=None)


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
    ("not_directory", "save_destination_unverified"),
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
    result = open_panel(path, opens_dialog=scenario != "no_opener")
    assert result["code"] == code
    assert not cards
    assert not any(name in {"act", "takeover_begin", "plan_actions"} for name, _ in backend.calls)


@pytest.mark.parametrize("scenario", [
    "document", "file_menu", "no_extension", "different_extension", "other_app", "other_version",
    "no_stable_identity", "not_a_document",
])
def test_save_as_source_can_be_the_bound_wps_document_title(workflow, backend, registry, scenario):
    from agent.runtime.approval_inbox import safe_approval_request

    source, target, _, cards, act, _, bind_panel = workflow
    document = backend.snapshot_ax_tree_override
    document.pop("document_path")
    if scenario == "no_extension":
        backend.catalog_window_title = document["title"] = source.stem
    if scenario == "different_extension":
        target = target.with_suffix(".pdf")
    if scenario == "other_app":
        backend.catalog_bundle_id = "com.apple.TextEdit"
    if scenario == "other_version":
        backend.catalog_app_version = "12.1.26027"
    if scenario == "no_stable_identity":
        backend.catalog_window_identity_ref = ""
    if scenario == "not_a_document":
        document["subrole"] = "AXDialog"
    cu.focus(registry)
    state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
    if scenario == "file_menu":
        backend.snapshot_ax_tree_override = {
            "role": "AXMenuBarItem", "title": "文件",
            "observation_capabilities": document["observation_capabilities"],
            "children": document["children"],
        }
        state = snapshot(cu.run(registry.execute("computer_snapshot", {})))
    result = act(state, {"type": "click", "element_ref": "snapshot-1:open"}, opens_dialog=True, save_as_intent=str(target))
    if scenario not in {"document", "file_menu"}:
        assert result["code"] == "save_destination_unverified"
        assert not cards and not any(name == "act" for name, _ in backend.calls)
        return

    assert not result["error"], result
    assert len(cards) == 1 and cards[0]["choices"] == ["once", "deny"]
    disclosure = {
        "source": source.name + "（窗口标题，无文件路径）",
        "directory": str(target.parent), "filename": target.name,
    }
    assert cards[0]["arguments"]["save_as_intent"] == disclosure
    assert safe_approval_request(cards[0])["arguments"]["save_as_intent"] == disclosure
    state = bind_panel()
    edited = act(state, {"type": "type", "element_ref": "snapshot-1:name", "text": target.name})
    saved = act(snapshot(edited), {"type": "click", "element_ref": "snapshot-1:save", "save_new_copy_to": str(target)})
    assert not saved["error"], saved
    assert len(cards) == 1


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
