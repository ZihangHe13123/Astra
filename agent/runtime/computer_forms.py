"""Compact native control discovery and coordinate contracts; no input dispatch."""

import json
import math
from collections.abc import Mapping
from typing import Any


def observation_capabilities(tree) -> list[str]:
    if not isinstance(tree, Mapping):
        return []
    values = tree.get("observation_capabilities", [])
    return (
        [
            item
            for item in values
            if isinstance(item, str) and item in {"subtree_v1", "checked_click_v1", "auto_takeover_v1", "replace_text_v1"}
        ]
        if isinstance(values, list)
        else []
    )


def form_elements(tree, *, limit=160):
    """Expose deep controls without expanding every structural AX wrapper."""
    roles = {"AXRadioButton", "AXCheckBox", "AXTextField", "AXTextArea", "AXComboBox", "AXPopUpButton"}
    pending: list[tuple[Any, tuple[str, ...]]] = [(tree, ())]
    controls = []
    visited = 0
    size = 0
    incomplete = False
    while pending and visited < 4000:
        node, parents = pending.pop()
        if not isinstance(node, Mapping):
            continue
        visited += 1
        incomplete |= bool(node.get("children_truncated"))
        title = str(node.get("label") or node.get("title") or "")[:200]
        if node.get("role") in roles and node.get("element_ref"):
            item = {
                key: node[key]
                for key in (
                    "index",
                    "element_ref",
                    "observation_identity",
                    "role",
                    "label",
                    "title",
                    "enabled",
                    "checked",
                    "bounds",
                )
                if key in node
            }
            item["context"] = " / ".join(parents[-3:])[:300]
            # Do not expose extra text values, including secure fields.
            encoded_size = len(json.dumps(item, ensure_ascii=False).encode())
            if len(controls) >= limit or size + encoded_size > 40000:
                return {"elements": controls, "truncated": True}
            controls.append(item)
            size += encoded_size
        children = node.get("children", [])
        if isinstance(children, list) and len(parents) < 64:
            ancestry = parents + (title,) if title else parents
            pending.extend((child, ancestry) for child in reversed(children[: 4000 - visited]))
    return {"elements": controls, "truncated": bool(pending) or incomplete}


def truncated_form_region(tree):
    """One bounded native re-read when application chrome crowds out a form."""
    if "subtree_v1" not in observation_capabilities(tree):
        return None
    candidates = []
    pending = [tree]
    visited = 0
    while pending and visited < 4000:
        node = pending.pop()
        if not isinstance(node, Mapping):
            continue
        visited += 1
        if node.get("role") == "AXWebArea":
            controls = form_elements(node)
            if any(c.get("role") in {"AXRadioButton", "AXCheckBox"} for c in controls["elements"]):
                candidates.append((node.get("element_ref"), controls["truncated"]))
            continue
        pending.extend(node.get("children", []))
    return candidates[0][0] if len(candidates) == 1 and candidates[0][1] else None


def image_actions(actions, geometry):
    """Convert coordinates using the exact published image, including rounding."""
    if not geometry or any(
        not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in geometry
    ):
        raise ValueError("No current target-window image geometry; capture a fresh snapshot")
    width, height, logical_width, logical_height = geometry
    converted = []
    for source in actions:
        action = dict(source)
        for key, pixels, logical in (
            ("x", width, logical_width),
            ("end_x", width, logical_width),
            ("y", height, logical_height),
            ("end_y", height, logical_height),
        ):
            if key in action:
                value = action[key]
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < pixels:
                    raise ValueError("Pixel coordinate is outside the current published image")
                action[key] = value * logical / pixels
        converted.append(action)
    return converted


def verify_choice_goals(before, after, actions):
    goals = [(i, action) for i, action in enumerate(actions) if getattr(action, "checked", None) is not None]
    if not goals:
        return None
    old = form_elements(before)["elements"]
    new = form_elements(after)["elements"]

    def signature(node):
        if node.get("observation_identity"):
            return tuple(node.get(k, "") for k in ("observation_identity", "role", "label", "title"))
        return tuple(node.get(k, "") for k in ("role", "label", "title", "context", "bounds"))

    results = []
    for index, action in goals:
        targets = [
            n
            for n in old
            if (action.element_ref and n.get("element_ref") == action.element_ref)
            or (action.element_index and n.get("index") == action.element_index)
        ]
        matches = [n for n in new if len(targets) == 1 and signature(n) == signature(targets[0])]
        actual = matches[0].get("checked") if len(matches) == 1 else None
        results.append(
            {
                "index": index,
                "expected": action.checked,
                "actual": actual,
                "status": "verified" if actual is action.checked else "unknown" if actual is None else "not_satisfied",
            }
        )
    return {
        "status": "verified" if all(r["status"] == "verified" for r in results) else "not_verified",
        "results": results,
    }


def safe_action_receipt(value):
    """Fixed diagnostics only; never persist page content, input, paths or refs."""
    if not isinstance(value, Mapping):
        return {}
    result = {}
    enums = {
        "channel": {"native"},
        "mode": {"background", "foreground_takeover"},
        "dispatch_state": {"not_dispatched", "attempted", "acknowledged", "partial", "unknown"},
        "verification_state": {"verified", "unverified"},
        "next_step": {"continue_from_fresh_observation", "observe_result", "refresh_snapshot", "handoff", "inspect_error"},
        "replay": {"forbidden"},
    }
    for key, options in enums.items():
        if isinstance(value.get(key), str) and value[key] in options:
            result[key] = value[key]
    for key in ("action_count", "acknowledged", "verified_count", "goal_count", "goal_verified_count"):
        if type(value.get(key)) is int and 0 <= value[key] <= 64:
            result[key] = value[key]
    return result
