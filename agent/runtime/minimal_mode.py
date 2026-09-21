"""Minimal Mode: a dsh-style minimal agent preset on Windows.

The dsh (DeepSeek Harness) ``minimal`` preset is a fixed-prompt, two-tool
coding composition: a single sentence as the complete system prompt, plus
persistent bash and str_replace_editor. Its purpose is to send the model the
exact RL-aligned prompt and tool schemas used during post-training; community
benchmarks reproduce the V4-Pro grayscale-era ability with it while standard
presets score lower.

Windows cannot run dsh's persistent-PTY bash (``process-inspector`` only
implements linux/darwin), so Astra's Minimal Mode keeps the DSH-facing tool
shapes and supplies a WSL shell sidecar:

- persistent bash      -> ``bash`` (a WSL Bash command surface; Docker remains
  the active sandbox for PTC)
- str_replace_editor  -> ``str_replace_editor`` (the DSH-compatible
  view/create/str_replace/insert editor over Astra's guarded filesystem)
- PTC extension        -> ``run_code`` (a Docker-isolated Python subprocess
  program that can orchestrate the Minimal tool set internally)

Isolation follows BarModeController: the work context is parked, a fresh
session with a single-sentence prompt and a strict tool allowlist is swapped
in, and leaving restores every parked field untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from .context import AgentContext
from .session_store import SessionStore
from .token_estimator import estimate_value_tokens

if TYPE_CHECKING:
    from .react import ReActAgent


MINIMAL_SYSTEM_PROMPT = "You are a helpful software engineer assistant."

# The first two names preserve DSH's model-facing contract. ``run_code`` is an
# explicit Docker-only Astra extension, while the read-only web surface is kept
# in this isolated allowlist for research tasks. Browser actions and every other
# work-mode capability stay out of the strict allowlist.
MINIMAL_NETWORK_TOOL_NAMES = frozenset({
    "search_web",
    "web_extract",
})
MINIMAL_TOOL_NAMES = frozenset({"bash", "str_replace_editor", "run_code"}) | MINIMAL_NETWORK_TOOL_NAMES

# Status text emitted by ``/minimal status``.  It must stay in lockstep with
# MINIMAL_TOOL_NAMES: bash and str_replace_editor are the DSH-shaped tool pair,
# run_code is the Docker-only PTC extension, and search_web/web_extract are the
# read-only web tools retained inside the isolated allowlist.
MINIMAL_STATUS_OPEN_TEMPLATE = (
    "Minimal mode: open · session {session} · PTC sandbox ON · "
    "tools bash/str_replace_editor/run_code + read-only web "
    "(search_web/web_extract) · memory off · skills off · compaction off · "
    "work context parked"
)


class MinimalModeController:
    """Swap into a zero-injection minimal session and restore the parked work context."""

    def __init__(self, agent: "ReActAgent") -> None:
        self.agent = agent
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
        self._work_code_mode = "native"
        self._work_minimal_mode = False
        self._work_sandbox_mode: str | None = None
        self._work_yolo = False

    @property
    def active(self) -> bool:
        return self._work_context is not None

    @property
    def session_name(self) -> str:
        path = self.agent.context.session_path if self.active else ""
        return Path(path).stem if path else ""

    @staticmethod
    def _close_persistent_bash(agent: "ReActAgent") -> None:
        sandbox = getattr(agent, "_sandbox", None)
        close = getattr(sandbox, "close_persistent_bash", None)
        if not callable(close):
            close = getattr(sandbox, "close_wsl_shell", None)
        if callable(close):
            close()

    @staticmethod
    def _close_wsl_shell(agent: "ReActAgent") -> None:
        """Compatibility alias for the former WSL-specific lifecycle hook."""
        MinimalModeController._close_persistent_bash(agent)

    def _minimal_context(self, path: Path) -> AgentContext:
        work = self._work_context
        if work is None:
            raise RuntimeError("Work context is not parked")
        minimal = AgentContext(
            system_prompt=MINIMAL_SYSTEM_PROMPT,
            max_messages=work.max_messages,
            max_prompt_tokens=work.max_prompt_tokens,
            show_reasoning=work.show_reasoning,
            # dsh's minimal preset does not mount compaction.  This must be a
            # separate switch from ``compressor=None`` because Astra also has
            # deterministic and legacy fallback compaction layers.
            compaction_enabled=False,
        )
        # No compressor: dsh minimal has no context compaction, and long
        # tool-heavy sessions must not silently fold tool results.
        minimal.set_system_prompt(MINIMAL_SYSTEM_PROMPT)
        minimal.enforce_session_ownership = work.enforce_session_ownership
        minimal.set_session(str(path))
        if not minimal.load():
            SessionStore(path).save({
                "system_prompt": MINIMAL_SYSTEM_PROMPT,
                "messages": [],
                "show_reasoning": minimal.show_reasoning,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "last_prompt_tokens": 0,
            })
        minimal.set_system_prompt(MINIMAL_SYSTEM_PROMPT)
        return minimal

    def _apply_isolation(self) -> None:
        """Apply the Minimal-only runtime fields to the active context."""
        self.agent.memory_store = None
        self.agent.external_memory_provider = None
        self.agent.skill_store = None
        self.agent.task_store = None
        self.agent.tools_enabled = True
        self.agent.runtime_context_provider = None
        self.agent.runtime_turn_context_provider = None
        self.agent.generation_overrides_provider = None
        self.agent.finalize_after_tools_provider = None
        self.agent.forced_tool_name = None
        # ``_apply_code_mode`` removes run_code in "native" and keeps only
        # run_code + approval fallbacks in "code"; "both" passes the strict
        # allowlist through unchanged.
        self.agent.code_mode = "both"
        self.agent.tool_allowlist = set(MINIMAL_TOOL_NAMES)
        self.agent.context.set_stable_system_suffix("")
        self.agent.context.set_tools_token_cost(
            estimate_value_tokens(
                self.agent.tools.to_openai_tools(names=MINIMAL_TOOL_NAMES)
            )
        )

    def enter(self, path: Path) -> bool:
        if self.active:
            return False

        # A previous Minimal session must not leave cwd/env state behind for
        # the next isolated session.
        self._close_persistent_bash(self.agent)

        # PTC is a subprocess boundary, not merely a schema flag.  Force the
        # runtime router onto Docker for the isolated session and restore the
        # user's previous selection on leave.  Do this before parking context
        # so a failed router switch cannot leave a half-entered mode behind.
        sandbox = getattr(self.agent, "_sandbox", None)
        switch_sandbox = getattr(sandbox, "switch", None)
        self._work_sandbox_mode = (
            str(getattr(sandbox, "mode"))
            if callable(switch_sandbox) and getattr(sandbox, "mode", None)
            else None
        )
        if callable(switch_sandbox) and self._work_sandbox_mode != "docker":
            switch_sandbox("docker")

        work = self.agent.context
        self.agent.end_session("minimal_enter")
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
        self._work_code_mode = self.agent.code_mode
        self._work_minimal_mode = self.agent.minimal_mode
        # YOLO is a process-wide approval bypass. Park it with the work
        # context so a Minimal session always starts approval-safe and
        # leaving restores the user's previous work-mode choice.
        self._work_yolo = bool(self.agent.tools.yolo)
        self.agent.tools.yolo = False

        # Suppress skill/project-instruction suffix injection before the new
        # session begins (begin_session refreshes the skill catalog).
        self.agent.minimal_mode = True
        self.agent.context = self._minimal_context(path)
        self.agent.begin_session()
        self._apply_isolation()
        return True

    def switch(self, path: Path) -> None:
        """Save the active Minimal session and switch to another isolated one."""
        if not self.active:
            raise RuntimeError("Minimal mode is not active")
        self._close_persistent_bash(self.agent)
        self.agent.end_session("session_switch")
        self.agent.context.save()
        self.agent.context = self._minimal_context(path)
        self.agent.begin_session()
        self.agent.tools.yolo = False
        self._apply_isolation()

    def leave(self) -> bool:
        if not self.active or self._work_context is None:
            return False

        self._close_persistent_bash(self.agent)
        self.agent.end_session("minimal_leave")
        self.agent.context.save()
        self.agent.context = self._work_context
        # begin_session refreshes the skill catalog; the guard is still active
        # here so the restored suffix cannot be touched by the swap itself.
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
        self.agent.code_mode = self._work_code_mode
        self.agent.minimal_mode = self._work_minimal_mode
        self.agent.tools.yolo = self._work_yolo

        sandbox = getattr(self.agent, "_sandbox", None)
        switch_sandbox = getattr(sandbox, "switch", None)
        if callable(switch_sandbox) and self._work_sandbox_mode is not None:
            switch_sandbox(self._work_sandbox_mode)

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
        self._work_code_mode = "native"
        self._work_minimal_mode = False
        self._work_sandbox_mode = None
        self._work_yolo = False
        return True

    def reset(self) -> bool:
        """Clear only the active Minimal session without touching parked work."""
        if not self.active:
            return False

        self._close_persistent_bash(self.agent)
        self.agent.end_session("minimal_reset")
        path = Path(self.agent.context.session_path)
        self.agent.context.reset()
        self.agent.context.set_system_prompt(MINIMAL_SYSTEM_PROMPT)
        self.agent.context.set_stable_system_suffix("")
        self.agent.context.set_tools_token_cost(
            estimate_value_tokens(
                self.agent.tools.to_openai_tools(names=MINIMAL_TOOL_NAMES)
            )
        )
        SessionStore(path).save({
            "system_prompt": MINIMAL_SYSTEM_PROMPT,
            "messages": [],
            "show_reasoning": self.agent.context.show_reasoning,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "last_prompt_tokens": 0,
        })
        self.agent.tools.yolo = False
        self.agent.begin_session()
        return True
