"""Public Computer Use approval and dispatch across independent native Save attempts."""
import asyncio
import json
import os

import pytest

from agent.runtime.computer_backend import ComputerActionResult, ComputerAppCatalog
from agent.runtime.computer_protocol import ComputerError, ComputerErrorCode
from tests import test_computer_tools as cu
from tests.test_computer_tools import _wps_new_copy_panel, focus, register, run

# Use the same registered host and fake native adapter as the general CU suite.
backend, manager, registry = cu.backend, cu.manager, cu.registry

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Native Save uses POSIX filesystem semantics")


def _snapshot(reg):
    result = run(reg.execute("computer_snapshot", {}))
    assert not result["error"], result
    return json.loads(result["fresh_output"])["snapshot_id"]


def _save(reg, destination=None):
    action = {"type": "keypress", "key": "return"}
    if destination is not None:
        action["save_new_copy_to"] = str(destination)
    return run(reg.execute("computer_act", {"snapshot_id": _snapshot(reg), "actions": [action]}))


def _panel(reg, session, native, tmp_path, *, bundle="com.kingsoft.wpsoffice.mac", known_directory=True):
    native.catalog_bundle_id = bundle
    native.catalog_window_title = "Save As"
    tree = _wps_new_copy_panel(tmp_path)
    if not known_directory:
        tree["standard_save_panel"].pop("directory")
        tree["standard_save_panel"]["directory_status"] = "display_name_only"
    native.snapshot_ax_tree_override = tree
    cards = []
    async def approve(card):
        cards.append(card)
        return "once"
    register(reg, session)
    reg.set_approval_handler(approve)
    focus(reg)
    return cards


@pytest.mark.parametrize("mode", ["ordinary_unknown", "ordinary_existing", "guarded_new", "guarded_existing"])
def test_standard_save_outside_wps_keeps_ordinary_semantics_and_never_grants_wps_readback(registry, manager, backend, tmp_path, mode):
    destination = tmp_path / "copy.docx"
    if mode.endswith("existing"):
        destination.write_text("existing")
    cards = _panel(registry, manager, backend, tmp_path, bundle="com.apple.TextEdit", known_directory=mode != "ordinary_unknown")
    result = _save(registry, destination if mode.startswith("guarded") else None)
    if mode == "guarded_existing":
        assert result["code"] == "save_destination_exists"
        assert not any(key == "act" for key, _ in backend.calls)
    else:
        assert not result["error"], result
        assert sum(key == "act" for key, _ in backend.calls) == 1
    if mode == "guarded_new":
        assert cards[-1]["arguments"]["known_path"] == str(destination)
        assert not cards[-1]["arguments"].get("saved_copy_readback")
        assert "main-body text" not in cards[-1]["approval_effect"]


def test_old_save_panel_and_active_format_confirmation_do_not_allow_duplicate_new_copy(
        registry, manager, backend, tmp_path, monkeypatch):
    destination = tmp_path / "copy.docx"
    phase = "first"
    apps = backend.apps
    async def catalog():
        result = await apps()
        app = dict(result.apps[0])
        panel = dict(app["windows"][0], focused_dialog_document_identity_ref="document")
        panel["window_identity_ref"] = "panel-1" if phase == "first" else "panel-2"
        app["windows"] = [panel, {**app["windows"][1], "window_identity_ref": "document"}]
        if phase == "format":
            app["windows"].append({**panel, "window_ref": "format-window", "window_identity_ref": "format",
                                  "parent_window_identity_ref": "document", "title": "Confirm format"})
        absent = {"first": (), "format": ("panel-1",), "cancelled": ("format",)}[phase]
        return ComputerAppCatalog(result.generation, (app,), confirmed_absent_window_identity_refs=absent)
    monkeypatch.setattr(backend, "apps", catalog)
    _panel(registry, manager, backend, tmp_path)
    assert not _save(registry, destination)["error"]
    assert _save(registry, destination)["code"] == "effect_pending"
    phase = "format"
    focus(registry)
    assert _save(registry, destination)["code"] == "effect_pending"
    phase = "cancelled"
    focus(registry)
    assert not destination.exists()
    assert not _save(registry, destination)["error"]
    assert sum(key == "act" for key, _ in backend.calls) == 2


@pytest.mark.parametrize("outcome", ["preinput_failure", "unknown_then_closed"])
def test_new_copy_retry_keeps_unknown_input_scoped_to_the_old_panel(registry, manager, backend, tmp_path, monkeypatch, outcome):
    _panel(registry, manager, backend, tmp_path)
    destination = tmp_path / "copy.docx"
    successful = backend.act_result
    cause = ComputerErrorCode.SAVE_DESTINATION_UNVERIFIED if outcome == "preinput_failure" else ComputerErrorCode.UNKNOWN_OUTCOME
    backend.act_result = ComputerActionResult(error=ComputerError(cause, cause.value),
        result={"outcomes": [], "last_acknowledged_action": -1})
    first = _save(registry, destination)
    assert first["code"] == cause.value
    backend.act_result = successful
    if outcome == "unknown_then_closed":
        focus(registry)
        assert _save(registry, destination)["code"] == "effect_pending"
        original = backend.apps
        async def fresh_catalog():
            result = await original()
            result.apps[0]["windows"][0]["window_identity_ref"] = "replacement-panel"
            return ComputerAppCatalog(result.generation, result.apps, confirmed_absent_window_identity_refs=("identity-a",))
        monkeypatch.setattr(backend, "apps", fresh_catalog)
        focus(registry)
    assert not _save(registry, destination)["error"]


@pytest.mark.parametrize("guarded_first", [True, False])
def test_parallel_native_save_intents_only_dispatch_once(registry, manager, backend, tmp_path, monkeypatch, guarded_first):
    cards = _panel(registry, manager, backend, tmp_path)
    first_snapshot = _snapshot(registry)
    plain = {"type": "click", "element_ref": "snapshot-1:save"}
    guarded = {"type": "keypress", "key": "return", "save_new_copy_to": str(tmp_path / "copy.docx")}

    async def race():
        delivered, release = asyncio.Event(), asyncio.Event()
        dispatch = manager.act

        async def delayed_receipt(*args, **kwargs):
            # The application received Save, but the first caller has no receipt yet.
            result = await dispatch(*args, **kwargs)
            delivered.set()
            await release.wait()
            return result

        monkeypatch.setattr(manager, "act", delayed_receipt)
        first = asyncio.create_task(registry.execute("computer_act", {
            "snapshot_id": first_snapshot, "actions": [guarded if guarded_first else plain],
        }))
        try:
            await asyncio.wait_for(delivered.wait(), 3)
            fresh = json.loads((await registry.execute("computer_snapshot", {}))["fresh_output"])
            approvals = len(cards)
            second = await registry.execute("computer_act", {
                "snapshot_id": fresh["snapshot_id"], "actions": [plain if guarded_first else guarded],
            })
            assert second["code"] == "effect_pending", second
            assert second["computer_receipt"]["dispatch_state"] == "not_dispatched"
            assert len(cards) == approvals
        finally:
            release.set()
        assert (await first)["computer_receipt"]["dispatch_state"] == "acknowledged"

    run(race())
    assert sum(key == "act" for key, _ in backend.calls) == 1


def test_cancelled_caller_cannot_repeat_a_delivered_save_until_the_panel_is_gone(
        registry, manager, backend, tmp_path, monkeypatch):
    _panel(registry, manager, backend, tmp_path)
    destination = tmp_path / "copy.docx"
    snap = _snapshot(registry)
    original_act = manager.act

    async def cancel_after_input():
        delivered, hold = asyncio.Event(), asyncio.Event()
        async def interrupted(*args, **kwargs):
            result = await original_act(*args, **kwargs)
            delivered.set()
            await hold.wait()
            return result

        monkeypatch.setattr(manager, "act", interrupted)
        action = {"type": "keypress", "key": "return", "save_new_copy_to": str(destination)}
        call = asyncio.create_task(registry.execute("computer_act", {"snapshot_id": snap, "actions": [action]}))
        await asyncio.wait_for(delivered.wait(), 3)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    run(cancel_after_input())
    monkeypatch.setattr(manager, "act", original_act)
    # Even when the current folder label is all a reduced panel publishes,
    # the same stable native panel cannot replay the interrupted input.
    panel = _wps_new_copy_panel(tmp_path)
    panel["standard_save_panel"].pop("directory")
    panel["standard_save_panel"]["directory_status"] = "display_name_only"
    backend.snapshot_ax_tree_override = panel
    focus(registry)
    assert _save(registry)["code"] == "effect_pending"
    previous_apps = backend.apps
    async def new_panel():
        result = await previous_apps()
        result.apps[0]["windows"][0]["window_identity_ref"] = "new-panel"
        return ComputerAppCatalog(result.generation, result.apps, confirmed_absent_window_identity_refs=("identity-a",))
    monkeypatch.setattr(backend, "apps", new_panel)
    backend.snapshot_ax_tree_override = _wps_new_copy_panel(tmp_path)
    focus(registry)
    assert not _save(registry, destination)["error"]
    assert sum(key == "act" for key, _ in backend.calls) == 2
