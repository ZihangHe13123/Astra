"""Runtime capability truth used by diagnostics and contract tests."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class CapabilityState(StrEnum):
    CONFIGURED = "configured"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CapabilityStatus:
    name: str
    state: CapabilityState
    detail: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.state is CapabilityState.AVAILABLE and not self.warnings


@dataclass
class CapabilityReport:
    statuses: list[CapabilityStatus] = field(default_factory=list)

    def add(
        self,
        name: str,
        state: CapabilityState | str,
        detail: str = "",
        warnings: Iterable[str] = (),
    ) -> CapabilityStatus:
        item = CapabilityStatus(
            name=str(name),
            state=CapabilityState(state),
            detail=str(detail),
            warnings=tuple(str(value) for value in warnings if str(value).strip()),
        )
        self.statuses.append(item)
        return item

    def get(self, name: str) -> CapabilityStatus | None:
        return next((item for item in self.statuses if item.name == name), None)

    def select(self, prefix: str) -> CapabilityReport:
        normalized = str(prefix).strip().lower()
        return CapabilityReport([
            item for item in self.statuses
            if item.name == normalized or item.name.startswith(normalized + ".")
        ])

    def render(self) -> str:
        lines = ["Runtime capabilities:"]
        for item in self.statuses:
            detail = f" — {item.detail}" if item.detail else ""
            lines.append(f"  {item.name}: {item.state.value}{detail}")
            lines.extend(f"    warning: {warning}" for warning in item.warnings)
        return "\n".join(lines)


def computer_use_capability(agent) -> CapabilityStatus:
    """Return local Computer Use truth without probing or inspecting tool names."""
    if sys.platform != "darwin":
        return CapabilityStatus(
            "computer.use",
            CapabilityState.UNSUPPORTED,
            "Computer Use is currently supported only on macOS.",
        )
    runtime = getattr(agent, "_computer_runtime", None)
    capability = getattr(runtime, "capability_status", None)
    if not callable(capability):
        return CapabilityStatus(
            "computer.use",
            CapabilityState.UNSUPPORTED,
            "Local Computer Use runtime is not registered on this surface.",
        )
    try:
        value = capability()
        if not isinstance(value, tuple) or len(value) != 2:
            raise TypeError("capability status must be a state/detail pair")
        state, detail = value
        return CapabilityStatus("computer.use", CapabilityState(state), str(detail))
    except (TypeError, ValueError):
        return CapabilityStatus(
            "computer.use",
            CapabilityState.DEGRADED,
            "Local Computer Use capability state is invalid.",
        )


def build_runtime_capabilities(agent, sandbox=None, mcp_manager=None) -> CapabilityReport:
    """Inspect live runtime objects; do not infer support from config alone."""
    report = CapabilityReport()
    computer = computer_use_capability(agent)
    report.add(computer.name, computer.state, computer.detail, computer.warnings)

    memory = getattr(agent, "memory_store", None)
    if memory is None:
        report.add("memory.builtin", CapabilityState.UNSUPPORTED, "store is not initialized")
    else:
        path = Path(getattr(memory, "path", ""))
        record_store = getattr(memory, "record_store", None)
        record_detail = "structured records unavailable"
        if record_store is not None:
            stats = record_store.stats()
            record_detail = (
                f"records={stats['total']}; "
                f"fts5={'enabled' if stats['fts_enabled'] else 'fallback-like'}"
            )
        core_usage = [memory.core_usage(scope) for scope in ("memory", "user")]
        core_over_limit = any(bool(item["over_limit"]) for item in core_usage)
        report.add(
            "memory.builtin",
            CapabilityState.AVAILABLE if path.exists() and not core_over_limit else CapabilityState.DEGRADED,
            f"sqlite={path}; core=bounded-markdown; "
            f"core_dir={getattr(memory, 'core_dir', '(unknown)')}; legacy_archive={record_detail}",
            ("one or more Core Markdown files exceed their hard limit",) if core_over_limit else (),
        )
        router = getattr(agent, "memory_router", None)
        report.add(
            "memory.recall_router",
            CapabilityState.AVAILABLE if router is not None else CapabilityState.UNSUPPORTED,
            "Hindsight-only; query-driven; max_results=" + str(getattr(router, "recall_limit", "(unknown)"))
            if router is not None else "router is not initialized",
        )
        retainer = getattr(agent, "memory_retainer", None)
        auto_retain = bool(getattr(retainer, "enabled", False))
        report.add(
            "memory.auto_retain",
            CapabilityState.AVAILABLE if retainer is not None and auto_retain else CapabilityState.CONFIGURED,
            (
                f"mode={getattr(retainer, 'mode', 'unknown')}; opted-in tool evidence only; "
                "conversation extraction is disabled"
                if retainer is not None and auto_retain
                else "disabled by MEMORY_AUTO_RETAIN" if retainer is not None else "retainer is not initialized"
            ),
        )
        external = getattr(agent, "external_memory_provider", None)
        if external is None:
            report.add(
                "memory.hindsight",
                CapabilityState.CONFIGURED,
                "disabled by HINDSIGHT_ENABLED",
            )
        else:
            health = external.last_health
            state = {
                "available": CapabilityState.AVAILABLE,
                "configured": CapabilityState.CONFIGURED,
                "unavailable": CapabilityState.DEGRADED,
            }.get(health.state, CapabilityState.DEGRADED)
            report.add(
                "memory.hindsight",
                state,
                health.detail,
                (
                    "automatic historical recall is unavailable; session_search remains explicit"
                    if state is CapabilityState.DEGRADED else ""
                ,),
            )
            sync_enabled = bool(getattr(external, "session_sync_enabled", False))
            report.add(
                "memory.hindsight.session_sync",
                (
                    state
                    if sync_enabled
                    else CapabilityState.CONFIGURED
                ),
                (
                    f"enabled; every_n_turns={external.session_sync_every_n_turns}; "
                    f"server_processing_model={external.processing_model}; "
                    f"retain_async={external.session_sync_async}; "
                    f"last={external.last_sync_detail}"
                    if sync_enabled
                    else "disabled by HINDSIGHT_SESSION_SYNC"
                ),
                (
                    "session delivery will retry from its durable cursor"
                    if sync_enabled and state is CapabilityState.DEGRADED else ""
                ,),
            )

    skills = getattr(agent, "skill_store", None)
    if skills is None:
        report.add("skills", CapabilityState.UNSUPPORTED, "store is not initialized")
    else:
        try:
            count = len(skills.list())
            report.add("skills", CapabilityState.AVAILABLE, f"{count} valid skill(s) in {skills.root}")
        except Exception as exc:
            report.add("skills", CapabilityState.DEGRADED, f"{type(exc).__name__}: {exc}")

    task_store = getattr(agent, "task_store", None)
    report.add(
        "tasks.durable",
        CapabilityState.AVAILABLE if task_store is not None else CapabilityState.UNSUPPORTED,
        "SQLite TaskRun/Step/Event" if task_store is not None else "task store is not initialized",
    )

    tools = getattr(agent, "tools", None)
    tool_names = {item["name"] for item in tools.describe()} if tools is not None else set()
    read_tools = {"fetch_url", "web_extract", "extract_url", "search_web"} & tool_names
    report.add(
        "browser.read",
        CapabilityState.AVAILABLE if read_tools else CapabilityState.UNSUPPORTED,
        ", ".join(sorted(read_tools)) if read_tools else "no URL reader registered",
    )
    interactive = {
        "browser_open",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_handoff",
        "browser_resume",
    }
    missing = sorted(interactive - tool_names)
    report.add(
        "browser.interactive",
        CapabilityState.CONFIGURED if not missing else CapabilityState.UNSUPPORTED,
        "tools registered; live host/extension readiness is checked separately" if not missing else "missing: " + ", ".join(missing),
    )

    if sandbox is None:
        report.add("sandbox", CapabilityState.UNSUPPORTED, "not initialized")
    else:
        name = getattr(sandbox, "description", type(sandbox).__name__)
        report.add("sandbox", CapabilityState.AVAILABLE, str(name))

    if mcp_manager is None:
        report.add("mcp", CapabilityState.UNSUPPORTED, "manager is not initialized")
    else:
        states = {status.state for status in getattr(mcp_manager, "statuses", [])}
        warnings = tuple(getattr(mcp_manager, "config_warnings", ()))
        if "ready" in states:
            state = CapabilityState.DEGRADED if (states & {"error", "unavailable"}) else CapabilityState.AVAILABLE
        elif states <= {"disabled"}:
            state = CapabilityState.CONFIGURED
        else:
            state = CapabilityState.DEGRADED
        report.add("mcp", state, f"{mcp_manager.loaded_tools} loaded tool(s)", warnings)

    return report
