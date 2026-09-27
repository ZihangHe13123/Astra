"""Deep runtime diagnostics used by the /doctor command."""

from __future__ import annotations

import inspect
import json
from collections import Counter
from typing import Any

from agent.runtime.capabilities import build_runtime_capabilities, computer_use_capability
from agent.runtime.metrics import runtime_metrics
from agent.runtime.runtime_identity import runtime_identity
from agent.runtime.providers import DEFAULT_PROVIDER_REGISTRY
from agent.runtime.tools.web import exa_configuration_status
from agent.runtime.tracing import tracing_status

from .connections import probe_profile
from .models import model_profiles
from .search_preferences import resolve_startup_search_provider


def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(item.get("status") or "unknown") for item in items)
    return dict(sorted(counts.items()))


def build_runtime_diagnostics(
    agent,
    *,
    startup_profile: dict[str, Any] | None = None,
    mcp_manager=None,
    task_store=None,
    process_manager=None,
    section: str = "",
) -> str:
    """Build a fast, read-only snapshot without endpoint or network probes."""
    refresh_tool_cost = getattr(agent, "refresh_tool_token_cost", None)
    if callable(refresh_tool_cost):
        refresh_tool_cost()
    context = agent.context.prompt_token_breakdown()
    cache_hit = int(getattr(agent.context, "total_cache_hit_tokens", 0) or 0)
    cache_miss = int(getattr(agent.context, "total_cache_miss_tokens", 0) or 0)
    cache_input = cache_hit + cache_miss
    tool_details = list(agent.tools.describe())
    tool_groups = Counter(str(item.get("group") or "unknown") for item in tool_details)
    mcp_statuses = []
    if mcp_manager is not None:
        mcp_statuses = [
            {
                "name": str(status.name),
                "state": str(status.state),
                "tools": int(status.tools),
                "error": str(status.error)[:240],
            }
            for status in mcp_manager.statuses
        ]

    task_error = ""
    tasks: list[dict[str, Any]] = []
    if task_store is not None:
        try:
            tasks = list(task_store.list_tasks(limit=100))
        except Exception as exc:
            task_error = f"{type(exc).__name__}: {exc}"
    process_error = ""
    processes: list[dict[str, Any]] = []
    if process_manager is not None:
        try:
            processes = list(process_manager.list(include_completed=True))
        except Exception as exc:
            process_error = f"{type(exc).__name__}: {exc}"

    computer = computer_use_capability(agent)
    snapshot: dict[str, Any] = {
        "version": 1,
        "runtime": runtime_identity(compare_disk=True),
        "model": str(agent.llm.config.model),
        "context": context,
        "prompt_cache": {
            "hit_tokens": cache_hit,
            "miss_tokens": cache_miss,
            "hit_rate": round(cache_hit / cache_input, 4) if cache_input else 0.0,
            "telemetry_available": cache_input > 0,
            "stable_tool_prefix": bool(getattr(agent, "prompt_cache_stable_tools", False)),
        },
        "tools": {
            "registered": len(agent.tools.tool_names),
            "schema_revision": int(agent.tools.schema_revision),
            "groups": dict(sorted(tool_groups.items())),
            "hooks": dict(agent.tools.hooks.counts),
        },
        "mcp": {
            "servers": mcp_statuses,
            "ready": sum(item["state"] == "ready" for item in mcp_statuses),
            "total": len(mcp_statuses),
            "tools": sum(item["tools"] for item in mcp_statuses if item["state"] == "ready"),
        },
        "tasks": {
            "recent_limit": 100,
            "counts": _status_counts(tasks),
            "error": task_error,
        },
        "processes": {
            "retained": len(processes),
            "counts": _status_counts(processes),
            "error": process_error,
        },
        "startup": dict(startup_profile or {}),
        "reliability": runtime_metrics.snapshot(),
        "computer": {
            "state": computer.state.value,
            "detail": computer.detail,
        },
    }
    selected = str(section or "").strip().lower()
    if selected == "json":
        return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True)
    if selected not in {"", "context", "startup", "mcp", "tasks", "computer"}:
        return (
            f"Unknown diagnostics section: {selected}. "
            "Available: computer, context, startup, mcp, tasks, json"
        )

    def counts_text(counts: dict[str, int]) -> str:
        return ", ".join(f"{name}={value}" for name, value in counts.items()) or "none"

    current = int(context["estimated_current"])
    limit = int(context["limit"])
    pct = current / limit * 100 if limit else 0.0
    context_lines = [
        "Context:",
        f"  Current estimate: {current:,} / {limit:,} ({pct:.1f}%)",
        f"  System: {context['system']:,}; tools: {context['tools']:,}; messages: {context['messages']:,}",
        f"  Messages: {context['message_count']} ({counts_text(context['role_messages'])})",
        f"  Last provider/request estimate: {context['last_request']:,}",
        (
            f"  Prompt cache: {cache_hit:,} hit / {cache_miss:,} miss "
            f"({cache_hit / cache_input * 100:.1f}% hit)"
            if cache_input else "  Prompt cache: provider telemetry not available yet"
        ),
        (
            f"  Tool schema: revision {snapshot['tools']['schema_revision']}; "
            f"stable prefix={'on' if snapshot['prompt_cache']['stable_tool_prefix'] else 'off'}"
        ),
    ]
    startup = snapshot["startup"]
    runtime = snapshot["runtime"]
    if startup.get("phases"):
        phases = sorted(
            startup["phases"],
            key=lambda item: float(item.get("delta_ms", 0)),
            reverse=True,
        )[:5]
        startup_lines = [
            "Startup:",
            f"  Total: {float(startup.get('total_ms', 0)):.1f} ms",
            *[
                f"  {item.get('name', 'unknown')}: {float(item.get('delta_ms', 0)):.1f} ms"
                for item in phases
            ],
        ]
        if startup.get("path"):
            startup_lines.append(f"  Report: {startup['path']}")
    else:
        startup_lines = [
            "Startup:",
            "  Profiling is off; set ASTRA_PROFILE_STARTUP=1 before launch for phase timings.",
        ]
    mcp = snapshot["mcp"]
    startup_lines.extend([
        f"  Loaded runtime: pid={runtime['pid']} run={runtime['run_id']}",
        f"  Loaded source: {runtime['source_sha256']}",
        f"  Source changed since startup: {runtime['source_changed']}",
        f"  Execution limits: {json.dumps(runtime['execution_limits'], sort_keys=True)}",
    ])
    mcp_lines = [f"MCP: {mcp['ready']}/{mcp['total']} ready; {mcp['tools']} tools"]
    mcp_lines.extend(
        f"  {item['name']}: {item['state']} ({item['tools']} tools)"
        + (f" — {item['error']}" if item["error"] else "")
        for item in mcp_statuses
    )
    task_lines = [
        f"Tasks (latest {snapshot['tasks']['recent_limit']}): {counts_text(snapshot['tasks']['counts'])}",
        f"Processes (retained): {counts_text(snapshot['processes']['counts'])}",
    ]
    if task_error:
        task_lines.append(f"  Task store unavailable: {task_error}")
    if process_error:
        task_lines.append(f"  Process store unavailable: {process_error}")
    computer_lines = [
        "Computer Use:",
        f"  State: {computer.state.value}",
        f"  Detail: {computer.detail}",
    ]
    if selected == "computer":
        return "Astra diagnostics (computer):\n" + "\n".join(computer_lines)
    if selected == "context":
        return "Astra diagnostics (context):\n" + "\n".join(context_lines)
    if selected == "startup":
        return "Astra diagnostics (startup):\n" + "\n".join(startup_lines)
    if selected == "mcp":
        return "Astra diagnostics (mcp):\n" + "\n".join(mcp_lines)
    if selected == "tasks":
        return "Astra diagnostics (tasks):\n" + "\n".join(task_lines)
    return "\n".join([
        "Astra diagnostics (local snapshot):",
        *context_lines,
        *startup_lines,
        *mcp_lines,
        *task_lines,
        *computer_lines,
        (
            f"Tools: {snapshot['tools']['registered']} registered "
            f"({counts_text(snapshot['tools']['groups'])})"
        ),
        "Reliability:\n" + runtime_metrics.render(),
    ])


async def build_doctor_report(agent, sandbox=None, mcp_manager=None, section: str = "") -> str:
    section = str(section).strip().lower()
    if section == "metrics":
        return "Agent doctor (metrics):\n" + runtime_metrics.render()
    if section in {"", "computer"}:
        computer_runtime = getattr(agent, "_computer_runtime", None)
        probe = getattr(computer_runtime, "probe", None)
        if callable(probe):
            result = probe()
            if inspect.isawaitable(result):
                await result
    external_memory = getattr(agent, "external_memory_provider", None)
    if external_memory is not None:
        await external_memory.health()
    runtime = build_runtime_capabilities(agent, sandbox, mcp_manager)
    if section:
        aliases = {"memory": "memory", "browser": "browser", "computer": "computer", "mcp": "mcp", "task": "tasks", "tasks": "tasks"}
        prefix = aliases.get(section, section)
        selected = runtime.select(prefix)
        if not selected.statuses:
            available = "browser, computer, mcp, memory, metrics, sandbox, skills, tasks"
            return f"Unknown doctor section: {section}. Available: {available}"
        lines = [f"Agent doctor ({section}):", selected.render()]
        if prefix == "browser":
            from agent.runtime.browser_control_install import status as host_status
            from .browser_commands import execute_browser_command
            for browser in ("edge", "chrome"):
                host = host_status(browser=browser)
                lines.append(f"Native host ({browser}): {host['state']}; {host.get('message', 'live connection checked below')}")
            output, error = await execute_browser_command(["status"], agent.tools)
            lines.append(output or error)
        if prefix == "mcp" and mcp_manager is not None:
            lines.append(mcp_manager.report())
        return "\n".join(lines)

    config = agent.llm.config
    profiles = model_profiles()
    profile = profiles.get(config.model)
    lines = [
        "Agent doctor:",
        f"Model: {config.model}",
        f"Provider: {config.provider}",
        f"Registered providers: {', '.join(DEFAULT_PROVIDER_REGISTRY.names)}",
        f"Base URL: {config.base_url}",
        f"Capabilities: {', '.join(sorted(config.capabilities)) or '(unspecified)'}",
    ]
    if profile is None:
        lines.append("Endpoint probe: skipped (current model is not in the catalog)")
    else:
        probe = await probe_profile(
            config.model,
            profile,
            api_key=config.api_key,
            base_url=config.base_url,
        )
        lines.append(f"Endpoint probe: {'ok' if probe.ok else 'failed'} — {probe.message}")
        lines.append(f"API key source: {profile.api_key_env} ({'set' if config.api_key else 'not set'}; runtime config)")
    sandbox_name = getattr(sandbox, "description", type(sandbox).__name__) if sandbox is not None else "none"
    lines.append(f"Sandbox: {sandbox_name}")
    lines.append(
        "ReAct iterations: "
        + ("unlimited (guarded)" if agent.max_iterations == 0 else str(agent.max_iterations))
    )
    lines.append(f"Tool policy: {agent.tools.policy.mode}")
    risks = Counter(item["risk"] for item in agent.tools.describe())
    lines.append("Tool risks: " + ", ".join(f"{name}={count}" for name, count in sorted(risks.items())))
    exa_ready, exa_source = exa_configuration_status()
    default_search = resolve_startup_search_provider()
    lines.append(
        f"Web search: default={default_search}; Exa={'configured' if exa_ready else 'not configured'} ({exa_source})"
    )
    trace = tracing_status()
    trace_detail = f" — {trace.error}" if trace.error else ""
    lines.append(f"Tracing: {'enabled' if trace.enabled else 'disabled'} ({trace.backend}){trace_detail}")
    lines.append("Reliability metrics:\n" + runtime_metrics.render())
    if mcp_manager is None:
        lines.append("MCP: not initialized")
    else:
        lines.append(mcp_manager.report())
    lines.append(runtime.render())
    return "\n".join(lines)
