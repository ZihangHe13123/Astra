"""One scheduled read-only check, bootstrapped before optional runtime imports."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

from agent.runtime.jobs.store import MAX_OUTPUT, JobStore

READ_TOOLS = frozenset({"read_file", "stat_file", "search_files", "search_text", "current_time"})
WEB_TOOLS = frozenset({"search_web", "fetch_url", "search_status"})


async def run_check(store: JobStore, run: dict) -> None:
    # Import and resolve provider configuration only after home/ownership is fixed.
    from agent.cli.connections import is_local_url
    from agent.cli.environment import load_project_env
    from agent.cli.model_catalog import configured_model_catalog
    from agent.cli.models import prompt_token_budget
    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.llm import LLMClient, LLMConfig
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.files import register_file_tools
    from agent.runtime.tools.registry import ToolRegistry
    from agent.runtime.tools.time import register_time_tools
    from agent.sandbox.local import LocalSandbox

    spec = run["spec"]
    root = store.run_dir(run["id"])
    progress = root / "progress"
    progress.touch()
    load_project_env(Path(__file__).resolve().parents[2])
    os.chdir(spec["workdir"])
    entry = configured_model_catalog().resolve_persisted(spec["model"])
    if entry is None:
        raise ValueError("The scheduled model is no longer configured.")
    profile = entry.profile
    if profile.api_key_env:
        from dotenv import dotenv_values
        from agent.runtime.paths import env_file

        # Refresh custom-provider keys too. A stale parent credential must not
        # override an explicit key in this job owner's profile configuration.
        configured_key = dotenv_values(env_file(Path(__file__).resolve().parents[2])).get(profile.api_key_env)
        if configured_key is not None:
            os.environ[profile.api_key_env] = str(configured_key)
    key = profile.api_key() or ("local" if is_local_url(profile.base_url) else "")
    if not key and profile.provider not in {"openai-codex", "claude-code"}:
        raise ValueError("The scheduled model is not connected in this home.")
    config = LLMConfig(provider=profile.provider, model=profile.model_id or entry.key,
                       api_key=key, base_url=profile.base_url, context_limit=profile.context_limit,
                       capabilities=profile.capabilities, **profile.generation_settings())
    sandbox = LocalSandbox(timeout=30, workdir=spec["workdir"])
    source = ToolRegistry(artifact_dir=root / "tool-results")
    register_file_tools(source, workdir=spec["workdir"], sandbox=sandbox)
    register_time_tools(source)
    names = set(READ_TOOLS)
    if spec["web"]:
        from agent.cli.search_preferences import resolve_startup_search_provider
        from agent.runtime.tools.web import register_web_tools

        register_web_tools(source, sandbox, default_provider=resolve_startup_search_provider())
        names.update(WEB_TOOLS)
    # Execution registry, not just schemas: writes and dynamically discovered tools
    # cannot execute even if the model names them directly.
    tools = ToolRegistry(policy=source.policy, artifact_dir=root / "tool-results")
    for name in sorted(names):
        tool = source.get(name)
        if tool is None or tool.risk not in {"read", "network"}:
            raise ValueError("Scheduled read-only tool contract changed.")
        tools.register(tool)
    client = LLMClient(config)
    agent = ReActAgent("scheduled-check", client, tools, system_prompt=(
        "Perform one bounded, read-only scheduled check. The original user request below is the authority. "
        "Do not infer new permission, modify files, send messages, create schedules, or start background work. "
        "Return a useful result or explain the blocker. The host delivers the result to a local inbox."
    ), max_iterations=spec["max_iterations"], progressive_tools=False,
        vision_cache_root=root / "images", turn_timeout_seconds=spec["timeout_seconds"])
    agent._sandbox = sandbox
    agent.tool_allowlist = names
    agent.context.max_prompt_tokens = prompt_token_budget(
        profile.context_limit, config.max_tokens, model=config.model, provider=config.provider,
    )
    agent.context.set_session(str(root / "session.json"))
    output = ""
    error = ""
    last_progress = 0.0
    try:
        msg = Msg(sender="user", role="user", content=[ContentBlock.text(
            f"Original user request:\n{spec['original_request']}\n\nScheduled check:\n{spec['prompt']}"
        )],
                  metadata={"source": "scheduled_job", "request_id": run["id"]})
        async for event in agent.reply_stream(msg):
            now = time.monotonic()
            if now - last_progress >= 0.1:
                progress.touch()
                last_progress = now
            if event.get("type") == "chunk":
                output = (output + str(event.get("content", "")))[:MAX_OUTPUT]
            elif event.get("type") == "error":
                # Provider errors may embed URLs or secrets; keep the public code only.
                error = str(event.get("code") or "scheduled_check_failed")[:120]
        await agent.context.save_async()
        if not output.strip() and not error:
            error = "scheduled_check_returned_no_result"
        store.finish(run["id"], "failed" if error else "completed", output=output, detail=error)
    finally:
        agent.end_session("scheduled_job_finished")
        await agent.close_external_memory()
        await sandbox.close()
        provider_client = getattr(client.provider, "client", None)
        close = getattr(provider_client, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    home = args.home.expanduser().resolve()
    if Path(os.environ.get("ASTRA_HOME", "")).resolve() != home:
        raise ValueError("Worker home does not match its launch scope.")
    from agent.launcher.installation import discover
    from agent.launcher.locking import RuntimeLease

    with RuntimeLease(discover(), "scheduled job worker"):
        store = JobStore(home)
        with store.run_lock(args.run):
            if not store.start(args.run):
                return 1
            try:
                asyncio.run(run_check(store, store.run(args.run)))
            except Exception as exc:
                store.finish(args.run, "failed", detail=f"Scheduled check failed: {type(exc).__name__}")
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
