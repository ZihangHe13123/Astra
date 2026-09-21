"""Isolated night-bar mode with its own persisted session namespace."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .context import AgentContext
from .persona_overrides import local_mode_prompt
from .context_compressor import ContextCompressor
from .session_store import SessionStore
from .token_estimator import estimate_value_tokens

if TYPE_CHECKING:
    from .react import ReActAgent


BAR_ATOMIC_OUTPUT_RULE = """- 当前为 ATOMIC 输出模式：每轮只通过 `bar_turn` 提交一次原子回合；把所有给用户看的文字放在 `reply`，把本轮真正发生的场景变化放在 `actions`，没有动作时提交空数组。不要在工具之外输出另一份回复。实际递出或替换饮料时加入 `serve_drink` 动作；`note` 只写约 20–36 个汉字的“主要原料 + 一个感官印象”，`name` 填本轮文字中使用的确切名称，只有故意暂不命名时才填空字符串。续杯用 `refill_drink`，只改名用 `rename_drink`；喝一口仍由用户手动触发，不得添加用户喝酒动作。回复与全部动作只有在同一个 `bar_turn` 成功后才会显示。reply 字段按正常对话的丰满度书写，不要因为它是工具参数就压缩篇幅。"""

BAR_STREAM_OUTPUT_RULE = """- 当前为 STREAM REACT 输出模式：不要调用 `bar_turn`。没有真实场景变化时直接正常回复，文字会边生成边显示。需要改变场景时，第一步只提交对应的独立工具调用，不要同时输出给用户看的正文；等待工具结果后，下一步再根据已经提交的最新状态正常回复。递出或替换饮料用 `serve_drink`，续杯用 `refill_drink`，只改名用 `rename_drink`，改变环境用 `set_ambiance`，Lyra 自己倒酒或喝酒用对应的 Lyra 酒杯工具。绝对不要把 `serve_drink(...)` 等调用语法写进普通正文冒充工具调用。`serve_drink.note` 只写约 20–36 个汉字的“主要原料 + 一个感官印象”；最终回复中的名称须与工具结果一致。喝一口仍由用户手动触发。工具成功后不要再次调用同一动作；此模式采用标准的“工具调用 → 执行 → 最终回复”流程。"""

BAR_MODE_PROMPT = f"""You are Lyra, the host of AGENT BAR, a fictional late-night cafe
and bar in Glitch City. Be warm, observant and conversational, with a little dry
humor. This is a relaxed creative scene separate from the ordinary workspace.

Setting:
- Use an original near-future city: rain, old terminals, night-shift workers,
  local broadcasts and small neighborhood stories. Keep the setting accessible
  without assuming knowledge of another fictional universe.
- Describe a few concrete details rather than explaining the whole world. Reuse
  people and events introduced in this shift; distinguish rumors from facts.
- Do not assume a prior relationship with the user, invent a shared personal
  history, or assign the user an identity or feelings they have not supplied.
  Let the user choose how to participate in the fictional scene.

Conversation and drinks:
- Offer a drink, coffee, tea, soda or fictional mix when it fits the conversation.
  Follow the user's preference. Do not narrate the user drinking or taking actions
  they have not chosen. Do not pressure the user to drink alcohol.
- Respond naturally to conversation, questions, jokes or quiet moments. Match
  the user's language and desired detail; avoid repeated questions, repetitive
  gestures and forcing every topic into a plot.
{BAR_ATOMIC_OUTPUT_RULE}
- Keep established drink names consistent. Do not repeat a successful action or
  make a fresh drink every turn without a scene reason.
- Ambiance and glass actions change only this fictional scene's visual state.
  If Lyra describes pouring or sipping her own drink, include the corresponding
  scene action in the same turn. Let lighting, weather and music change only when
  useful to the scene, not automatically on every reply.

Isolation:
- Use only this shift's conversation. Do not use work sessions, persistent memory,
  learned skills or private history; do not pretend to remember unseen events.
- Fictional events are not real shared experiences. Keep conversation suitable
  for a general audience, and respect the user's stated boundaries.
- The available tools only update the local scene. They do not carry out real
  tasks. To inspect files, change code or resume ordinary work, remind the user
  they can leave with /bar leave.
"""


BAR_DRINK_TONES = {"amber", "cyan", "pink", "clear"}
BAR_DRINK_TEMPERATURES = {"hot", "cold", "room"}
BAR_WEATHERS = {"drizzle", "rain", "downpour", "clearing"}
BAR_POWER_STATES = {"stable", "flicker", "brownout"}
BAR_MUSIC_STATES = {"silent", "low_synth", "old_radio", "jukebox"}
BAR_RADIO_STATES = {"static", "local_news", "weather", "emergency"}
BAR_TURN_TOOL_NAME = "bar_turn"
BAR_OUTPUT_MODES = frozenset({"atomic", "stream"})
BAR_SCENE_TOOL_NAMES = frozenset({
    BAR_TURN_TOOL_NAME,
    "serve_drink",
    "refill_drink",
    "rename_drink",
    "set_ambiance",
    "pour_lyra_drink",
    "sip_lyra_drink",
})
BAR_ATOMIC_TOOL_NAMES = frozenset({BAR_TURN_TOOL_NAME})
BAR_STREAM_TOOL_NAMES = BAR_SCENE_TOOL_NAMES - BAR_ATOMIC_TOOL_NAMES
BAR_GENERATION_OVERRIDES = {
    "temperature": 0.65,
    "top_p": 0.9,
    "max_tokens": 8192,
    "repetition_penalty": 1.05,
}
# Backward-compatible name for the default contract.
BAR_MODEL_TOOL_NAMES = BAR_ATOMIC_TOOL_NAMES


def bar_mode_prompt(output_mode: str) -> str:
    if output_mode == "atomic":
        return local_mode_prompt("bar_atomic", BAR_MODE_PROMPT)
    if output_mode == "stream":
        return local_mode_prompt("bar_stream", BAR_MODE_PROMPT.replace(BAR_ATOMIC_OUTPUT_RULE, BAR_STREAM_OUTPUT_RULE))
    raise ValueError(f"Unknown bar output mode: {output_mode}")


@dataclass
class BarDrink:
    name: str = ""
    note: str = ""
    tone: str = "clear"
    temperature: str = "cold"
    fill: int = 0
    revision: int = 0
    acknowledged_revision: int = 0
    last_action: str = "none"
    last_actor: str = ""
    previous_fill: int = 0
    unnamed_counter: int = 0

    @property
    def active(self) -> bool:
        return bool(self.name)

    def to_event(self) -> dict:
        return {
            "name": self.name,
            "note": self.note,
            "tone": self.tone,
            "temperature": self.temperature,
            "fill": self.fill,
            "active": self.active,
        }


@dataclass
class BarAmbiance:
    weather: str = "rain"
    power: str = "stable"
    music: str = "low_synth"
    radio: str = "static"
    revision: int = 0
    acknowledged_revision: int = 0
    last_change: str = "none"

    def to_event(self) -> dict:
        return {
            "weather": self.weather,
            "power": self.power,
            "music": self.music,
            "radio": self.radio,
        }


@dataclass
class LyraGlass:
    name: str = ""
    note: str = ""
    fill: int = 0
    revision: int = 0
    acknowledged_revision: int = 0
    last_action: str = "none"
    previous_fill: int = 0

    @property
    def active(self) -> bool:
        return bool(self.name)

    def to_event(self) -> dict:
        return {
            "active": self.active,
            "name": self.name,
            "note": self.note,
            "fill": self.fill,
        }


@dataclass
class BarShift:
    turn_count: int = 0
    phase: str = "early"
    revision: int = 0
    acknowledged_revision: int = 0
    previous_phase: str = "early"

    def to_event(self) -> dict:
        return {"turn_count": self.turn_count, "phase": self.phase}


def bar_phase_for_turn(turn_count: int) -> str:
    if turn_count >= 14:
        return "last_call"
    if turn_count >= 9:
        return "late"
    if turn_count >= 4:
        return "deep"
    return "early"


class BarModeController:
    """Swap into a memory-free bar context and restore the parked work context."""

    def __init__(self, agent: "ReActAgent", output_mode: str = "atomic") -> None:
        if output_mode not in BAR_OUTPUT_MODES:
            raise ValueError(f"Unknown bar output mode: {output_mode}")
        self.agent = agent
        self._output_mode = output_mode
        self._work_context: AgentContext | None = None
        self._work_memory_store = None
        self._work_external_memory_provider = None
        self._work_skill_store = None
        self._work_task_store = None
        self._work_tools_enabled = True
        self._work_tool_allowlist: set[str] | None = None
        self._work_runtime_context_provider = None
        self._work_runtime_turn_context_provider = None
        self._work_generation_overrides_provider = None
        self._work_finalize_after_tools_provider = None
        self._work_forced_tool_name: str | None = None
        self._drink = BarDrink()
        self._ambiance = BarAmbiance()
        self._lyra_glass = LyraGlass()
        self._shift = BarShift()

    @property
    def active(self) -> bool:
        return self._work_context is not None

    @property
    def output_mode(self) -> str:
        return self._output_mode

    def _apply_output_contract(self) -> None:
        if not self.active:
            return
        if self._output_mode == "atomic":
            self.agent.tool_allowlist = set(BAR_ATOMIC_TOOL_NAMES)
            self.agent.forced_tool_name = BAR_TURN_TOOL_NAME
        else:
            self.agent.tool_allowlist = set(BAR_STREAM_TOOL_NAMES)
            self.agent.forced_tool_name = None

    @staticmethod
    def generation_overrides() -> dict:
        """Use conservative sampling in Bar without changing the work model profile."""
        return dict(BAR_GENERATION_OVERRIDES)

    def set_output_mode(self, output_mode: str) -> bool:
        normalized = str(output_mode).strip().lower()
        if normalized not in BAR_OUTPUT_MODES:
            raise ValueError(f"Unknown bar output mode: {output_mode}")
        changed = normalized != self._output_mode
        self._output_mode = normalized
        if self.active:
            self.agent.context.set_system_prompt(bar_mode_prompt(normalized))
            self._apply_output_contract()
        return changed

    def _bar_context(self, path: str | Path) -> AgentContext:
        if self._work_context is None:
            raise RuntimeError("Work context is not parked")
        work = self._work_context
        prompt = bar_mode_prompt(self._output_mode)
        bar = AgentContext(
            system_prompt=prompt,
            max_messages=work.max_messages,
            max_prompt_tokens=work.max_prompt_tokens,
            show_reasoning=work.show_reasoning,
        )
        bar.set_system_prompt(prompt)
        bar.set_tools_token_cost(estimate_value_tokens(
            self.agent.tools.to_openai_tools(names=BAR_SCENE_TOOL_NAMES)
        ))
        bar.compressor = ContextCompressor(self.agent.llm)
        bar.compaction_observer = work.compaction_observer
        bar.enforce_session_ownership = work.enforce_session_ownership
        bar.set_session(str(path))
        if not bar.load():
            SessionStore(path).save({
                "system_prompt": prompt,
                "messages": [],
                "show_reasoning": bar.show_reasoning,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "last_prompt_tokens": 0,
            })
        # Saved bar sessions keep their conversation, but always receive the
        # current mode contract after prompt revisions.
        bar.set_system_prompt(prompt)
        self._drink = self._load_state(path, bar.messages)
        return bar

    @staticmethod
    def _drink_state_path(path: str | Path) -> Path:
        session = Path(path)
        return session.parent / ".state" / f"{session.stem}.json"

    def _load_state(self, path: str | Path, messages: list[dict] | None = None) -> BarDrink:
        state_path = self._drink_state_path(path)
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            data: dict = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            data = {}
        try:
            fill = max(0, min(3, int(data.get("fill", 0))))
        except (TypeError, ValueError):
            fill = 0
        try:
            revision = max(0, int(data.get("revision", 0)))
            acknowledged = max(0, min(revision, int(data.get("acknowledged_revision", revision))))
            previous_fill = max(0, min(3, int(data.get("previous_fill", fill))))
            unnamed_counter = max(0, int(data.get("unnamed_counter", 0)))
        except (TypeError, ValueError):
            revision = acknowledged = 0
            previous_fill = fill
            unnamed_counter = 0
        drink = BarDrink(
            name=str(data.get("name", ""))[:48],
            note=str(data.get("note", ""))[:120],
            tone=str(data.get("tone", "clear")) if data.get("tone") in BAR_DRINK_TONES else "clear",
            temperature=(
                str(data.get("temperature", "cold"))
                if data.get("temperature") in BAR_DRINK_TEMPERATURES else "cold"
            ),
            fill=fill,
            revision=revision,
            acknowledged_revision=acknowledged,
            last_action=str(data.get("last_action", "none"))[:32],
            last_actor=str(data.get("last_actor", ""))[:16],
            previous_fill=previous_fill,
            unnamed_counter=unnamed_counter,
        )

        ambiance_raw = data.get("ambiance")
        ambiance_data: dict = ambiance_raw if isinstance(ambiance_raw, dict) else {}
        ambiance_revision = max(0, int(ambiance_data.get("revision", 0) or 0))
        self._ambiance = BarAmbiance(
            weather=str(ambiance_data.get("weather", "rain")) if ambiance_data.get("weather") in BAR_WEATHERS else "rain",
            power=str(ambiance_data.get("power", "stable")) if ambiance_data.get("power") in BAR_POWER_STATES else "stable",
            music=str(ambiance_data.get("music", "low_synth")) if ambiance_data.get("music") in BAR_MUSIC_STATES else "low_synth",
            radio=str(ambiance_data.get("radio", "static")) if ambiance_data.get("radio") in BAR_RADIO_STATES else "static",
            revision=ambiance_revision,
            acknowledged_revision=max(0, min(ambiance_revision, int(ambiance_data.get("acknowledged_revision", ambiance_revision) or 0))),
            last_change=str(ambiance_data.get("last_change", "none"))[:160],
        )

        lyra_raw = data.get("lyra_glass")
        lyra_data: dict = lyra_raw if isinstance(lyra_raw, dict) else {}
        lyra_revision = max(0, int(lyra_data.get("revision", 0) or 0))
        lyra_fill = max(0, min(3, int(lyra_data.get("fill", 0) or 0)))
        self._lyra_glass = LyraGlass(
            name=str(lyra_data.get("name", ""))[:48],
            note=str(lyra_data.get("note", ""))[:80],
            fill=lyra_fill,
            revision=lyra_revision,
            acknowledged_revision=max(0, min(lyra_revision, int(lyra_data.get("acknowledged_revision", lyra_revision) or 0))),
            last_action=str(lyra_data.get("last_action", "none"))[:32],
            previous_fill=max(0, min(3, int(lyra_data.get("previous_fill", lyra_fill) or 0))),
        )

        shift_raw = data.get("shift")
        shift_data: dict = shift_raw if isinstance(shift_raw, dict) else {}
        fallback_turns = sum(1 for item in (messages or []) if item.get("role") == "assistant")
        turn_count = max(0, int(shift_data.get("turn_count", fallback_turns) or 0))
        phase = bar_phase_for_turn(turn_count)
        shift_revision = max(0, int(shift_data.get("revision", 0) or 0))
        self._shift = BarShift(
            turn_count=turn_count,
            phase=phase,
            revision=shift_revision,
            acknowledged_revision=max(0, min(shift_revision, int(shift_data.get("acknowledged_revision", shift_revision) or 0))),
            previous_phase=str(shift_data.get("previous_phase", phase))[:24],
        )
        return drink

    def _save_drink(self) -> None:
        if not self.active or not self.agent.context.session_path:
            return
        state_path = self._drink_state_path(self.agent.context.session_path)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        payload = {
            **asdict(self._drink),
            "ambiance": asdict(self._ambiance),
            "lyra_glass": asdict(self._lyra_glass),
            "shift": asdict(self._shift),
        }
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, state_path)

    @property
    def drink(self) -> BarDrink:
        return self._drink

    @property
    def ambiance(self) -> BarAmbiance:
        return self._ambiance

    @property
    def lyra_glass(self) -> LyraGlass:
        return self._lyra_glass

    @property
    def shift(self) -> BarShift:
        return self._shift

    def scene_prompt(self) -> str:
        drink = self._drink
        ambiance = self._ambiance
        lyra_glass = self._lyra_glass
        shift = self._shift
        fill_label = {3: "full", 2: "two_sips_left", 1: "one_sip_left", 0: "empty"}[drink.fill]
        lyra_fill_label = {3: "full", 2: "two_sips_left", 1: "one_sip_left", 0: "empty"}[lyra_glass.fill]
        lines = [
            "[BAR SCENE STATE — runtime only, authoritative; never quote this block verbatim]",
            f"session: {self.session_name or 'unknown'}",
            f"shift_turn: {shift.turn_count}",
            f"night_phase: {shift.phase}",
            f"drink_present: {'true' if drink.active else 'false'}",
            f"drink_name: {drink.name or 'none'}",
            f"drink_note: {drink.note or 'none'}",
            f"temperature: {drink.temperature if drink.active else 'none'}",
            f"fill: {drink.fill}/3",
            f"glass_state: {fill_label if drink.active else 'no_glass'}",
            f"state_revision: {drink.revision}",
            f"ambiance: weather={ambiance.weather}, power={ambiance.power}, music={ambiance.music}, radio={ambiance.radio}",
            f"lyra_drink_present: {'true' if lyra_glass.active else 'false'}",
            f"lyra_drink_name: {lyra_glass.name or 'none'}",
            f"lyra_drink_note: {lyra_glass.note or 'none'}",
            f"lyra_glass_state: {lyra_fill_label if lyra_glass.active else 'no_glass'} ({lyra_glass.fill}/3)",
        ]
        pending = drink.revision > drink.acknowledged_revision
        lines.append(f"new_scene_event: {'true' if pending else 'false'}")
        if pending:
            lines.extend([
                f"last_action: {drink.last_action}",
                f"last_actor: {drink.last_actor or 'unknown'}",
                f"previous_fill: {drink.previous_fill}/3",
            ])
        ambiance_pending = ambiance.revision > ambiance.acknowledged_revision
        lines.append(f"new_ambiance_event: {'true' if ambiance_pending else 'false'}")
        if ambiance_pending:
            lines.append(f"ambiance_change: {ambiance.last_change}")
        lyra_pending = lyra_glass.revision > lyra_glass.acknowledged_revision
        lines.append(f"new_lyra_glass_event: {'true' if lyra_pending else 'false'}")
        if lyra_pending:
            lines.extend([
                f"lyra_last_action: {lyra_glass.last_action}",
                f"lyra_previous_fill: {lyra_glass.previous_fill}/3",
            ])
        phase_pending = shift.revision > shift.acknowledged_revision
        lines.append(f"new_phase_event: {'true' if phase_pending else 'false'}")
        if phase_pending:
            lines.append(f"phase_change: {shift.previous_phase} -> {shift.phase}")
        output_contract = (
            "Every response must be submitted through exactly one bar_turn call. Put visible prose in reply and all real scene changes in actions; use an empty actions array for pure conversation."
            if self._output_mode == "atomic"
            else "Use standard ReAct. For pure conversation, reply normally. For a real scene change, first return only individual scene tool calls with no visible prose; after their results arrive, produce the final reply from the committed state. Do not call bar_turn and never print tool syntax as ordinary text."
        )
        consistency_contract = (
            "The runtime commits reply and actions together, so never describe a scene change without its matching action in the same bar_turn."
            if self._output_mode == "atomic"
            else "Never describe a scene change before its tool succeeds. After the tool result, describe only the committed state and do not request the completed action again."
        )
        lines.extend([
            "Treat fill and glass_state as current truth. Only react as if an action just happened when new_scene_event is true.",
            f"output_mode: {self._output_mode}",
            output_contract,
            "The user alone controls sipping. Never add an action that drinks from the user's glass.",
            "Use set_ambiance only for a meaningful scene beat; stable ambiance should usually remain unchanged.",
            "Lyra controls only her own glass through pour_lyra_drink and sip_lyra_drink actions. Do not confuse her glass with the user's glass.",
            "A served drink action must carry the exact name used in reply; use an empty name only when it is intentionally unnamed.",
            consistency_contract,
            "[END BAR SCENE STATE]",
        ])
        return "\n".join(lines)

    def scene_turn_prompt(self) -> str:
        """Compact truth placed beside the latest request in the LLM-only prompt."""
        drink = self._drink
        pending = drink.revision > drink.acknowledged_revision
        if drink.active:
            fill_label = {3: "FULL", 2: "TWO SIPS LEFT", 1: "ONE SIP LEFT", 0: "EMPTY"}[drink.fill]
            lines = [
                f"BAR STATE r{drink.revision}: {drink.name}",
                f"glass: {fill_label} ({drink.fill}/3)",
            ]
        else:
            lines = [
                f"BAR STATE r{drink.revision}: NO DRINK",
                "glass: NO GLASS (0/3)",
            ]
        if pending:
            lines.append(
                f"latest scene event: {drink.last_actor or 'unknown'} {drink.last_action} "
                f"({drink.previous_fill}/3 -> {drink.fill}/3)"
            )
        turn_contract = (
            "Submit exactly one bar_turn call. Put the complete user-visible response in reply and matching scene changes in actions."
            if self._output_mode == "atomic"
            else "Reply normally for immediate streaming. Do not call bar_turn; call an individual scene tool only for a real change. A complete reply accompanying successful scene calls is final and must not be repeated."
        )
        lines.extend([
            "This state overrides every older drink or glass claim in conversation history.",
            "Make all claims about the user's drink, Lyra's glass, ambiance, and shift phase agree with it.",
            f"output mode: {self._output_mode.upper()}",
            turn_contract,
            f"shift: {self._shift.phase} (turn {self._shift.turn_count})",
            f"ambiance: {self._ambiance.weather} | {self._ambiance.power} | {self._ambiance.music} | {self._ambiance.radio}",
            (
                f"Lyra's glass: {self._lyra_glass.name} ({self._lyra_glass.fill}/3)"
                if self._lyra_glass.active else "Lyra's glass: EMPTY (0/3)"
            ),
        ])
        return "\n".join(lines)

    def acknowledge_scene(self) -> None:
        changed = False
        for state in (self._drink, self._ambiance, self._lyra_glass, self._shift):
            if state.acknowledged_revision < state.revision:
                state.acknowledged_revision = state.revision
                changed = True
        if changed:
            self._save_drink()

    def complete_turn(self) -> None:
        """Acknowledge visible events and advance the persisted fictional shift."""
        self.acknowledge_scene()
        self._shift.turn_count += 1
        next_phase = bar_phase_for_turn(self._shift.turn_count)
        if next_phase != self._shift.phase:
            self._shift.previous_phase = self._shift.phase
            self._shift.phase = next_phase
            self._shift.revision += 1
        self._save_drink()

    def commit_turn(self, reply: str, actions: list[dict] | None = None) -> str:
        """Atomically apply one model-authored reply and its scene actions."""
        if not self.active:
            raise RuntimeError("Bar mode is not active")
        clean_reply = str(reply).strip()
        if not clean_reply:
            raise ValueError("Bar turn reply cannot be empty")
        if len(clean_reply) > 6000:
            raise ValueError("Bar turn reply is too long")
        if actions is None:
            actions = []
        if not isinstance(actions, list):
            raise ValueError("Bar turn actions must be an array")
        if len(actions) > 4:
            raise ValueError("Bar turn supports at most four scene actions")

        snapshot = (
            deepcopy(self._drink),
            deepcopy(self._ambiance),
            deepcopy(self._lyra_glass),
        )
        try:
            for raw_action in actions:
                if not isinstance(raw_action, dict):
                    raise ValueError("Each bar action must be an object")
                action = str(raw_action.get("type") or "").strip()
                if action == "serve_drink":
                    missing = [key for key in ("name", "note", "tone", "temperature") if key not in raw_action]
                    if missing:
                        raise ValueError(f"serve_drink missing fields: {', '.join(missing)}")
                    note = str(raw_action.get("note") or "").strip()
                    if not note:
                        raise ValueError("serve_drink requires note")
                    self.serve_drink(
                        str(raw_action.get("name") or ""),
                        note,
                        str(raw_action.get("tone") or "clear"),
                        str(raw_action.get("temperature") or "cold"),
                    )
                elif action == "refill_drink":
                    self.refill_drink()
                elif action == "rename_drink":
                    if "name" not in raw_action:
                        raise ValueError("rename_drink requires name")
                    self.rename_drink(str(raw_action.get("name") or ""))
                elif action == "set_ambiance":
                    if not any(key in raw_action for key in ("weather", "power", "music", "radio")):
                        raise ValueError("set_ambiance requires at least one changed field")
                    self.set_ambiance(
                        raw_action.get("weather"),
                        raw_action.get("power"),
                        raw_action.get("music"),
                        raw_action.get("radio"),
                    )
                elif action == "pour_lyra_drink":
                    if "name" not in raw_action:
                        raise ValueError("pour_lyra_drink requires name")
                    self.pour_lyra_drink(
                        str(raw_action.get("name") or ""),
                        str(raw_action.get("note") or ""),
                    )
                elif action == "sip_lyra_drink":
                    self.sip_lyra_drink()
                else:
                    raise ValueError(f"Unknown bar action: {action or 'missing type'}")
        except Exception:
            self._drink, self._ambiance, self._lyra_glass = snapshot
            self._save_drink()
            raise
        self._save_drink()
        return clean_reply

    def set_ambiance(
        self,
        weather: str | None = None,
        power: str | None = None,
        music: str | None = None,
        radio: str | None = None,
    ) -> BarAmbiance:
        if not self.active:
            raise RuntimeError("Bar mode is not active")
        requested = {
            "weather": (weather, BAR_WEATHERS),
            "power": (power, BAR_POWER_STATES),
            "music": (music, BAR_MUSIC_STATES),
            "radio": (radio, BAR_RADIO_STATES),
        }
        changes = []
        for field, (value, allowed) in requested.items():
            if value is None:
                continue
            if value not in allowed:
                raise ValueError(f"Invalid bar ambiance {field}: {value}")
            previous = getattr(self._ambiance, field)
            if value != previous:
                setattr(self._ambiance, field, value)
                changes.append(f"{field}:{previous}->{value}")
        if changes:
            self._ambiance.revision += 1
            self._ambiance.last_change = ", ".join(changes)
            self._save_drink()
        return self._ambiance

    def pour_lyra_drink(self, name: str, note: str = "") -> LyraGlass:
        if not self.active:
            raise RuntimeError("Bar mode is not active")
        clean_name = " ".join(str(name).split())[:48]
        if not clean_name:
            raise ValueError("Lyra's drink needs a name")
        previous = self._lyra_glass.fill
        self._lyra_glass = LyraGlass(
            name=clean_name,
            note=" ".join(str(note).split())[:80],
            fill=3,
            revision=self._lyra_glass.revision + 1,
            acknowledged_revision=self._lyra_glass.acknowledged_revision,
            last_action="poured",
            previous_fill=previous,
        )
        self._save_drink()
        return self._lyra_glass

    def sip_lyra_drink(self) -> LyraGlass:
        if not self.active:
            raise RuntimeError("Bar mode is not active")
        if not self._lyra_glass.active:
            raise ValueError("Lyra has no drink to sip")
        if self._lyra_glass.fill <= 0:
            raise ValueError("Lyra's glass is already empty")
        previous = self._lyra_glass.fill
        self._lyra_glass.fill -= 1
        self._lyra_glass.revision += 1
        self._lyra_glass.last_action = "sipped"
        self._lyra_glass.previous_fill = previous
        self._save_drink()
        return self._lyra_glass

    def serve_drink(
        self,
        name: str = "",
        note: str = "",
        tone: str = "clear",
        temperature: str = "cold",
    ) -> BarDrink:
        if not self.active:
            raise RuntimeError("Bar mode is not active")
        clean_name = " ".join(str(name).split())[:48]
        unnamed_counter = self._drink.unnamed_counter
        if not clean_name:
            unnamed_counter += 1
            clean_name = f"未命名调饮 #{unnamed_counter}"
        clean_note = " ".join(str(note).split())[:120]
        clean_tone = tone if tone in BAR_DRINK_TONES else "clear"
        clean_temperature = temperature if temperature in BAR_DRINK_TEMPERATURES else "cold"
        previous = self._drink.fill
        revision = self._drink.revision + 1
        acknowledged = self._drink.acknowledged_revision
        self._drink = BarDrink(
            clean_name,
            clean_note,
            clean_tone,
            clean_temperature,
            3,
            revision,
            acknowledged,
            "served",
            "lyra",
            previous,
            unnamed_counter,
        )
        self._save_drink()
        return self._drink

    def sip(self) -> tuple[BarDrink, str]:
        if not self.active:
            raise RuntimeError("Bar mode is not active")
        if not self._drink.active:
            return self._drink, "There is no drink on the bar yet."
        if self._drink.fill <= 0:
            return self._drink, f"{self._drink.name} is already empty."
        previous = self._drink.fill
        self._drink.fill -= 1
        self._drink.revision += 1
        self._drink.last_action = "sipped"
        self._drink.last_actor = "user"
        self._drink.previous_fill = previous
        self._save_drink()
        labels = {2: "two sips left", 1: "one sip left", 0: "empty"}
        return self._drink, f"You take a sip of {self._drink.name}: {labels[self._drink.fill]}."

    def refill_drink(self) -> BarDrink:
        if not self.active:
            raise RuntimeError("Bar mode is not active")
        if not self._drink.active:
            raise ValueError("There is no current drink to refill")
        if self._drink.fill >= 3:
            return self._drink
        previous = self._drink.fill
        self._drink.fill = 3
        self._drink.revision += 1
        self._drink.last_action = "refilled"
        self._drink.last_actor = "lyra"
        self._drink.previous_fill = previous
        self._save_drink()
        return self._drink

    def rename_drink(self, name: str) -> BarDrink:
        if not self.active:
            raise RuntimeError("Bar mode is not active")
        if not self._drink.active:
            raise ValueError("There is no current drink to rename")
        clean_name = " ".join(str(name).split())[:48]
        if not clean_name:
            raise ValueError("A new drink name is required")
        if clean_name == self._drink.name:
            return self._drink
        self._drink.name = clean_name
        self._drink.revision += 1
        self._drink.last_action = "renamed"
        self._drink.last_actor = "lyra"
        self._drink.previous_fill = self._drink.fill
        self._save_drink()
        return self._drink

    @property
    def session_name(self) -> str:
        path = self.agent.context.session_path if self.active else ""
        return Path(path).stem if path else ""

    def enter(self, path: str | Path) -> bool:
        if self.active:
            return False

        work = self.agent.context
        self.agent.end_session("bar_enter")
        work.save()
        self._work_context = work
        self._work_memory_store = self.agent.memory_store
        self._work_external_memory_provider = self.agent.external_memory_provider
        self._work_skill_store = self.agent.skill_store
        self._work_task_store = self.agent.task_store
        self._work_tools_enabled = self.agent.tools_enabled
        self._work_tool_allowlist = self.agent.tool_allowlist
        self._work_runtime_context_provider = self.agent.runtime_context_provider
        self._work_runtime_turn_context_provider = self.agent.runtime_turn_context_provider
        self._work_generation_overrides_provider = self.agent.generation_overrides_provider
        self._work_finalize_after_tools_provider = self.agent.finalize_after_tools_provider
        self._work_forced_tool_name = self.agent.forced_tool_name

        self.agent.context = self._bar_context(path)
        self.agent.begin_session()
        self.agent.memory_store = None
        self.agent.external_memory_provider = None
        self.agent.skill_store = None
        self.agent.task_store = None
        self.agent.tools_enabled = True
        self.agent.runtime_context_provider = self.scene_prompt
        self.agent.runtime_turn_context_provider = self.scene_turn_prompt
        self.agent.generation_overrides_provider = self.generation_overrides
        # Stream mode follows the normal ReAct loop: tool call, execution,
        # then a separate model step for the visible final response.
        self.agent.finalize_after_tools_provider = None
        self._apply_output_contract()
        return True

    def switch(self, path: str | Path) -> None:
        if not self.active:
            raise RuntimeError("Bar mode is not active")
        self.agent.end_session("session_switch")
        self.agent.context.save()
        self.agent.context = self._bar_context(path)
        self.agent.begin_session()

    def leave(self) -> bool:
        if not self.active or self._work_context is None:
            return False

        self.agent.end_session("bar_leave")
        self.agent.context.save()
        self.agent.context = self._work_context
        self.agent.begin_session()
        self.agent.memory_store = self._work_memory_store
        self.agent.external_memory_provider = self._work_external_memory_provider
        self.agent.skill_store = self._work_skill_store
        self.agent.task_store = self._work_task_store
        self.agent.tools_enabled = self._work_tools_enabled
        self.agent.tool_allowlist = self._work_tool_allowlist
        self.agent.runtime_context_provider = self._work_runtime_context_provider
        self.agent.runtime_turn_context_provider = self._work_runtime_turn_context_provider
        self.agent.generation_overrides_provider = self._work_generation_overrides_provider
        self.agent.finalize_after_tools_provider = self._work_finalize_after_tools_provider
        self.agent.forced_tool_name = self._work_forced_tool_name

        self._work_context = None
        self._work_memory_store = None
        self._work_external_memory_provider = None
        self._work_skill_store = None
        self._work_task_store = None
        self._work_tool_allowlist = None
        self._work_runtime_context_provider = None
        self._work_runtime_turn_context_provider = None
        self._work_generation_overrides_provider = None
        self._work_finalize_after_tools_provider = None
        self._work_forced_tool_name = None
        self._drink = BarDrink()
        self._ambiance = BarAmbiance()
        self._lyra_glass = LyraGlass()
        self._shift = BarShift()
        return True
