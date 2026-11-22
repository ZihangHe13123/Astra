from copy import deepcopy

from agent.runtime.computer_observation import observe_action_effect, window_transition


def node(value="before", ref="old"):
    return {
        "role": "AXTextField",
        "label": "Search",
        "element_ref": ref,
        "bounds": {"x": 1, "y": 2, "width": 40, "height": 20},
        "value": value,
    }


def test_observes_matching_control_without_leaking_values_or_reusing_refs():
    result = observe_action_effect(
        node("SECRET-BEFORE"), node("SECRET-AFTER", "new"), [{"type": "type", "element_ref": "old"}]
    )
    assert result == {"status": "changed", "evidence": "matching_control_value_changed"}
    assert "SECRET" not in str(result)


def test_ambiguous_control_and_missing_target_are_unknown():
    after = {"children": [node("a", "new1"), node("b", "new2")]}
    assert observe_action_effect(node(), after, [{"type": "click", "element_ref": "old"}])["status"] == "unknown"
    assert observe_action_effect(node(), {}, [{"type": "click", "element_ref": "old"}])["status"] == "unknown"
    assert observe_action_effect(node(), node(), [{"type": "click", "x": 1, "y": 2}]) is None


def test_tab_close_checks_requested_tab_not_generic_screen_changes():
    before = {
        "role": "AXRadioButton",
        "subrole": "AXTabButton",
        "title": "Probe",
        "children": [{"role": "AXButton", "title": "关闭标签页", "element_ref": "close"}],
    }
    actions = [{"type": "click", "element_ref": "close"}]
    assert observe_action_effect(before, deepcopy(before), actions) == {
        "status": "unchanged",
        "evidence": "requested_tab_still_present",
    }
    assert observe_action_effect(before, {}, actions) == {
        "status": "unknown",
        "evidence": "requested_tab_not_in_observed_tree",
    }


def test_unrelated_action_batches_do_not_infer_effects():
    assert observe_action_effect(node(), node("after"), [{"type": "wait"}]) is None
    assert observe_action_effect(node(), node("after"), [{"type": "click", "element_ref": "old"}] * 2) is None


def catalog(bundle="test", identities=("old", "new")):
    return [
        {
            "bundle_id": bundle,
            "app_ref": "fresh-app",
            "windows": [
                {"window_ref": "ref-" + identity, "window_identity_ref": identity, "title": identity, "bindable": True}
                for identity in identities
            ],
        }
    ]


def test_window_transition_uses_identity_and_exposes_only_same_app():
    result = window_transition(catalog() + catalog("other"), "test", "old", {"old"})
    assert result["status"] == "new_window_observed"
    assert result["next_observation"] == {
        "tool": "computer_get_app_state",
        "arguments": {"app_ref": "fresh-app", "window_ref": "ref-new"},
    }
    assert len(result["windows"]) == 2
    assert (
        window_transition(catalog(identities=("new",)), "test", "old", {"old"})["status"]
        == "previous_target_not_observed"
    )


def test_ambiguous_apps_or_windows_do_not_auto_select():
    assert "next_observation" not in window_transition(catalog() * 2, "test", "old", {"old"})
    assert "next_observation" not in window_transition(
        catalog(identities=("old", "new", "new2")), "test", "old", {"old"}
    )
    assert window_transition([], "test", "old", {"old"})["status"] == "application_not_observed"


def test_unmatched_panel_does_not_suggest_its_obscured_parent():
    apps = catalog(identities=("old",))
    parent = apps[0]["windows"][0]
    parent["bounds"] = {"x": 0, "y": 30, "width": 1352, "height": 768}
    panel = {"window_ref": "unmatched-panel", "title": "Open", "bindable": False,
             "binding_status": "ax_window_unmatched",
             "bounds": {"x": 236, "y": 190, "width": 880, "height": 448}}
    apps[0]["windows"].append(panel)
    result = window_transition(apps, "test", "old", {"old"})
    assert result["status"] == "unmatched_window_observed"
    assert result["unmatched_window_refs"] == ["unmatched-panel"]
    assert "next_observation" not in result
    panel.pop("bounds")
    assert "next_observation" not in window_transition(apps, "test", "old", {"old"})


def test_unrelated_unmatched_window_does_not_block_exact_previous_target():
    apps = catalog(identities=("old",))
    apps[0]["windows"][0]["bounds"] = {"x": 0, "y": 0, "width": 500, "height": 400}
    apps[0]["windows"].append({"window_ref": "other", "bindable": False,
        "bounds": {"x": 600, "y": 0, "width": 300, "height": 200}})
    result = window_transition(apps, "test", "old", {"old"})
    assert result["observation_reason"] == "exact_previous_target"
    assert result["next_observation"]["arguments"]["window_ref"] == "ref-old"


def test_new_bindable_panel_can_be_suggested_but_not_if_it_is_also_obscured():
    apps = catalog(identities=("old", "new"))
    for window in apps[0]["windows"]:
        window["bounds"] = {"x": 0, "y": 0, "width": 500, "height": 400}
    assert window_transition(apps, "test", "old", {"old"})["next_observation"]["arguments"]["window_ref"] == "ref-new"
    apps[0]["windows"].append({"window_ref": "unmatched", "bindable": False,
        "bounds": {"x": 20, "y": 20, "width": 300, "height": 200}})
    assert "next_observation" not in window_transition(apps, "test", "old", {"old"})


def test_duplicate_tab_titles_never_claim_requested_tab_still_present():
    tab = {
        "role": "AXRadioButton",
        "subrole": "AXTabButton",
        "title": "Same",
        "children": [{"role": "AXButton", "title": "Close Tab", "element_ref": "close"}],
    }
    other = {**tab, "children": []}
    result = observe_action_effect({"children": [tab, other]}, other, [{"type": "click", "element_ref": "close"}])
    assert result["status"] == "unknown"


def test_extremely_wide_or_deep_trees_do_not_produce_conclusive_evidence():
    root = {"children": [node(ref=str(i)) for i in range(4100)]}
    assert observe_action_effect(root, node("after"), [{"type": "click", "element_ref": "0"}])["status"] == "unknown"


def test_coordinate_click_observes_its_bound_control():
    from agent.runtime.computer_protocol import ComputerAction

    action = {"type": "click", "target_element_ref": "old", "x": 10, "y": 10}
    expected = {"status": "changed", "evidence": "matching_control_value_changed"}
    assert observe_action_effect(node(), node("after", "fresh"), [action]) == expected
    assert observe_action_effect(node(), node("after", "fresh"), [ComputerAction(**action)]) == expected


def test_coordinate_observation_requires_point_within_actual_control():
    for point in ({"x": 100, "y": 10}, {"x": 10}, {"x": True, "y": 10}, {"x": float("nan"), "y": 10}):
        action = {"type": "click", "target_element_ref": "old", **point}
        assert observe_action_effect(node(), node("after"), [action]) is None


def test_coordinate_tab_close_retains_unchanged_evidence_without_closure_claim():
    before = {
        "role": "AXRadioButton", "subrole": "AXTabButton", "title": "Probe",
        "children": [{"role": "AXButton", "title": "Close Tab", "element_ref": "close",
                      "bounds": {"x": 1, "y": 2, "width": 18, "height": 18}}],
    }
    action = {"type": "click", "target_element_ref": "close", "x": 10, "y": 10}
    assert observe_action_effect(before, deepcopy(before), [action]) == {
        "status": "unchanged", "evidence": "requested_tab_still_present",
    }
    assert observe_action_effect(before, {}, [action])["status"] == "unknown"


def scroll_tree(value, *, ref="page", x=900):
    return {
        "role": "AXScrollBar", "value": value,
        "bounds": {"x": x, "y": 100, "width": 19, "height": 266},
        "children": [{"role": "AXButton", "subrole": "AXDecrementPage", "element_ref": ref,
                      "bounds": {"x": x + 9, "y": 104, "width": 6, "height": 66}}],
    }


def test_scroll_page_effect_tracks_own_scrollbar_when_page_button_resizes():
    before, after = scroll_tree(1), scroll_tree(0, ref="fresh")
    after["children"][0]["bounds"]["height"] = 0
    assert observe_action_effect(before, after, [{"type": "click", "element_ref": "page"}]) == {
        "status": "changed", "evidence": "matching_scrollbar_value_changed",
    }


def test_scroll_page_does_not_attribute_other_scrollbar_movement():
    before = {"children": [scroll_tree(0), scroll_tree(0, ref="sidebar", x=150)]}
    after = {"children": [scroll_tree(0, ref="fresh"), scroll_tree(1, ref="fresh-sidebar", x=150)]}
    assert observe_action_effect(before, after, [{"type": "click", "element_ref": "page"}]) == {
        "status": "unchanged", "evidence": "matching_scrollbar_value_unchanged",
    }


def test_scroll_page_missing_ambiguous_or_nonfinite_scrollbar_is_unknown():
    before = scroll_tree(0)
    for after in ({}, {"children": [scroll_tree(1), scroll_tree(1)]}, scroll_tree(float("nan")), scroll_tree(True)):
        assert observe_action_effect(before, after, [{"type": "click", "element_ref": "page"}])["status"] == "unknown"


def test_scroll_page_accepts_bounded_native_numeric_strings_but_not_arbitrary_values():
    action = [{"type": "click", "element_ref": "page"}]
    assert observe_action_effect(scroll_tree("1.0"), scroll_tree("0.0"), action)["status"] == "changed"
    for value in ("nan", "inf", "", "SECRET", "1" * 129):
        assert observe_action_effect(scroll_tree("0.0"), scroll_tree(value), action)["status"] == "unknown"


def semantic_scroll_tree(value, *, ref="list", bar_ref="bar"):
    tree = scroll_tree(value, ref="page")
    tree["element_ref"] = bar_ref
    tree["children"][0]["subrole"] = "AXIncrementPage"
    tree["children"][0]["enabled"] = True
    tree["children"][0]["actions"] = ["AXPress"]
    return {
        "role": "AXList",
        "element_ref": ref,
        "bounds": {"x": 100, "y": 100, "width": 819, "height": 266},
        "children": [tree],
    }


def test_semantic_scroll_observes_unique_descendant_scrollbar_in_requested_direction():
    result = observe_action_effect(
        semantic_scroll_tree(0),
        semantic_scroll_tree(1, ref="fresh-list", bar_ref="fresh-bar"),
        [{"type": "scroll", "element_ref": "list", "delta_y": 400}],
    )
    assert result == {"status": "changed", "evidence": "matching_scrollbar_value_changed"}


def test_semantic_scroll_accepts_missing_enabled_but_rejects_explicitly_disabled_button():
    before, after = semantic_scroll_tree(0), semantic_scroll_tree(1, ref="fresh-list", bar_ref="fresh-bar")
    before["children"][0]["children"][0].pop("enabled")
    after["children"][0]["children"][0].pop("enabled")
    action = [{"type": "scroll", "element_ref": "list", "delta_y": 400}]

    assert observe_action_effect(before, after, action) == {
        "status": "changed", "evidence": "matching_scrollbar_value_changed",
    }

    before["children"][0]["children"][0]["enabled"] = False
    after["children"][0]["children"][0]["enabled"] = False
    assert observe_action_effect(before, after, action) == {
        "status": "unknown", "evidence": "scrollbar_control_not_found",
    }


def test_semantic_scroll_reports_when_target_subtree_has_no_scrollbar():
    before = {
        "role": "AXScrollArea", "element_ref": "web-area",
        "actions": ["AXShowMenu", "AXScrollToVisible"], "children": [],
    }
    after = deepcopy(before)

    assert observe_action_effect(
        before,
        after,
        [{"type": "scroll", "element_ref": "web-area", "delta_y": 400}],
    ) == {"status": "unknown", "evidence": "scrollbar_not_found"}


def test_semantic_scroll_wrong_direction_and_ambiguous_scrollbars_are_unknown():
    wrong = observe_action_effect(
        semantic_scroll_tree(1),
        semantic_scroll_tree(0, ref="fresh-list", bar_ref="fresh-bar"),
        [{"type": "scroll", "element_ref": "list", "delta_y": 400}],
    )
    assert wrong == {"status": "unknown", "evidence": "scrollbar_value_changed_wrong_direction"}

    before = semantic_scroll_tree(0)
    before["children"].append(scroll_tree(0, ref="other-page", x=150))
    before["children"][1]["children"][0]["subrole"] = "AXIncrementPage"
    before["children"][1]["children"][0]["enabled"] = True
    before["children"][1]["children"][0]["actions"] = ["AXPress"]
    assert observe_action_effect(
        before,
        semantic_scroll_tree(1, ref="fresh-list", bar_ref="fresh-bar"),
        [{"type": "scroll", "element_ref": "list", "delta_y": 400}],
    ) == {"status": "unknown", "evidence": "scrollbar_identity_not_unique"}


def test_closed_modal_suggests_sole_remaining_known_window_for_observation():
    result = window_transition(catalog(identities=("parent",)), "test", "modal", {"parent", "modal"})
    assert result["status"] == "previous_target_not_observed"
    assert result["observation_reason"] == "sole_remaining_bindable_window"
    assert result["next_observation"]["arguments"]["window_ref"] == "ref-parent"


def test_closed_modal_does_not_guess_among_old_windows_or_without_old_identity():
    for apps, old in [(catalog(identities=("parent", "other")), "modal"), (catalog(identities=("parent",)), None)]:
        assert "next_observation" not in window_transition(apps, "test", old, {"parent", "other", "modal"})


def test_attached_sheet_transition_uses_parent_identity_among_simultaneous_windows():
    apps = catalog(identities=("parent", "sheet", "sibling"))
    windows = apps[0]["windows"]
    windows[1]["parent_window_identity_ref"] = "parent"
    transition = window_transition(apps, "test", "parent", {"parent"})
    assert transition["observation_reason"] == "attached_sheet"
    assert transition["next_observation"]["arguments"]["window_ref"] == "ref-sheet"
    windows[2]["parent_window_identity_ref"] = "parent"
    ambiguous = window_transition(apps, "test", "parent", {"parent"})
    assert ambiguous["observation_reason"] == "ambiguous_attached_sheets"
    assert "next_observation" not in ambiguous


def test_new_sheet_of_another_document_does_not_become_the_pending_action_target():
    apps = catalog(identities=("parent", "other", "unrelated-sheet"))
    for window in apps[0]["windows"]:
        window["title"] = "Review"
    apps[0]["windows"][-1]["parent_window_identity_ref"] = "other"
    transition = window_transition(apps, "test", "parent", {"parent", "other"})
    assert transition["observation_reason"] == "sheet_belongs_to_other_window"
    assert "next_observation" not in transition
    # A disappearing sheet still leads back to its own bound parent.
    closed = window_transition(apps, "test", "closed", {"parent", "other", "closed"}, parent_identity="parent")
    assert closed["next_observation"]["arguments"]["window_ref"] == "ref-parent"
    # Legacy catalogs lack the contradicting relation and retain their suggestion.
    del apps[0]["windows"][-1]["parent_window_identity_ref"]
    legacy = window_transition(apps, "test", "parent", {"parent", "other"})
    assert legacy["next_observation"]["arguments"]["window_ref"] == "ref-unrelated-sheet"


def test_closed_sheet_recovers_its_actual_parent_among_other_documents():
    apps = catalog(identities=("parent", "sibling"))
    def closed():
        return window_transition(apps, "test", "sheet", {"parent", "sibling", "sheet"}, parent_identity="parent")
    assert closed()["next_observation"]["arguments"]["window_ref"] == "ref-parent"
    assert closed()["observation_reason"] == "sheet_parent"
    apps[0]["windows"][1]["parent_window_identity_ref"] = "parent"
    assert "next_observation" not in closed()
    apps[0]["windows"].pop(0)
    assert "next_observation" not in closed()


def test_transition_can_reobserve_exact_surviving_target_with_fresh_refs():
    result = window_transition(catalog(identities=("old", "other")), "test", "old", {"old", "other"})
    assert result["status"] == "target_still_present"
    assert result["next_observation"]["arguments"]["window_ref"] == "ref-old"
    assert result["observation_reason"] == "exact_previous_target"
