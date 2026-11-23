"""Interpret exact WPS native panel controls from the current helper snapshot."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .computer_forms import observation_capabilities
from .computer_policy import trusted_action_elements
from .computer_protocol import ComputerAction

BUNDLE = "com.kingsoft.wpsoffice.mac"
VERSION = "12.1.26026"
_FIND_TITLES = {"查找和替换", "查找与替换", "find and replace", "find & replace"}
_FIELDS = {"查找内容": "find", "find what": "find", "替换为": "replacement", "replace with": "replacement"}
_BUTTONS = {
    "下一处": "find_next",
    "下一个": "find_next",
    "查找下一处": "find_next",
    "find next": "find_next",
    "next": "find_next",
    "上一处": "find_previous",
    "上一个": "find_previous",
    "查找上一处": "find_previous",
    "find previous": "find_previous",
    "替换": "replace_once",
    "取代": "replace_once",
    "replace": "replace_once",
    "关闭": "dismiss_find",
    "close": "dismiss_find",
    "取消": "dismiss_find",
    "cancel": "dismiss_find",
}
_SEARCH_FINISHED = {"已完成对文档的搜索。", "已完成对文档的搜索", "finished searching the document."}


def _label(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s*[（(][a-z][）)]$", "", value.strip().rstrip(":：").casefold()).strip().rstrip(":：")


def _name(node) -> str:
    return _label(node.get("title") or node.get("label"))


def _children(node):
    children = node.get("children")
    return [child for child in children if isinstance(child, Mapping)] if isinstance(children, list) else []


def _nodes(root):
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(_children(node)))


def _field(node) -> str:
    return _FIELDS.get(_name(node), "") or _FIELDS.get(_label(node.get("value")), "")


def _row_label(label, control) -> str:
    """The WPS static label immediately precedes its popup and occupies the same row."""
    if label.get("role") != "AXStaticText":
        return ""
    a, b = label.get("bounds"), control.get("bounds")
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        return ""
    if not all(type(r.get(k)) in (int, float) for r in (a, b) for k in ("x", "y", "width", "height")):
        return ""
    if (
        min(a["width"], b["width"], a["height"], b["height"]) <= 0
        or a["x"] + a["width"] > b["x"]
        or min(a["y"] + a["height"], b["y"] + b["height"]) <= max(a["y"], b["y"])
    ):
        return ""
    return _field(label)


@dataclass(frozen=True)
class WpsFindFields:
    find: Mapping
    replacement: Mapping | None


def find_fields(root) -> WpsFindFields | None:
    candidates = {}
    for group in _nodes(root):
        siblings = _children(group)
        if group.get("role") != "AXGroup":
            continue
        for index, popup in enumerate(siblings):
            if popup.get("role") not in {"AXPopUpButton", "AXComboBox"}:
                continue
            editors = [n for n in _children(popup) if n.get("role") == "AXTextField"]
            if not editors:
                continue
            if len(editors) != 1 or editors[0].get("enabled") is not True:
                return None
            editor = editors[0]
            # WPS changes the popup title to the user's text. A value echoed
            # in a label cannot determine which operation that field controls.
            popup_label = "" if popup.get("title") == editor.get("value") else _FIELDS.get(_name(popup), "")
            preceding = _row_label(siblings[index - 1], popup) if index else ""
            labels = {popup_label, _FIELDS.get(_name(editor), ""), preceding} - {""}
            if len(labels) != 1:
                return None
            name = labels.pop()
            if name in candidates:
                return None
            candidates[name] = editor
    if "find" not in candidates:
        return None
    return WpsFindFields(candidates["find"], candidates.get("replacement"))


def has_wps_panel_capability(context: Mapping, tree) -> bool:
    return (
        context.get("metadata_trusted") is True
        and context.get("bundle_id") == BUNDLE
        and context.get("app_version") == VERSION
        and isinstance(tree, Mapping)
        and "wps_panel_ax_value_v1" in observation_capabilities(tree)
    )


def addresses_single_replace(action: ComputerAction, context: Mapping, tree) -> bool:
    """A coordinate or double-click on Replace cannot evade the one-action guard."""
    if (
        not has_wps_panel_capability(context, tree)
        or tree.get("role") != "AXWindow"
        or tree.get("subrole") != "AXDialog"
        or _name(tree) not in _FIND_TITLES
    ):
        return False
    if action.type == "keypress" and action.key not in {"return", "space"}:
        return False
    if action.type not in {"click", "double_click", "keypress"}:
        return False
    if action.element_index is not None:
        hits = [node for node in _nodes(tree) if node.get("index") == action.element_index]
    else:
        hits = trusted_action_elements(context, action.to_mapping())
    return any(n.get("role") == "AXButton" and _BUTTONS.get(_name(n)) == "replace_once" for n in hits)


def wps_panel_action_kind(actions: Sequence[ComputerAction], context: Mapping, tree) -> str:
    if len(actions) != 1 or not has_wps_panel_capability(context, tree):
        return ""
    action = actions[0]
    if action.modifiers or action.type not in {"click", "type"} or not (action.element_ref or action.element_index):
        return ""
    # Both sides resolve the current snapshot's ref/index before recognizing a command.
    matches = [
        n
        for n in _nodes(tree)
        if (
            (action.element_ref and n.get("element_ref") == action.element_ref)
            or (action.element_index and n.get("index") == action.element_index)
        )
    ]
    if len(matches) != 1 or matches[0].get("enabled") is not True:
        return ""
    node = matches[0]
    panel = tree.get("standard_save_panel")
    if action.type == "type":
        if action.replace is not True or node.get("role") != "AXTextField":
            return ""
        if (
            isinstance(panel, Mapping)
            and node.get("element_ref")
            and node.get("element_ref") == panel.get("filename_element_ref")
        ):
            return "field_write"
    elif "AXPress" not in (node.get("actions") or ()):
        return ""
    if node.get("role") == "AXMenuItem" and _name(node) in {"保存", "save", "儲存", "储存"}:
        file_menus = [
            n
            for n in _nodes(tree)
            if n.get("role") in {"AXMenuBarItem", "AXMenu"} and _name(n) in {"file", "文件", "档案", "檔案"}
        ]
        if any(any(x is node for x in _nodes(file)) for file in file_menus):
            return "save_document"
    if (
        isinstance(panel, Mapping)
        and node.get("role") == "AXButton"
        and node.get("element_ref") == panel.get("commit_element_ref")
    ):
        return "save_copy" if node.get("element_ref") else ""
    if tree.get("role") != "AXWindow" or tree.get("subrole") != "AXDialog":
        return ""
    if _name(tree) in _FIND_TITLES:
        if action.type == "type":
            fields = find_fields(tree)
            if fields and (node is fields.find or node is fields.replacement):
                return "field_write"
        if node.get("role") == "AXButton":
            return _BUTTONS.get(_name(node), "")
    if (
        node.get("role") == "AXButton"
        and _name(node) in {"确定", "好", "ok"}
        and any(
            n.get("role") == "AXStaticText" and _label(n.get("value") or n.get("title")) in _SEARCH_FINISHED
            for n in _nodes(tree)
        )
    ):
        return "dismiss_search_notice"
    return ""
