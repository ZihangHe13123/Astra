"""Bounded, advisory post-action evidence. Never creates input authority."""

import math
from collections.abc import Mapping


class _IncompleteTree(Exception):
    pass


def _nodes(tree):
    pending: list[tuple[object, tuple[Mapping, ...]]] = [(tree, ())]
    result = []
    while pending and len(result) < 4000:
        node, ancestors = pending.pop()
        if not isinstance(node, Mapping):
            continue
        result.append((node, ancestors))
        children = node.get("children", [])
        if children and len(ancestors) >= 32:
            raise _IncompleteTree
        if isinstance(children, list) and len(ancestors) < 32:
            remaining = max(0, 4000 - len(result) - len(pending))
            if len(children) > remaining:
                raise _IncompleteTree
            pending.extend((child, ancestors + (node,)) for child in children[:remaining])
    return result


def _signature(node):
    bounds = node.get("bounds")
    if not isinstance(bounds, Mapping):
        return None
    coords = tuple(bounds.get(key) for key in ("x", "y", "width", "height"))
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in coords):
        return None
    return tuple(node.get(key) for key in ("role", "subrole", "title", "label")) + coords


def _finite_scalar(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str) and len(value) > 128:
        return None
    try:
        scalar = float(value)
    except (ValueError, OverflowError):
        return None
    return scalar if math.isfinite(scalar) else None


def observe_action_effect(before, after, actions):
    """Report only what matched AX data shows, not whether the user's task succeeded."""
    if len(actions) != 1:
        return None
    action = actions[0]
    def field(key):
        return action.get(key) if isinstance(action, Mapping) else getattr(action, key, None)

    kind = field("type")
    coordinate_target = field("target_element_ref")
    ref = field("element_ref") or coordinate_target
    if kind not in {"click", "type", "keypress", "scroll"} or not ref:
        return None
    try:
        old_nodes, new_nodes = _nodes(before), _nodes(after)
    except _IncompleteTree:
        return {"status": "unknown", "evidence": "incomplete_ax_observation"}
    targets = [(n, parents) for n, parents in old_nodes if n.get("element_ref") == ref]
    if len(targets) != 1:
        return None
    target, parents = targets[0]
    if coordinate_target:
        signature = _signature(target)
        x, y = field("x"), field("y")
        if signature is None or not all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (x, y)
        ):
            return None
        left, top, width, height = signature[-4:]
        if not (left < x < left + width and top < y < top + height):
            return None
    if kind == "scroll":
        delta_x, delta_y = field("delta_x"), field("delta_y")
        if (
            isinstance(delta_y, bool)
            or not isinstance(delta_y, (int, float))
            or not math.isfinite(delta_y)
            or delta_y == 0
            or delta_x not in (None, 0)
        ):
            return None
        direction = "Increment" if delta_y > 0 else "Decrement"

        try:
            subtree_nodes = _nodes(target)
        except _IncompleteTree:
            return {"status": "unknown", "evidence": "incomplete_ax_observation"}
        scrollbars = [node for node, _ in subtree_nodes if node.get("role") == "AXScrollBar"]
        if not scrollbars:
            return {"status": "unknown", "evidence": "scrollbar_not_found"}

        def candidates(suffix):
            return [
                (scrollbar, child)
                for scrollbar in scrollbars
                for child in (
                    scrollbar.get("children", [])
                    if isinstance(scrollbar.get("children"), list)
                    else []
                )
                if isinstance(child, Mapping)
                and child.get("role") == "AXButton"
                and child.get("subrole") == f"AX{direction}{suffix}"
                and child.get("enabled") is not False
                and isinstance(child.get("actions"), list)
                and "AXPress" in child["actions"]
            ]

        selected = candidates("Page")
        if not selected:
            selected = candidates("Arrow")
        if not selected:
            return {"status": "unknown", "evidence": "scrollbar_control_not_found"}
        if len(selected) > 1:
            return {"status": "unknown", "evidence": "scrollbar_identity_not_unique"}
        signature = _signature(selected[0][0])
        old_matches = [node for node, _ in old_nodes if signature is not None and _signature(node) == signature]
        new_matches = [node for node, _ in new_nodes if signature is not None and _signature(node) == signature]
        if len(old_matches) != 1 or len(new_matches) != 1:
            return {"status": "unknown", "evidence": "scrollbar_identity_not_unique"}
        left = _finite_scalar(old_matches[0].get("value"))
        right = _finite_scalar(new_matches[0].get("value"))
        if left is None or right is None:
            return {"status": "unknown", "evidence": "scrollbar_value_unavailable"}
        if left == right:
            return {"status": "unchanged", "evidence": "matching_scrollbar_value_unchanged"}
        if (delta_y > 0 and right > left) or (delta_y < 0 and right < left):
            return {"status": "changed", "evidence": "matching_scrollbar_value_changed"}
        return {"status": "unknown", "evidence": "scrollbar_value_changed_wrong_direction"}
    if kind == "click" and target.get("role") == "AXButton" and target.get("subrole") in {
        "AXIncrementPage", "AXDecrementPage", "AXIncrementArrow", "AXDecrementArrow",
    }:
        scrollbar = next((n for n in reversed(parents) if n.get("role") == "AXScrollBar"), None)
        signature = _signature(scrollbar) if scrollbar is not None else None
        old_matches = [n for n, _ in old_nodes if signature is not None and _signature(n) == signature]
        matches = [n for n, _ in new_nodes if signature is not None and _signature(n) == signature]
        if len(old_matches) != 1 or len(matches) != 1:
            return {"status": "unknown", "evidence": "scrollbar_identity_not_unique"}
        left, right = _finite_scalar(old_matches[0].get("value")), _finite_scalar(matches[0].get("value"))
        if left is None or right is None:
            return {"status": "unknown", "evidence": "scrollbar_value_unavailable"}
        changed = left != right
        return {
            "status": "changed" if changed else "unchanged",
            "evidence": "matching_scrollbar_value_changed" if changed else "matching_scrollbar_value_unchanged",
        }
    tab = next((n for n in reversed(parents) if n.get("subrole") == "AXTabButton"), None)
    if kind == "click" and tab is not None and target.get("title") in {"关闭标签页", "Close Tab", "Close tab"}:
        title = tab.get("title") or tab.get("label")
        if isinstance(title, str) and title:
            prior_matches = [
                n
                for n, _ in old_nodes
                if n.get("subrole") == "AXTabButton" and (n.get("title") or n.get("label")) == title
            ]
            if len(prior_matches) != 1:
                return {"status": "unknown", "evidence": "tab_identity_not_unique"}
            matches = [
                n
                for n, _ in new_nodes
                if n.get("subrole") == "AXTabButton" and (n.get("title") or n.get("label")) == title
            ]
            if len(matches) == 1:
                return {"status": "unchanged", "evidence": "requested_tab_still_present"}
            # Compact AX coverage is not guaranteed complete. Absence isn't closure proof.
            return {"status": "unknown", "evidence": "requested_tab_not_in_observed_tree"}
    signature = _signature(target)
    matches = [n for n, _ in new_nodes if signature is not None and _signature(n) == signature]
    if len(matches) != 1 or sum(_signature(n) == signature for n, _ in old_nodes) != 1:
        return {"status": "unknown", "evidence": "control_identity_not_unique"}
    left, right = target.get("value"), matches[0].get("value")
    if left is None or right is None or type(left) is not type(right) or not isinstance(left, (str, int, float, bool)):
        return {"status": "unknown", "evidence": "control_value_unavailable"}
    if isinstance(left, str) and (len(left) > 128 or len(right) > 128):
        return {"status": "unknown", "evidence": "control_value_unavailable"}
    changed = left != right
    return {
        "status": "changed" if changed else "unchanged",
        "evidence": "matching_control_value_changed" if changed else "matching_control_value_unchanged",
    }


def _catalog_windows_may_overlap(first, second):
    def bounds(window) -> tuple[float, float, float, float] | None:
        value = window.get("bounds")
        if not isinstance(value, Mapping):
            return None
        coordinates: list[float] = []
        for key in ("x", "y", "width", "height"):
            component = value.get(key)
            if not isinstance(component, (int, float)) or isinstance(component, bool):
                return None
            try:
                number = float(component)
            except OverflowError:
                return None
            if not math.isfinite(number):
                return None
            coordinates.append(number)
        x, y, width, height = coordinates
        return (x, y, width, height) if width > 0 and height > 0 else None

    left, right = bounds(first), bounds(second)
    if left is None or right is None:
        return True
    return (
        left[0] < right[0] + right[2] and right[0] < left[0] + left[2]
        and left[1] < right[1] + right[3] and right[1] < left[1] + left[3]
    )


def window_transition(apps, bundle_id, old_identity, prior_identities, *, parent_identity=None):
    """Return catalog evidence and an observation suggestion, never a selected target."""
    matches = [a for a in apps[:100] if isinstance(a, Mapping) and bundle_id and a.get("bundle_id") == bundle_id]
    if len(matches) != 1:
        return {"status": "application_not_observed" if not matches else "ambiguous_application", "windows": []}
    app = matches[0]
    windows = app.get("windows", [])
    windows = [w for w in windows[:100] if isinstance(w, Mapping)] if isinstance(windows, list) else []
    present = any(old_identity and w.get("window_identity_ref") == old_identity for w in windows)
    new = [
        w
        for w in windows
        if w.get("bindable") is True
        and w.get("window_identity_ref")
        and w["window_identity_ref"] not in prior_identities
    ]
    result = {
        "status": "previous_target_not_observed" if old_identity and not present else "target_still_present",
        "app_ref": app.get("app_ref"),
        "windows": windows[:20],
    }
    if old_identity and present and new:
        result["status"] = "new_window_observed"
    # A new unlabelled popup remains a possible observation on older helpers.
    # Explicit attachment to a different document rules that suggestion out.
    candidate = new[0] if len(new) == 1 and not new[0].get("parent_window_identity_ref") else None
    if new and all(w.get("parent_window_identity_ref") and w["parent_window_identity_ref"] != old_identity for w in new):
        result["observation_reason"] = "sheet_belongs_to_other_window"
    if present:
        attached = [w for w in new if w.get("parent_window_identity_ref") == old_identity]
        if len(attached) == 1:
            candidate = attached[0]
            result["observation_reason"] = "attached_sheet"
        elif len(attached) > 1:
            result["observation_reason"] = "ambiguous_attached_sheets"
            return result
    if not present and parent_identity:
        parents = [w for w in windows if w.get("bindable") is True and w.get("window_identity_ref") == parent_identity]
        if len(parents) != 1:
            result["observation_reason"] = "sheet_parent_unavailable"
            return result
        visible_sheets = [w for w in windows if w.get("parent_window_identity_ref") == parent_identity]
        if visible_sheets:
            result["observation_reason"] = "sheet_parent_still_covered"
            return result
        candidate = parents[0]
        result["observation_reason"] = "sheet_parent"
    if old_identity and not present and not new and not parent_identity:
        remaining = [w for w in windows if w.get("bindable") is True and w.get("window_identity_ref")]
        if len(remaining) == 1:
            candidate = remaining[0]
            result["observation_reason"] = "sole_remaining_bindable_window"
    if old_identity and present and not new:
        previous = [w for w in windows if w.get("bindable") is True and w.get("window_identity_ref") == old_identity]
        if len(previous) == 1:
            candidate = previous[0]
            result["observation_reason"] = "exact_previous_target"
    if old_identity and candidate and app.get("app_ref") and candidate.get("window_ref"):
        blockers = [
            w for w in windows
            if w.get("bindable") is False and _catalog_windows_may_overlap(candidate, w)
        ]
        if blockers:
            result["status"] = "unmatched_window_observed"
            result["observation_reason"] = "unmatched_window_blocks_target"
            result["unmatched_window_refs"] = [w["window_ref"] for w in blockers[:20] if w.get("window_ref")]
            return result
        result["next_observation"] = {
            "tool": "computer_get_app_state",
            "arguments": {"app_ref": app["app_ref"], "window_ref": candidate["window_ref"]},
        }
    return result
