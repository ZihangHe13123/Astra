"""Private-bar scene actions.

These tools deliberately update local presentation state only. They are hidden
from the normal work catalog and exposed through BarModeController's allowlist.
"""

from __future__ import annotations

import json

from ..bar_mode import BAR_TURN_REPLY_MAX_CHARS, BarModeController
from .registry import ToolDef, ToolRegistry

_BAR_TURN_CALLS_PER_TURN = 3
_ONE_CALL_PER_TURN = (
    "At most one call per turn; a call that ran and failed counts, one rejected for its arguments does not. "
    "A second call, in the same step or a later one, is not run, and nothing else in that step runs either."
)


def register_bar_tools(registry: ToolRegistry, controller: BarModeController) -> None:
    # Coroutine functions on purpose. The registry runs a plain function in a
    # worker thread, and these tools are read-risk, so several scene calls in
    # one step would overlap: they share one scene state and one state file.
    # On the event loop each call applies, saves and reports before the next.
    async def bar_turn(reply: str, actions: list[dict] | None = None) -> str:
        return controller.commit_turn(reply, actions)

    async def serve_drink(
        name: str = "",
        note: str = "",
        tone: str = "clear",
        temperature: str = "cold",
    ) -> str:
        drink = controller.serve_drink(name, note, tone, temperature)
        return json.dumps(drink.to_event(), ensure_ascii=False)

    async def refill_drink() -> str:
        drink = controller.refill_drink()
        return json.dumps(drink.to_event(), ensure_ascii=False)

    async def rename_drink(name: str) -> str:
        drink = controller.rename_drink(name)
        return json.dumps(drink.to_event(), ensure_ascii=False)

    async def set_ambiance(
        weather: str | None = None,
        power: str | None = None,
        music: str | None = None,
        radio: str | None = None,
    ) -> str:
        ambiance = controller.set_ambiance(weather, power, music, radio)
        return json.dumps(ambiance.to_event(), ensure_ascii=False)

    async def pour_lyra_drink(name: str, note: str = "") -> str:
        glass = controller.pour_lyra_drink(name, note)
        return json.dumps(glass.to_event(), ensure_ascii=False)

    async def sip_lyra_drink() -> str:
        glass = controller.sip_lyra_drink()
        return json.dumps(glass.to_event(), ensure_ascii=False)

    registry.register(ToolDef(
        name="bar_turn",
        description=(
            "Commit one complete AGENT BAR response atomically. This is the only model-facing bar tool. "
            "Put every user-visible word in reply and every scene state change in actions. "
            "Use an empty actions array for conversation without a scene change. Never narrate a state "
            "change without its matching action. A successful call is the whole turn: reply is shown as "
            "written and nothing follows it. A failed call applies no action and shows no reply; correct "
            f"it and call again. At most {_BAR_TURN_CALLS_PER_TURN} calls per turn: when the last of them "
            "fails too, the turn ends."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reply": {
                    "type": "string",
                    "description": (
                        "The complete reply shown to the user after all actions commit. At most "
                        f"{BAR_TURN_REPLY_MAX_CHARS} characters; a longer reply fails the call."
                    ),
                },
                "actions": {
                    "type": "array",
                    "maxItems": 4,
                    "description": (
                        "Scene changes committed with reply, applied in order; at most 4. Allowed types: "
                        "serve_drink, refill_drink, rename_drink, set_ambiance, pour_lyra_drink, sip_lyra_drink. "
                        "serve_drink needs name, note, tone and temperature; rename_drink and pour_lyra_drink "
                        "need name (pour_lyra_drink may add note); set_ambiance needs at least one of weather, "
                        "power, music, radio; refill_drink and sip_lyra_drink need only type. "
                        "If one action fails, none is applied."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "serve_drink", "refill_drink", "rename_drink",
                                    "set_ambiance", "pour_lyra_drink", "sip_lyra_drink",
                                ],
                                "description": (
                                    "refill_drink and rename_drink fail without a current drink, sip_lyra_drink "
                                    "when Lyra has no drink or her glass is empty. A drink served or poured by "
                                    "an earlier action in the same call counts."
                                ),
                            },
                            "name": {
                                "type": "string",
                                "description": (
                                    "Exact drink name used in reply. For serve_drink only, use an empty "
                                    "string when intentionally unnamed; the runtime then assigns a temporary "
                                    "name. Longer than 48 characters is cut."
                                ),
                            },
                            "note": {
                                "type": "string",
                                "description": (
                                    "Compact ingredient plus sensory shelf note. Must not be empty for "
                                    "serve_drink. Cut at 120 characters (80 for pour_lyra_drink)."
                                ),
                            },
                            "tone": {
                                "type": "string",
                                "enum": ["amber", "cyan", "pink", "clear"],
                                "description": "Pixel-card accent color matching the drink.",
                            },
                            "temperature": {"type": "string", "enum": ["hot", "cold", "room"]},
                            "weather": {"type": "string", "enum": ["drizzle", "rain", "downpour", "clearing"]},
                            "power": {"type": "string", "enum": ["stable", "flicker", "brownout"]},
                            "music": {"type": "string", "enum": ["silent", "low_synth", "old_radio", "jukebox"]},
                            "radio": {"type": "string", "enum": ["static", "local_news", "weather", "emergency"]},
                        },
                        "required": ["type"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["reply", "actions"],
            "additionalProperties": False,
        },
        fn=bar_turn,
        risk="read",
        approval="never",
        idempotent=False,
        max_calls_per_turn=_BAR_TURN_CALLS_PER_TURN,
        repeat_guard=True,
        group="bar",
        expose_by_default=False,
        return_direct=True,
    ))

    registry.register(ToolDef(
        name="serve_drink",
        description=(
            "Place a newly prepared drink on the private bar and update its visual card. "
            "Use whenever Lyra actually serves or replaces a drink. If the drink has no final name yet, "
            "omit name and the runtime will assign a temporary name; never delay serving while waiting for a name. "
            "The user controls sipping manually. "
            + _ONE_CALL_PER_TURN
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Optional short drink name. Omit it when the drink is intentionally unnamed. "
                        "Longer than 48 characters is cut."
                    ),
                },
                "note": {
                    "type": "string",
                    "description": (
                        "One compact shelf-label note, ideally 20-36 Chinese characters (or similarly brief). "
                        "Use ingredients plus one sensory impression; never put narration or a full recipe here. "
                        "Cut at 120 characters."
                    ),
                },
                "tone": {
                    "type": "string",
                    "enum": ["amber", "cyan", "pink", "clear"],
                    "description": "Pixel-card accent color matching the drink.",
                },
                "temperature": {
                    "type": "string",
                    "enum": ["hot", "cold", "room"],
                },
            },
            "required": ["note", "tone", "temperature"],
            "additionalProperties": False,
        },
        fn=serve_drink,
        risk="read",
        approval="never",
        idempotent=False,
        max_calls_per_turn=1,
        repeat_guard=True,
        group="bar",
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="refill_drink",
        description=(
            "Refill the current private-bar drink to a full glass without changing its name, recipe, "
            "temperature, or visual tone. Use whenever Lyra agrees to top up or pour another glass of "
            "the same drink. Do not claim the refill happened unless this action succeeds. "
            "Fails when there is no current drink. "
            + _ONE_CALL_PER_TURN
        ),
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        fn=refill_drink,
        risk="read",
        approval="never",
        idempotent=False,
        max_calls_per_turn=1,
        repeat_guard=True,
        group="bar",
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="rename_drink",
        description=(
            "Rename the current private-bar drink without pouring a new drink or changing its recipe, "
            "temperature, tone, or fill. Use after the user or Lyra settles on a name. "
            "Do not claim the label was updated unless this action succeeds. "
            "Fails when there is no current drink. "
            + _ONE_CALL_PER_TURN
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "The final non-empty name for the current drink. Longer than 48 characters is cut."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        fn=rename_drink,
        risk="read",
        approval="never",
        idempotent=False,
        max_calls_per_turn=1,
        repeat_guard=True,
        group="bar",
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="set_ambiance",
        description=(
            "Change one or more persistent AGENT BAR atmosphere signals for a meaningful scene beat. "
            "Do not call every turn or randomly churn stable ambiance. The change becomes a one-time scene event. "
            "Put every signal that changes into the same call. "
            + _ONE_CALL_PER_TURN
        ),
        parameters={
            "type": "object",
            "properties": {
                "weather": {"type": "string", "enum": ["drizzle", "rain", "downpour", "clearing"]},
                "power": {"type": "string", "enum": ["stable", "flicker", "brownout"]},
                "music": {"type": "string", "enum": ["silent", "low_synth", "old_radio", "jukebox"]},
                "radio": {"type": "string", "enum": ["static", "local_news", "weather", "emergency"]},
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
        fn=set_ambiance,
        risk="read",
        approval="never",
        idempotent=False,
        max_calls_per_turn=1,
        repeat_guard=True,
        group="bar",
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="pour_lyra_drink",
        description=(
            "Pour or replace Lyra's own small drink when she chooses to accompany the guest. "
            "This never changes the user's glass. Use it in the same turn as any matching narration. "
            + _ONE_CALL_PER_TURN
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Short name for Lyra's drink. Longer than 48 characters is cut.",
                },
                "note": {
                    "type": "string",
                    "description": "Optional compact ingredient or sensory note. Cut at 80 characters.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        fn=pour_lyra_drink,
        risk="read",
        approval="never",
        idempotent=False,
        max_calls_per_turn=1,
        repeat_guard=True,
        group="bar",
        expose_by_default=False,
    ))
    registry.register(ToolDef(
        name="sip_lyra_drink",
        description=(
            "Take one sip from Lyra's own glass. This never changes the user's glass. "
            "Use it in the same turn as any matching narration. "
            "Fails when Lyra has no drink or her glass is empty. "
            + _ONE_CALL_PER_TURN
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        fn=sip_lyra_drink,
        risk="read",
        approval="never",
        idempotent=False,
        max_calls_per_turn=1,
        repeat_guard=True,
        group="bar",
        expose_by_default=False,
    ))
