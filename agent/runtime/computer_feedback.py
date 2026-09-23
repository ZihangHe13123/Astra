"""Bounded execution facts and observation guidance, independent of model/provider."""

import re
from collections.abc import Mapping

from .computer_forms import safe_action_receipt


NEXT_STEPS = {
    "continue_from_fresh_observation": "Requested choice states are verified. Use the returned fresh refs for remaining work, or report completion briefly. No extra read is needed for these choices; server saving is a separate result.",
    "observe_result": "Inspect the returned fresh observation first. If it proves the requested result, report it briefly. Otherwise call computer_snapshot(settle_ms=300). An acknowledgement is not a failed click: do not click the same button again because the observation looked unchanged. After two inconclusive reads, report unconfirmed.",
    "follow_recovery": "The acknowledged actions were delivered and none after them were sent, but no fresh observation was returned, so the effect is unverified. Do not replay. Follow this result's Recovery field; if that cannot be done safely, hand this step to the user.",
    "refresh_snapshot": "This call did not dispatch input. Capture a fresh snapshot and decide whether the action is still needed; this does not authorize replaying an earlier uncertain action.",
    "handoff": "Input outcome is uncertain. Observe if possible, then report what remains unconfirmed. Do not replay the action.",
    "inspect_error": "This call did not dispatch input. Follow its error code and recovery hint; do not guess another API or target.",
}


def build_action_receipt(payload, *, mode, action_count, dispatch_attempted, error_code=""):
    payload = payload if isinstance(payload, Mapping) else {}
    native = payload.get("action_result")
    native = native if isinstance(native, Mapping) else {}
    outcomes = native.get("outcomes")
    outcomes = outcomes if isinstance(outcomes, list) else []
    # Count only the unique acknowledged prefix, not arbitrary success markers.
    acknowledgement = native.get("last_acknowledged_action")
    acknowledged = 0
    if type(acknowledgement) is int and -1 <= acknowledgement < action_count:
        for i, outcome in enumerate(outcomes[:acknowledgement + 1]):
            if not isinstance(outcome, Mapping) or type(outcome.get("index")) is not int or outcome["index"] != i or outcome.get("ok") is not True:
                break
            acknowledged += 1
    choice = payload.get("choice_verification")
    choice = choice if isinstance(choice, Mapping) else {}
    goals = choice.get("results")
    goals = goals if isinstance(goals, list) else []
    goal_verified = sum(isinstance(g, Mapping) and g.get("status") == "verified" for g in goals)
    verified = sum(isinstance(o, Mapping) and o.get("effect_verification") == "verified" for o in outcomes[:acknowledged])
    all_verified = (
        not error_code and action_count > 0 and acknowledged == action_count and len(goals) == action_count
        and goal_verified == action_count and choice.get("status") == "verified"
    )
    dispatch = ("acknowledged" if acknowledged == action_count and action_count else
                "partial" if acknowledged else "unknown" if dispatch_attempted else "not_dispatched")
    # No fresh observation came back and its recovery may already have revoked the
    # target, so the result's own Recovery decides how to observe next.
    observation_pending = error_code == "post_action_observation_pending" and acknowledged > 0
    next_step = ("continue_from_fresh_observation" if all_verified else
                 "follow_recovery" if observation_pending else
                 "observe_result" if dispatch == "acknowledged" or error_code == "effect_pending" else
                 "handoff" if dispatch in {"partial", "unknown"} else
                 "refresh_snapshot" if error_code == "stale_snapshot" else "inspect_error")
    return safe_action_receipt({
        "channel": "native", "mode": mode, "action_count": action_count,
        "acknowledged": acknowledged, "verified_count": verified,
        "goal_count": len(goals), "goal_verified_count": goal_verified,
        "dispatch_state": dispatch, "verification_state": "verified" if all_verified else "unverified",
        "next_step": next_step, "replay": "forbidden",
    })


def receipt_instruction(receipt):
    return NEXT_STEPS.get(safe_action_receipt(receipt).get("next_step", ""), "")


def _field(action, name):
    return action.get(name) if isinstance(action, Mapping) else getattr(action, name, None)


def _target(tree, action):
    ref = _field(action, "element_ref") or _field(action, "target_element_ref")
    index = _field(action, "element_index")
    pending, matches, visited = [tree], [], 0
    while pending and visited < 4000:
        node = pending.pop()
        visited += 1
        if not isinstance(node, Mapping):
            continue
        if (ref and node.get("element_ref") == ref) or (index is not None and node.get("index") == index):
            matches.append(node)
        children = node.get("children")
        if isinstance(children, list):
            pending.extend(children[:max(0, 4000 - visited - len(pending))])
    return matches[0] if len(matches) == 1 else None


_COMMIT_BUTTON = re.compile(r"\b(submit|send|save|delete|upload|export|publish|confirm)\b|提交|发送|保存|删除|上传|导出|发布|确认", re.I)


class PendingClickGuard:
    """Read correlation can reject a duplicate, never authorize an input target.

    Keep only the last acknowledged commit button in memory. A different action
    starts another step (e.g. editing then saving again, or a new confirmation
    dialog). Pure observation or ref churn does not make a pending submit safe.
    """

    def __init__(self):
        self.pending = None

    @staticmethod
    def binding_key(binding):
        # A same-window read may renew its authority generation. That does not
        # make another submit meaningful; this key grants no input authority.
        if all(hasattr(binding, key) for key in ("session_id", "app_ref", "window_ref")):
            return (binding.session_id, binding.app_ref, binding.window_ref)
        return binding

    @staticmethod
    def identity(tree, action):
        if _field(action, "type") != "click" or _field(action, "checked") is not None:
            return None
        node = _target(tree, action)
        if not node or node.get("role") != "AXButton":
            return None
        identity = node.get("observation_identity")
        label = str(node.get("label") or node.get("title") or "")
        if not isinstance(identity, str) or not identity or not _COMMIT_BUTTON.search(label):
            return None
        return (identity, node.get("role"), node.get("label"), node.get("title"))

    def blocks(self, binding, tree, actions):
        return self.pending is not None and any(
            (self.binding_key(binding), self.identity(tree, action)) == self.pending for action in actions
        )

    def remember(self, binding, tree, actions, result):
        if not isinstance(result, Mapping):
            return
        outcomes = result.get("outcomes")
        if not isinstance(outcomes, list):
            return
        for i, outcome in enumerate(outcomes):
            if i >= len(actions) or not isinstance(outcome, Mapping) or outcome.get("index") != i or outcome.get("ok") is not True:
                break
            if _field(actions[i], "type") == "wait":
                continue
            checked = _field(actions[i], "checked")
            if checked is not None:
                target = _target(tree, actions[i])
                if target is not None and target.get("checked") is checked:
                    continue
            identity = self.identity(tree, actions[i])
            self.pending = (self.binding_key(binding), identity) if identity and outcome.get("effect_verification") != "verified" else None
