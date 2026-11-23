"""Exercise a native WPS Save As, separate dialog and final Save via the real tool registry."""

import json
import os
from zipfile import ZipFile
from xml.sax.saxutils import escape

import pytest

from tests import test_computer_tools as cu

backend, manager, registry = cu.backend, cu.manager, cu.registry
pytestmark = pytest.mark.skipif(os.name != "posix", reason="native file readback requires POSIX file descriptors")
CAP = "wps_panel_ax_value_v1"


def body(path, lines):
    with ZipFile(path, "w") as package:
        package.writestr(
            "_rels/.rels",
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="r" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        content = "".join(f"<w:p><w:r><w:t>{escape(line)}</w:t></w:r></w:p>" for line in lines)
        package.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{content}</w:body></w:document>",
        )


def doc(name):
    return {"role": "AXWindow", "subrole": "AXStandardWindow", "title": name, "observation_capabilities": [CAP]}


def find_dialog(find, replacement, *, conflicting=False):
    rows = []
    for y, (label, value) in enumerate((("查找内容(N)", find), ("替换为(I)", replacement))):
        rows.extend(
            [
                {"role": "AXStaticText", "value": label, "bounds": {"x": 5, "y": y * 40, "width": 65, "height": 16}},
                {
                    "role": "AXPopUpButton",
                    "title": "查找内容(N)" if conflicting and y else value,
                    "bounds": {"x": 80, "y": y * 40, "width": 360, "height": 24},
                    "children": [
                        {"role": "AXTextField", "enabled": True, "value": value, "element_ref": f"edit-{y}"},
                    ],
                },
            ]
        )
    return {
        "role": "AXWindow",
        "subrole": "AXDialog",
        "title": "查找和替换",
        "observation_capabilities": [CAP],
        "children": [
            {"role": "AXGroup", "children": rows},
            {
                "role": "AXButton",
                "enabled": True,
                "actions": ["AXPress"],
                "element_ref": "replace-once",
                "title": "替换(R)",
                "bounds": {"x": 100, "y": 120, "width": 70, "height": 24},
            },
        ],
    }


@pytest.mark.parametrize(
    "case",
    [
        "verified",
        "other_paragraph_changed",
        "duplicate",
        "case_alias",
        "width_alias",
        "pattern",
        "pattern_extra_note_story",
        "extra_note_story",
        "header",
        "case_only",
        "missing_field_value",
        "invalid_same_text",
        "no_replace_readback_grant",
        "overlap",
        "unrelated_doc",
        "not_approved_copy",
        "diff_limit",
        "ambiguous_fields",
        "mixed_batch",
        "double_click",
        "pointer_hit",
        "changed_during_approval",
        "absent_target",
    ],
)
def test_wps_replace_approval_and_saved_paragraph_changes(
    registry, manager, backend, tmp_path, monkeypatch, case
):
    destination = tmp_path / "copy.docx"
    before = ["first", "unique seed", "last"]
    needle = "seed"
    if case == "duplicate":
        before[-1] = "another seed"
    if case == "diff_limit":
        before = ["旧" * 220] + [f"before {i}" for i in range(1, 22)]
    if case == "case_alias":
        before[-1] = "another SEEd"
    if case == "width_alias":
        before[-1] = "another Ｓｅｅｄ"
    if case == "case_only":
        before[1] = "unique SEED"
    if case in {"pattern", "pattern_extra_note_story"}:
        before[1], needle = "unique s??d", "s??d"
    if case == "overlap":
        before[1], needle = "aaa", "aa"
    if case == "absent_target":
        needle = "absent"
    dialog = find_dialog(needle, needle if case == "invalid_same_text" else "changed", conflicting=case == "ambiguous_fields")
    if case == "missing_field_value":
        dialog["children"][0]["children"][3]["children"][0].pop("value")
    backend.catalog_app_version = "12.1.26026"
    backend.catalog_bundle_id = "com.kingsoft.wpsoffice.mac"
    current_catalog = backend.apps

    async def windows():
        result = await current_catalog()
        group = result.apps[0]
        win = group["windows"][0]
        if win["title"] == "另存为":
            group["windows"].append(
                {**win, "window_ref": "source", "window_identity_ref": "doc-id", "title": "source.docx"}
            )
        if win["title"] == "查找和替换":
            win["focused_dialog_document_identity_ref"] = "unrelated-id" if case == "unrelated_doc" else "doc-id"
            group["windows"].append(
                {
                    **win,
                    "window_ref": "doc",
                    "window_identity_ref": "doc-id",
                    "title": "copy.docx",
                    "document_path": str(destination),
                }
            )
        return result

    monkeypatch.setattr(backend, "apps", windows)
    cu.register(registry, manager)
    asked = []

    async def allow(card):
        asked.append(card)
        if (
            case == "changed_during_approval"
            and backend.catalog_window_title == "查找和替换"
            and card.get("kind") == "computer_write"
        ):
            body(destination, ["an unrelated intervening edit"])
        return "once"

    registry.set_approval_handler(allow)

    def observe(title, identity, tree):
        backend.catalog_window_title, backend.catalog_window_identity_ref = title, identity
        backend.snapshot_ax_tree_override = tree
        cu.focus(registry)
        result = cu.run(registry.execute("computer_snapshot", {}))
        assert not result["error"], result
        return json.loads(result["fresh_output"])["snapshot_id"]

    if case != "not_approved_copy":
        observe("source.docx", "doc-id", doc("source.docx"))
        panel = cu._wps_new_copy_panel(tmp_path)
        if case != "no_replace_readback_grant":
            panel["observation_capabilities"].append(CAP)
        snapshot = observe("另存为", "panel-id", panel)
        copy = {"type": "click", "element_ref": "snapshot-1:save", "save_new_copy_to": str(destination)}
        result = cu.run(registry.execute("computer_act", {"snapshot_id": snapshot, "actions": [copy]}))
        assert not result["error"], result
        assert asked[-1]["arguments"]["saved_copy_readback"] is True
        assert bool(asked[-1]["arguments"].get("wps_single_replace_readback")) == (case != "no_replace_readback_grant")
    def write_document(paragraphs):
        body(destination, paragraphs)
        if case == "header":
            with ZipFile(destination, "a") as package:
                package.writestr(
                    "word/_rels/document.xml.rels",
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="header" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" '
                    'Target="header1.xml"/></Relationships>',
                )
                package.writestr(
                    "word/header1.xml",
                    '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    '<w:p><w:r><w:t>seed in header</w:t></w:r></w:p></w:hdr>',
                )

    write_document(before)
    if case in {"extra_note_story", "pattern_extra_note_story"}:
        with ZipFile(destination, "a") as package:
            package.writestr(
                "word/endnotes.xml",
                '<w:endnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:endnote w:id="2"><w:p><w:r><w:t>another seed</w:t></w:r></w:p></w:endnote></w:endnotes>',
            )
    def save_document(paragraphs):
        snap = observe("copy.docx", "doc-id", doc("copy.docx"))
        save_act = backend.act

        async def save(*args, **kwargs):
            reply = await save_act(*args, **kwargs)
            write_document(paragraphs)
            return reply

        with monkeypatch.context() as save_fixture:
            save_fixture.setattr(backend, "act", save)
            result = cu.run(registry.execute("computer_act", {"snapshot_id": snap, "actions": [
                {"type": "keypress", "key": "s", "modifiers": ["command"]},
            ]}))
        assert not result["error"], result
        return result, json.loads(result["fresh_output"])["saved_document"]

    has_baseline = case in {"duplicate", "diff_limit", "header", "case_only", "missing_field_value"}
    if has_baseline:
        first_save, first_evidence = save_document(before)
        if case == "diff_limit":
            after = ["新" * 230] + [f"after {i}" for i in range(1, 22)]
            _, evidence = save_document(after)
            changes = evidence["paragraph_changes"]
            assert changes["truncated"] is True and len(changes["changes"]) == 20
            assert changes["changes"][0] == {"paragraph": 1, "before": "旧" * 200, "after": "新" * 200}
            assert changes["changes"][-1]["paragraph"] == 20
            _, repeated = save_document(after)
            assert repeated["paragraph_changes"] == {"status": "compared", "changes": [], "truncated": False}
            assert first_evidence["paragraph_changes"] == {"status": "no_baseline"}
            return
        assert first_save["computer_receipt"]["next_step"] == "review_saved_document"

    observe("copy.docx", "doc-id", doc("copy.docx"))
    snap = observe("查找和替换", "dialog-id", dialog)
    actions = [{"type": "click", "element_ref": "replace-once"}]
    if case == "double_click":
        actions = [{"type": "double_click", "element_ref": "replace-once"}]
    if case == "pointer_hit":
        actions = [{"type": "click", "x": 105, "y": 121}]
    if case == "mixed_batch":
        actions.append({"type": "wait", "duration_ms": 1})
    acted = sum(name == "act" for name, _ in backend.calls)
    approvals = len(asked)
    result = cu.run(registry.execute("computer_act", {"snapshot_id": snap, "actions": actions}))
    if case in {"mixed_batch", "double_click", "pointer_hit"}:
        assert result["code"] == "replacement_requires_single_action", result
        assert sum(name == "act" for name, _ in backend.calls) == acted
        return
    assert not result["error"], result
    assert len(asked) == approvals + 1 and asked[-1]["kind"] == "computer_write"
    assert asked[-1]["choices"] == ["once", "session", "deny"]
    assert sum(name == "act" for name, _ in backend.calls) == acted + 1
    if case in {"unrelated_doc", "not_approved_copy"}:
        return

    snap = observe("查找和替换", "dialog-id", dialog)
    assert (
        cu.run(registry.execute("computer_act", {"snapshot_id": snap, "actions": actions}))["code"] == "effect_pending"
    )
    updated = list(before)
    updated[1] = "unique changed"
    if case == "other_paragraph_changed":
        updated[-1] = "extra"
    result, evidence = save_document(updated)
    if case in {"verified", "other_paragraph_changed"}:
        assert evidence["replacement_verification"]["status"] == ("verified" if case == "verified" else "mismatch")
        assert result["computer_receipt"]["next_step"] == (
            "review_saved_document" if case == "verified" else "report_replacement_mismatch"
        )
    else:
        assert "replacement_verification" not in evidence
    if has_baseline:
        assert evidence["paragraph_changes"] == {
            "status": "compared", "changes": [{"paragraph": 2, "before": before[1], "after": "unique changed"}],
            "truncated": False,
        }
        assert first_evidence["paragraph_changes"] == {"status": "no_baseline"}
    else:
        assert evidence["paragraph_changes"] == {"status": "no_baseline"}
    # A valid Save readback releases the one-replacement-at-a-time guard.
    snap = observe("查找和替换", "dialog-id", find_dialog("changed", "again"))
    result = cu.run(registry.execute("computer_act", {"snapshot_id": snap, "actions": actions}))
    assert not result["error"], result


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([["change se", "ed now"], ["unchanged"]], ["change changed now", "unchanged"]),
        ([["se"], ["ed"]], None),
    ],
)
def test_docx_matching_joins_runs_only_within_each_paragraph(tmp_path, runs, expected):
    from agent.runtime.computer_saved_document import ApprovedCopy
    from dataclasses import replace

    destination = tmp_path / "copy.docx"
    body(destination, [])
    parts = {}
    with ZipFile(destination) as old:
        for name in old.namelist():
            parts[name] = old.read(name)
    paragraphs = "".join(
        "<w:p>" + "".join(f"<w:r><w:t>{escape(text)}</w:t></w:r>" for text in group) + "</w:p>" for group in runs
    )
    parts["word/document.xml"] = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paragraphs}</w:body></w:document>"
    ).encode()
    with ZipFile(destination, "w") as new:
        for name, value in parts.items():
            new.writestr(name, value)
    grant = ApprovedCopy.bind(str(destination), "session", "grant", ("window", "source.docx"))
    assert grant is not None
    proof, reason = replace(grant, allow_replace_readback=True).read_for_replacement("seed", "changed")
    if expected is None:
        assert proof is None and reason == "no_match"
    else:
        assert proof is not None and list(proof.expected) == expected and not reason
