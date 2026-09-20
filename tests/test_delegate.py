import asyncio
import json
import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

from agent.runtime.tools import delegate
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.hooks import ToolDecision
from agent.runtime.tools.delegate import (
    DelegateConcurrencyGate,
    _DelegateSlotLease,
    _build_subagent_registry,
    _context_snapshot,
    _expand_requested_tools,
    _investigation_max_tokens,
    _investigation_token_floor,
    _is_unparsed_tool_markup,
    _resolve_model,
    _worker_llm,
    register_delegate_tools,
)
from agent.runtime.tools.processes import ProcessManager
from agent.runtime.tools.registry import ToolDef, ToolRegistry
from agent.runtime.tools.files import register_file_tools
from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.git import register_git_tools
from agent.sandbox.local import LocalSandbox
from agent.runtime.worker import WorkerSpec


class SequenceLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, **kwargs):
        del kwargs
        return self.responses.pop(0)


class SlowLLM:
    async def chat(self, **kwargs):
        del kwargs
        await asyncio.sleep(5)
        return {"content": "too late", "tool_calls": []}


def test_delegate_retains_codex_continuation_without_logging_ciphertext(process_manager):
    from agent.runtime.codex_wire import completed_response
    raw = completed_response({"status": "completed", "output": [
        {"type": "reasoning", "encrypted_content": "PRIVATE-CIPHER", "summary": []},
        {"type": "function_call", "call_id": "c", "name": "read_file", "arguments": '{"path":"a.txt"}'},
    ]}, account="account", model="model")
    seen, transcript = [], []
    class CapturingLLM(SequenceLLM):
        async def chat(self, **kwargs):
            import copy
            seen.append(copy.deepcopy(kwargs["messages"]))
            return await super().chat(**kwargs)
    llm = CapturingLLM([raw, {"content": "done", "tool_calls": []}])
    async def work():
        registry = ToolRegistry()
        _register_read_tool(registry)
        register_delegate_tools(registry, llm_getter=lambda: llm, session_id_getter=lambda: "session",
            on_session_event=lambda sid, event: transcript.append(event))
        result = await registry.execute("delegate_task", {"goal": "read", "timeout": 5})
        assert json.loads(result["output"])["worker_status"] == "completed"
    asyncio.run(work())
    assistant = next(m for m in seen[1] if m.get("role") == "assistant")
    assert assistant["_provider_state"] == raw["_provider_state"]
    assert "PRIVATE-CIPHER" not in json.dumps(transcript)


@pytest.mark.parametrize("kind", ["subagent", "workspace", "worktree"])
def test_running_delegate_observes_parent_yolo_changes(tmp_path, kind):
    parent = ToolRegistry()
    sandbox = LocalSandbox(workdir=str(tmp_path))
    child_root = tmp_path / "child"
    child_root.mkdir()
    register_file_tools(parent, workdir=str(tmp_path), sandbox=sandbox)
    if kind == "subagent":
        child = _build_subagent_registry(parent, "worker")
    elif kind == "workspace":
        child = delegate._create_workspace_registry(parent, sandbox, child_root, mode="worker")
    else:
        child = delegate._create_worker_worktree_registry(parent, sandbox, child_root)
    for enabled in (True, False, True, False):
        parent.yolo = enabled
        assert child.yolo is enabled
    assert ToolRegistry().yolo is False, "unrelated sessions must keep independent switches"


@pytest.mark.parametrize("kind", ["subagent", "workspace", "worktree"])
def test_rebound_tools_preserve_parent_hooks_audit_and_session_grant_cleanup(tmp_path, kind):
    async def scenario():
        parent = ToolRegistry()
        approvals, audit, executions = [], [], []

        async def approve(request):
            approvals.append(request)
            return "session"

        def probe():
            executions.append("read")
            return "observed"

        parent.register(ToolDef("read_probe", "probe", {"type": "object", "properties": {}},
                                probe, group="codegraph"))
        parent.set_approval_handler(approve)
        parent.set_approval_audit_handler(audit.append)
        parent.policy.add_rule_shortcut("read_probe", "ask")
        sandbox = LocalSandbox(workdir=str(tmp_path))
        child_root = tmp_path / "child"
        child_root.mkdir()
        if kind == "subagent":
            child = _build_subagent_registry(parent, "worker")
        elif kind == "workspace":
            child = delegate._create_workspace_registry(parent, sandbox, child_root, mode="worker")
        else:
            child = delegate._create_worker_worktree_registry(parent, sandbox, child_root)
        assert not (await child.execute("read_probe", {}))["error"]
        assert not (await parent.execute("read_probe", {}))["error"]
        assert len(approvals) == 1 and audit
        parent.hooks.dispatch_session_end("session", "new session")
        assert not (await child.execute("read_probe", {}))["error"]
        assert len(approvals) == 2
        parent.hooks.on_tool_decision(lambda name, args, tool: ToolDecision.deny("existing parent policy"))
        denied = await child.execute("read_probe", {})
        assert "existing parent policy" in denied["error"]
        assert len(executions) == 3
    asyncio.run(scenario())


def test_delegate_concurrency_gate_does_not_swallow_cancelled_acquire():
    async def scenario():
        gate = DelegateConcurrencyGate(global_limit=1, owner_limit=1)
        loop = asyncio.get_running_loop()

        async def worker():
            await gate.acquire("owner", loop.time() + 10)
            await asyncio.Future()

        task = asyncio.create_task(worker())

        # Let wait_for's immediately available semaphore acquire finish, but
        # cancel before the wrapper task has resumed from that inner future.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)

        try:
            assert task.cancelled()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_delegate_slot_lease_releases_once_and_can_reacquire():
    async def scenario():
        gate = DelegateConcurrencyGate(global_limit=1, owner_limit=1)
        lease = _DelegateSlotLease(gate, "owner")
        deadline = asyncio.get_running_loop().time() + 1

        await lease.acquire(deadline)
        assert lease.acquired is True
        assert gate._global._value == 0
        lease.release()
        lease.release()
        assert lease.acquired is False
        assert gate._global._value == 1

        await lease.acquire(deadline)
        assert lease.acquired is True
        assert gate._global._value == 0
        lease.release()

    asyncio.run(scenario())


def test_active_budget_pauses_idle_time_and_counts_reacquire_time():
    now = 100.0
    budget = delegate._ActiveBudget(10.0, clock=lambda: now)
    budget.resume()
    now = 103.0
    budget.pause()
    assert budget.remaining() == pytest.approx(7.0)
    now = 1003.0
    assert budget.remaining() == pytest.approx(7.0)
    budget.resume()
    now = 1005.5
    assert budget.remaining() == pytest.approx(4.5)
    assert budget.deadline() == pytest.approx(1010.0)
    assert delegate._keep_alive_deadline(budget, 1007.0) == pytest.approx(1007.0)
    assert delegate._keep_alive_remaining(
        budget,
        1007.0,
        now=now,
    ) == pytest.approx(1.5)


@pytest.fixture
def process_manager(monkeypatch, tmp_path):
    manager = ProcessManager(artifact_dir=tmp_path / "processes")
    monkeypatch.setattr(delegate, "_sub_processes", manager)
    return manager


def test_worktree_git_commands_hide_windows_console(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(delegate.subprocess, "run", fake_run)
    worktree = delegate._create_detached_worktree(
        SimpleNamespace(workdir=str(tmp_path)),
    )
    assert delegate._finalize_detached_worktree(tmp_path, worktree) is False

    expected_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    assert len(calls) == 3
    assert all(call_kwargs["creationflags"] == expected_flags for _, call_kwargs in calls)


def test_worktree_setup_and_cleanup_run_outside_event_loop(
    monkeypatch,
    tmp_path,
    process_manager,
):
    main_thread_id = threading.get_ident()
    worker_thread_ids = []
    worktree = tmp_path / "worker"

    def create_worktree(_sandbox):
        worker_thread_ids.append(threading.get_ident())
        return worktree

    def finalize_worktree(_root, _worktree):
        worker_thread_ids.append(threading.get_ident())
        return False

    monkeypatch.setattr(delegate, "_create_detached_worktree", create_worktree)
    monkeypatch.setattr(delegate, "_finalize_detached_worktree", finalize_worktree)
    monkeypatch.setattr(
        delegate,
        "_create_worker_worktree_registry",
        lambda *_args: ToolRegistry(),
    )

    async def scenario():
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: SequenceLLM([
                {"content": "worktree task complete", "tool_calls": []},
            ]),
            sandbox=SimpleNamespace(workdir=str(tmp_path)),
        )

        result = await registry.execute(
            "delegate_task",
            {
                "goal": "bounded worktree task",
                "mode": "worker",
                "isolation": "worktree",
                "foreground_yield_ms": 5_000,
            },
        )

        assert result["error"] == ""
        assert json.loads(result["output"])["result"] == "worktree task complete"

    asyncio.run(scenario())
    assert len(worker_thread_ids) == 2
    assert all(thread_id != main_thread_id for thread_id in worker_thread_ids)


def test_explicit_workspace_root_must_be_safe_and_distinct_from_isolation(
    tmp_path,
):
    sandbox_root = tmp_path / "repo"
    workspace = sandbox_root / ".worktrees" / "feature"
    outside = tmp_path / "outside"
    workspace.mkdir(parents=True)
    outside.mkdir()
    sandbox = LocalSandbox(workdir=str(sandbox_root))

    assert delegate._validated_workspace_root(
        sandbox, str(workspace), isolation="shared"
    ) == str(workspace.resolve())
    with pytest.raises(ValueError, match="absolute"):
        delegate._validated_workspace_root(
            sandbox, ".worktrees/feature", isolation="shared"
        )
    with pytest.raises(ValueError, match="inside the sandbox root"):
        delegate._validated_workspace_root(
            sandbox, str(outside), isolation="shared"
        )
    with pytest.raises(ValueError, match="existing directory"):
        delegate._validated_workspace_root(
            sandbox, str(sandbox_root / "missing"), isolation="shared"
        )
    with pytest.raises(ValueError, match="isolation=shared"):
        delegate._validated_workspace_root(
            sandbox, str(workspace), isolation="worktree"
        )


def test_workspace_scoped_registry_rebinds_worker_and_reviewer_tools(tmp_path):
    async def scenario():
        sandbox_root = tmp_path / "repo"
        workspace = sandbox_root / ".worktrees" / "feature"
        workspace.mkdir(parents=True)
        (sandbox_root / "marker.txt").write_text("backend-root", encoding="utf-8")
        (workspace / "marker.txt").write_text("feature-root", encoding="utf-8")
        sandbox = LocalSandbox(workdir=str(sandbox_root))
        registry = ToolRegistry()
        registry.yolo = True
        register_file_tools(registry, workdir=str(sandbox_root), sandbox=sandbox)
        register_code_tools(registry, sandbox)
        register_git_tools(registry, workdir=str(sandbox_root))

        worker = delegate._create_workspace_registry(
            registry, sandbox, workspace, mode="worker"
        )
        reviewer = delegate._create_workspace_registry(
            registry, sandbox, workspace, mode="explorer"
        )

        worker_read = await worker.execute("read_file", {"path": "marker.txt"})
        reviewer_read = await reviewer.execute("read_file", {"path": "marker.txt"})
        shell = await worker.execute(
            "execute_shell", {"command": "cd" if os.name == "nt" else "pwd"}
        )
        assert "feature-root" in worker_read["output"]
        assert "feature-root" in reviewer_read["output"]
        assert str(workspace.resolve()) in shell["output"]
        assert reviewer.get("execute_shell") is None

        sibling = workspace.parent / "sibling"
        sibling.mkdir()
        worker_escape = await worker.execute(
            "git_status", {"path": str(sibling)}
        )
        reviewer_escape = await reviewer.execute(
            "git_status", {"path": str(sibling)}
        )
        assert "Path escapes workspace" in worker_escape["error"]
        assert "Path escapes workspace" in reviewer_escape["error"]

    asyncio.run(scenario())


def _register_read_tool(registry, name="read_file", *, group="files", risk="read"):
    async def run(path=""):
        return f"read:{path}"

    registry.register(ToolDef(
        name=name,
        description=name,
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
        fn=run,
        group=group,
        risk=risk,
    ))


def test_subagent_registry_is_small_and_read_only():
    registry = ToolRegistry()
    _register_read_tool(registry)
    _register_read_tool(
        registry,
        "mcp__codegraph__explore",
        group="mcp:codegraph",
        risk="network",
    )
    _register_read_tool(
        registry,
        "mcp__other__write",
        group="mcp:other",
        risk="write",
    )

    sub = _build_subagent_registry(registry)

    assert set(sub.tool_names) == {"read_file", "mcp__codegraph__explore"}


def test_worker_registry_adds_workspace_tools_but_not_external_writes():
    registry = ToolRegistry()
    _register_read_tool(registry)
    _register_read_tool(registry, "edit_file", group="files", risk="write")
    _register_read_tool(registry, "execute_shell", group="code", risk="execute")
    _register_read_tool(registry, "channel_send", group="channels", risk="write")

    worker = _build_subagent_registry(registry, "worker")

    assert set(worker.tool_names) == {"read_file", "edit_file", "execute_shell"}
    assert "channel_send" not in worker.tool_names
    with pytest.raises(ValueError, match="mode"):
        _build_subagent_registry(registry, "unrestricted")


def test_capability_aliases_expand_with_selected_mode():
    registry = ToolRegistry()
    _register_read_tool(registry)
    _register_read_tool(registry, "edit_file", group="files", risk="write")
    _register_read_tool(registry, "execute_shell", group="code", risk="execute")
    _register_read_tool(registry, "git_status", group="git", risk="read")

    explorer = _build_subagent_registry(registry, "explorer")
    worker = _build_subagent_registry(registry, "worker")

    assert _expand_requested_tools(["file"], explorer) == ["read_file"]
    assert _expand_requested_tools(["file"], worker) == ["edit_file", "read_file"]
    assert _expand_requested_tools(["terminal"], worker) == [
        "execute_shell",
        "git_status",
    ]


def test_unavailable_capability_alias_fails_loud_before_spawn():
    registry = ToolRegistry()
    _register_read_tool(registry)
    explorer = _build_subagent_registry(registry, "explorer")

    with pytest.raises(ValueError, match="capability 'codegraph'.*unavailable"):
        _expand_requested_tools(["codegraph"], explorer)


def test_subagent_registry_excludes_conclave():
    registry = ToolRegistry()
    _register_read_tool(registry, "conclave", group="research", risk="network")

    assert "conclave" not in _build_subagent_registry(registry).tool_names


def test_subagent_registries_exclude_top_level_interaction_tools():
    registry = ToolRegistry()
    _register_read_tool(registry, "ask_user_question", group="core")
    _register_read_tool(registry, "plan_update", group="core")

    assert delegate.TOP_LEVEL_ONLY_TOOLS == frozenset({"ask_user_question", "plan_update"})
    assert "ask_user_question" not in _build_subagent_registry(registry, "explorer").tool_names
    assert "plan_update" not in _build_subagent_registry(registry, "explorer").tool_names
    assert "ask_user_question" not in _build_subagent_registry(registry, "worker").tool_names
    assert "plan_update" not in _build_subagent_registry(registry, "worker").tool_names


def test_worktree_registry_excludes_top_level_tools_but_copies_parent_tool(tmp_path):
    registry = ToolRegistry()
    _register_read_tool(registry, "ask_user_question", group="core")
    _register_read_tool(registry, "plan_update", group="core")
    _register_read_tool(registry, "parent_read", group="codegraph")
    worktree = tmp_path / "worker"
    worktree.mkdir()
    sandbox = SimpleNamespace(current=SimpleNamespace(
        timeout=30,
        workdir=str(tmp_path),
        max_output_bytes=200_000,
    ))

    worker = delegate._create_worker_worktree_registry(registry, sandbox, worktree)

    assert "ask_user_question" not in worker.tool_names
    assert "plan_update" not in worker.tool_names
    assert "parent_read" in worker.tool_names


def test_context_snapshot_keeps_recent_visible_turns_only():
    messages = [
        {"role": "system", "content": "secret system"},
        {"role": "user", "content": "old request"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "recent request"},
        {"role": "assistant", "content": "working", "tool_calls": [{"id": "x"}]},
        {"role": "tool", "content": "private tool output"},
        {"role": "assistant", "content": "recent answer"},
        {"role": "user", "content": "current request"},
    ]

    snapshot = _context_snapshot(
        messages,
        fork_turns="2",
        explicit_context="focus on delegate.py",
        max_chars=500,
    )

    assert "old request" not in snapshot
    assert "recent request" in snapshot
    assert "recent answer" in snapshot
    assert "current request" in snapshot
    assert "private tool output" not in snapshot
    assert "secret system" not in snapshot
    assert snapshot.endswith("focus on delegate.py")


def test_context_snapshot_none_and_bounded_all():
    messages = [{"role": "user", "content": "x" * 500}]

    assert "x" * 20 not in _context_snapshot(messages, fork_turns="none")
    assert len(_context_snapshot(messages, fork_turns="all", max_chars=80)) <= 80
    with pytest.raises(ValueError, match="fork_turns"):
        _context_snapshot(messages, fork_turns="0")


def test_remote_worker_uses_independent_bounded_client(monkeypatch):
    class Provider:
        def __init__(self):
            self.active = 0
            self.peak = 0

        async def chat(self, messages, tools):
            del messages, tools
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.02)
            self.active -= 1
            return {"content": "ok"}

    provider = Provider()
    active = LLMClient(
        LLMConfig(base_url="https://example.test", max_concurrent_requests=1),
        provider=provider,
        provider_factory=lambda config: provider,
    )
    monkeypatch.setenv("ASTRA_DELEGATE_CONCURRENCY", "2")

    cache = {}
    worker = _worker_llm(active, cache)

    assert worker is not active
    assert worker.config.max_concurrent_requests == 2
    assert worker.config.model == active.config.model
    assert _worker_llm(active, cache) is worker
    async def overlap():
        await asyncio.gather(
            active.chat([{"role": "user", "content": "root"}]),
            worker.chat([{"role": "user", "content": "child"}]),
        )

    asyncio.run(overlap())
    assert provider.peak == 2
    local = LLMClient(
        LLMConfig(base_url="http://127.0.0.1:8000", max_concurrent_requests=1),
        provider=provider,
    )
    assert _worker_llm(local, {}) is local
    lan = LLMClient(
        LLMConfig(base_url="http://192.0.2.10:8000/v1", max_concurrent_requests=1),
        provider=provider,
    )
    assert _worker_llm(lan, {}) is lan


def test_llm_chat_max_tokens_preserves_legacy_provider_protocol():
    class LegacyProvider:
        async def chat(self, messages, tools):
            del messages, tools
            return {"content": "legacy-ok"}

    class BudgetProvider:
        def __init__(self):
            self.max_tokens = None

        async def chat(self, messages, tools, *, max_tokens=None):
            del messages, tools
            self.max_tokens = max_tokens
            return {"content": "budget-ok"}

    async def scenario():
        legacy = LLMClient(LLMConfig(), provider=LegacyProvider())
        assert (
            await legacy.chat([{"role": "user", "content": "test"}], max_tokens=16_384)
        )["content"] == "legacy-ok"

        provider = BudgetProvider()
        budgeted = LLMClient(LLMConfig(), provider=provider)
        assert (
            await budgeted.chat(
                [{"role": "user", "content": "test"}], max_tokens=16_384
            )
        )["content"] == "budget-ok"
        assert provider.max_tokens == 16_384

    asyncio.run(scenario())


def test_worker_model_aliases_and_cache_are_isolated(monkeypatch):
    class Provider:
        async def chat(self, messages, tools):
            del messages, tools
            return {"content": "ok"}

    provider = Provider()
    active = LLMClient(
        LLMConfig(base_url="https://example.test", model="root-model"),
        provider=provider,
        provider_factory=lambda config: provider,
    )
    monkeypatch.setenv("ASTRA_DELEGATE_CONCURRENCY", "3")
    cache = {}

    flash = _worker_llm(active, cache, model="flash")
    assert flash.config.model == "deepseek-flash"
    assert _worker_llm(active, cache, model="FAST") is flash

    pro = _worker_llm(active, cache, model="deepseek-pro")
    assert pro is not flash
    assert pro.config.model == "deepseek-pro"

    inherited = _worker_llm(active, cache)
    assert inherited is not pro
    assert inherited.config.model == "root-model"

    local = LLMClient(
        LLMConfig(base_url="http://localhost:8000", model="loaded-local-model"),
        provider=provider,
    )
    assert _worker_llm(local, {}, model="flash") is local
    assert local.config.model == "loaded-local-model"


def test_worker_reasoning_effort_requires_supported_remote_model():
    class Provider:
        async def chat(self, messages, tools):
            del messages, tools
            return {"content": "ok"}

    provider = Provider()
    qwen = LLMClient(
        LLMConfig(base_url="https://example.test", model="qwen3.8-max"),
        provider=provider,
        provider_factory=lambda config: provider,
    )
    with pytest.raises(ValueError, match="remote DeepSeek"):
        _worker_llm(qwen, {}, reasoning_effort="low")

    local_deepseek = LLMClient(
        LLMConfig(base_url="http://127.0.0.1:8000", model="deepseek-v4-pro"),
        provider=provider,
    )
    with pytest.raises(ValueError, match="remote DeepSeek"):
        _worker_llm(local_deepseek, {}, reasoning_effort="high")

    remote_deepseek = LLMClient(
        LLMConfig(base_url="https://example.test", model="deepseek-v4-pro"),
        provider=provider,
        provider_factory=lambda config: provider,
    )
    worker = _worker_llm(remote_deepseek, {}, reasoning_effort="high")
    assert worker.config.reasoning_effort == "high"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   ", ""),
        ("flash", "deepseek-flash"),
        ("fast", "deepseek-flash"),
        ("FLASH", "deepseek-flash"),
        ("  vendor/custom-model  ", "vendor/custom-model"),
    ],
)
def test_resolve_worker_model_aliases(raw, expected):
    assert _resolve_model(raw) == expected


def test_invalid_delegate_concurrency_does_not_break_registration(monkeypatch):
    monkeypatch.setenv("ASTRA_DELEGATE_CONCURRENCY", "not-a-number")
    registry = ToolRegistry()

    register_delegate_tools(registry, llm_getter=SlowLLM)

    assert registry.get("delegate_task") is not None


def test_delegate_concurrency_accepts_five_and_caps_higher_values(monkeypatch):
    monkeypatch.setenv("ASTRA_DELEGATE_CONCURRENCY", "5")
    assert delegate._delegate_concurrency() == 5

    monkeypatch.setenv("ASTRA_DELEGATE_CONCURRENCY", "999")
    assert delegate._delegate_concurrency() == 5


def test_investigation_budget_is_safe_bounded_floor(monkeypatch):
    monkeypatch.setenv("DELEGATE_INVESTIGATE_MAX_TOKENS", "not-a-number")
    assert _investigation_token_floor() == 16_384

    monkeypatch.setenv("DELEGATE_INVESTIGATE_MAX_TOKENS", "-1")
    assert _investigation_token_floor() == 16_384

    monkeypatch.setenv("DELEGATE_INVESTIGATE_MAX_TOKENS", "999999")
    assert _investigation_token_floor() == 131_072

    monkeypatch.setenv("DELEGATE_INVESTIGATE_MAX_TOKENS", "8192")
    llm = SimpleNamespace(config=SimpleNamespace(max_tokens=32_768))
    assert _investigation_max_tokens(llm) == 32_768


def test_delegate_runs_tool_loop_and_returns_final_answer(process_manager, tmp_path):
    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = SequenceLLM([
            {
                "content": "",
                "finish_reason": "tool_calls",
                "reasoning_content": "hidden-reasoning-marker",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                },
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {"content": "investigation complete", "tool_calls": []},
        ])
        session_events = []

        def record_session_event(session_id, event):
            session_events.append((session_id, event))
            return tmp_path / "session.subagents.jsonl"

        register_delegate_tools(
            registry,
            llm_getter=lambda: llm,
            session_id_getter=lambda: "session-a",
            on_session_event=record_session_event,
        )

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "context": "repo", "timeout": 5},
            task_id="task-1",
        )
        payload = json.loads(result["output"])

        assert result["error"] == ""
        assert payload["result"] == "investigation complete"
        assert payload["turns_used"] == 2
        assert payload["max_turns"] == 12
        assert payload["turns_remaining"] == 10
        assert payload["worker_status"] == "completed"
        assert payload["error_code"] == ""
        assert payload["worker"]["status"] == "completed"
        assert payload["worker"]["worker_type"] == "explorer"
        assert payload["worker"]["turns_used"] == 2
        assert payload["worker"]["max_turns"] == 12
        assert payload["worker"]["turns_remaining"] == 10
        assert payload["session_transcript_path"].endswith("session.subagents.jsonl")
        lifecycle_events = [event[1] for event in session_events if event[1]["type"] == "lifecycle"]
        assert [event["state"] for event in lifecycle_events] == ["active", "terminal"]
        assert lifecycle_events[-1]["completion_reason"] == "reported"
        dialogue_events = [event[1] for event in session_events if event[1]["type"] != "lifecycle"]
        assert [event["type"] for event in dialogue_events] == [
            "started", "assistant", "tool", "assistant", "terminal",
        ]
        assert dialogue_events[2]["tool_name"] == "read_file"
        first_assistant = dialogue_events[1]
        assert first_assistant["finish_reason"] == "tool_calls"
        assert first_assistant["reasoning_chars"] == len("hidden-reasoning-marker")
        assert first_assistant["usage"] == {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
        }
        assert "reasoning_content" not in first_assistant
        assert session_events[-1][1]["result"] == "investigation complete"
        assert all(session_id == "session-a" for session_id, _ in session_events)
        assert process_manager.list() == []

    asyncio.run(scenario())


def test_delegate_read_advances_cursor_by_default(process_manager):
    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {"content": "报告完成", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        start = await registry.execute(
            "delegate_task",
            {"goal": "调查问题", "timeout": 5, "background": True},
        )
        pid = json.loads(start["output"])["process_id"]
        await registry.execute("delegate_poll", {"process_id": pid, "wait_ms": 3000})

        r1 = json.loads(
            (await registry.execute("delegate_read", {"process_id": pid}))["output"]
        )
        r2 = json.loads(
            (await registry.execute("delegate_read", {"process_id": pid}))["output"]
        )
        # First read starts at the beginning and reports character offsets.
        assert r1["offset"] == 0
        assert r1["next_offset"] == len(r1["content"])
        # Omitting offset continues from the cursor instead of re-reading.
        assert r2["offset"] == r1["next_offset"]
        assert r1["content"] and r1["content"] not in r2["content"]
        # An explicit 0 still reads from the start.
        r0 = json.loads(
            (await registry.execute(
                "delegate_read", {"process_id": pid, "offset": 0}
            ))["output"]
        )
        assert r0["content"].startswith(r1["content"][:20])

    asyncio.run(scenario())


def test_delegate_reports_progress_events(process_manager):
    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {"content": "investigation complete", "tool_calls": []},
        ])
        progress_events: list[tuple[dict, dict]] = []

        def record_progress(stage: str, **details):
            # registry's report() passes the full payload dict as first arg.
            progress_events.append((stage, details))

        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "timeout": 5},
            on_progress=record_progress,
        )
        payload = json.loads(result["output"])

        assert result["error"] == ""
        assert payload["worker_status"] == "completed"
        events = [event for event, _ in progress_events]
        stages = [event["stage"] for event in events]
        assert "delegate_started" in stages
        assert "tool_call" in stages
        assert "delegate_completed" in stages
        assert stages.index("delegate_started") < stages.index("tool_call")
        assert stages.index("tool_call") < stages.index("delegate_completed")
        tool_events = [e for e in events if e["stage"] == "tool_call"]
        assert tool_events[0]["message"] == "read_file"
        completed = [e for e in events if e["stage"] == "delegate_completed"]
        assert completed[0]["status"] == "completed"
        delegate_events = [
            event
            for event in events
            if event["stage"]
            in {
                "delegate_started",
                "assistant_turn",
                "tool_call",
                "delegate_completed",
                "delegate_failed",
            }
        ]
        assert all(event["max_turns"] == 12 for event in delegate_events)
        assert all(
            event["turns_remaining"]
            == max(0, event["max_turns"] - event["turns_used"])
            for event in delegate_events
        )
        by_stage = {event["stage"]: event for event in delegate_events}
        assert by_stage["delegate_started"]["turns_used"] == 0
        assert by_stage["tool_call"]["turns_used"] == 1
        assert by_stage["delegate_completed"]["turns_used"] == 2

    asyncio.run(scenario())


def test_delegate_gives_markup_and_empty_finalizers_separate_retries(process_manager):
    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        dsml = "<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name=\"read_file\"/>"
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {"content": dsml, "tool_calls": []},
            {"content": "", "finish_reason": "length", "tool_calls": []},
            {"content": "Conclusion after both recovery modes.", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "max_turns": 2, "timeout": 10},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert payload["result"] == "Conclusion after both recovery modes."
        assert payload["turns_used"] == 4

    asyncio.run(scenario())


def test_delegate_reserves_last_turn_for_tool_free_finalization(process_manager):
    class RecordingLLM(SequenceLLM):
        def __init__(self, responses):
            super().__init__(responses)
            self.tool_sets = []
            self.requests = []

        async def chat(self, **kwargs):
            self.tool_sets.append(kwargs.get("tools"))
            self.requests.append(kwargs)
            return await super().chat(**kwargs)

    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = RecordingLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {"content": "best report from existing evidence", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "max_turns": 2, "timeout": 10},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert payload["result"] == "best report from existing evidence"
        assert llm.tool_sets[0]
        assert llm.tool_sets[1] is None
        final_request = llm.requests[1]
        assert final_request["tool_choice"] == "none"
        assert [message["role"] for message in final_request["messages"]] == [
            "system",
            "user",
        ]
        assert "read:README.md" in final_request["messages"][1]["content"]
        assert not any(
            message["role"] in {"assistant", "tool"}
            for message in final_request["messages"]
        )

    asyncio.run(scenario())


def test_finalizer_prioritizes_latest_structured_team_assignment():
    spec = WorkerSpec(
        worker_type="worker",
        goal="Report readiness, then wait for work",
        context="standing role",
        keep_alive=True,
    )

    messages = delegate._finalization_messages(
        spec,
        ["[execute_shell]\n6 passed"],
        [],
        latest_assignment="Implement ellipsize with RED then GREEN",
    )

    assert "Standing goal:\nReport readiness, then wait for work" in messages[1]["content"]
    assert (
        "Latest assigned task:\nImplement ellipsize with RED then GREEN"
        in messages[1]["content"]
    )
    assert "report the latest assigned task" in messages[0]["content"].lower()


def test_partial_finalizer_labels_latest_assignment_instead_of_readiness_goal():
    report = delegate._partial_finalization_report(
        "Report readiness, then wait",
        ["[execute_shell]\n6 passed"],
        [],
        latest_assignment="Implement ellipsize with RED then GREEN",
    )

    assert "Task: Implement ellipsize with RED then GREEN" in report
    assert "Standing goal: Report readiness, then wait" in report


def test_delegate_uses_limited_clean_finalizer_when_available(process_manager):
    class LimitedFinalizerLLM:
        def __init__(self):
            self.finalizer_request = None
            self.initial_system = ""

        async def chat(self, **kwargs):
            self.initial_system = kwargs["messages"][0]["content"]
            return {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            }

        async def chat_limited(self, **kwargs):
            self.finalizer_request = kwargs
            return {"content": "bounded final report", "tool_calls": []}

    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = LimitedFinalizerLLM()
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "max_turns": 2, "timeout": 10},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert payload["result"] == "bounded final report"
        assert "Native workspace root:" in llm.initial_system
        request = llm.finalizer_request
        assert request["tools"] is None
        assert request["max_tokens"] == 2048
        assert request["disable_thinking"] is True
        assert request["reasoning_effort"] == "low"
        assert [message["role"] for message in request["messages"]] == [
            "system",
            "user",
        ]

    asyncio.run(scenario())


def test_delegate_recovers_reasoning_only_investigation_with_clean_finalizer(
    process_manager,
):
    class RecoveryLLM:
        def __init__(self):
            self.config = SimpleNamespace(max_tokens=4096)
            self.investigation_request = None
            self.finalizer_request = None

        async def chat(self, **kwargs):
            self.investigation_request = kwargs
            return {
                "content": "",
                "reasoning_content": "hidden reasoning consumed the budget",
                "finish_reason": "length",
                "usage": {"completion_tokens": 16_384},
                "tool_calls": [],
            }

        async def chat_limited(self, **kwargs):
            self.finalizer_request = kwargs
            return {"content": "Recovered final report.", "tool_calls": []}

    async def scenario():
        registry = ToolRegistry()
        llm = RecoveryLLM()
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "reason deeply, then report", "max_turns": 4, "timeout": 10},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert payload["result"] == "Recovered final report."
        assert payload["turns_used"] == 2
        assert llm.investigation_request["max_tokens"] == 16_384
        assert llm.finalizer_request["reasoning_effort"] == "low"
        assert llm.finalizer_request["disable_thinking"] is True

    asyncio.run(scenario())


def test_worker_mode_executes_workspace_edit_and_reports(process_manager):
    async def scenario():
        edits = []

        async def edit(path=""):
            edits.append(path)
            return f"edited:{path}"

        registry = ToolRegistry()
        registry.register(ToolDef(
            name="edit_file",
            description="edit",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
            fn=edit,
            group="files",
            risk="write",
            approval="never",
        ))
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "edit-1",
                    "name": "edit_file",
                    "arguments": json.dumps({"path": "agent/example.py"}),
                }],
            },
            {"content": "change verified", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {
                "goal": "make focused change",
                "mode": "worker",
                "max_turns": 2,
                "timeout": 10,
            },
            task_id="workspace-task",
        )
        payload = json.loads(result["output"])

        assert result["error"] == ""
        assert edits == ["agent/example.py"]
        assert payload["result"] == "change verified"
        assert payload["worker"]["worker_type"] == "worker"

    asyncio.run(scenario())


def test_worker_batches_known_edit_and_verification_in_model_order(process_manager):
    class BatchingWorkerLLM:
        def __init__(self):
            self.calls = 0
            self.system_prompts = []

        async def chat(self, **kwargs):
            self.calls += 1
            self.system_prompts.append(kwargs["messages"][0]["content"])
            if self.calls == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "write-first",
                            "name": "write_file",
                            "arguments": json.dumps({"path": "sample.py"}),
                        },
                        {
                            "id": "verify-second",
                            "name": "execute_python",
                            "arguments": json.dumps({"code": "verify sample.py"}),
                        },
                    ],
                }
            return {"content": "edit and verification complete", "tool_calls": []}

    async def scenario():
        events = []

        async def write(path=""):
            events.append(("write", path))
            return "written"

        async def verify(code=""):
            assert events == [("write", "sample.py")]
            events.append(("verify", code))
            return "passed"

        registry = ToolRegistry()
        for name, fn, argument in (
            ("write_file", write, "path"),
            ("execute_python", verify, "code"),
        ):
            registry.register(ToolDef(
                name=name,
                description=name,
                parameters={
                    "type": "object",
                    "properties": {argument: {"type": "string"}},
                    "required": [argument],
                },
                fn=fn,
                group="files" if name == "write_file" else "code",
                risk="write" if name == "write_file" else "execute",
                approval="never",
            ))
        llm = BatchingWorkerLLM()
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {
                "goal": "write and verify sample",
                "mode": "worker",
                "max_turns": 2,
                "timeout": 10,
            },
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert payload["result"] == "edit and verification complete"
        assert events == [
            ("write", "sample.py"),
            ("verify", "verify sample.py"),
        ]
        assert "executed serially in the exact order" in llm.system_prompts[0]
        assert "write implementation, write tests" in llm.system_prompts[0]
        assert "final report writer" in llm.system_prompts[-1]

    asyncio.run(scenario())


def test_worker_mode_edits_real_workspace_file(process_manager, tmp_path):
    async def scenario():
        target = tmp_path / "sample.py"
        target.write_text("VALUE = 'old'\n", encoding="utf-8")
        registry = ToolRegistry()
        register_file_tools(registry, workdir=str(tmp_path))
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "edit-real-file",
                    "name": "edit_file",
                    "arguments": json.dumps({
                        "path": "sample.py",
                        "old": "VALUE = 'old'",
                        "new": "VALUE = 'new'",
                    }),
                }],
            },
            {"content": "real file changed", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {
                "goal": "update sample value",
                "mode": "worker",
                "max_turns": 2,
                "timeout": 10,
            },
            task_id="real-edit-task",
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert target.read_text(encoding="utf-8") == "VALUE = 'new'\n"

    asyncio.run(scenario())


def test_worker_mode_preserves_main_registry_approval_boundary(process_manager):
    async def scenario():
        approvals = []
        grants = []
        executions = []

        async def edit(path=""):
            executions.append(path)
            return f"edited:{path}"

        def permission_check(args):
            return {
                "kind": "filesystem",
                "target": args["path"],
                "operation": "Edit file",
                "access": "write",
                "reason": "test approval boundary",
            }

        def permission_grant(args, request, decision):
            grants.append((args["path"], request["target"], decision))
            return None

        async def approve(request):
            approvals.append(request)
            return "once"

        registry = ToolRegistry()
        registry.register(ToolDef(
            name="edit_file",
            description="edit",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            fn=edit,
            group="files",
            risk="write",
            approval="on_risk",
            permission_check=permission_check,
            permission_grant=permission_grant,
        ))
        registry.set_approval_handler(approve)
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "edit-approved",
                    "name": "edit_file",
                    "arguments": json.dumps({"path": "agent/example.py"}),
                }],
            },
            {"content": "approved change complete", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {
                "goal": "make approved change",
                "mode": "worker",
                "max_turns": 2,
                "timeout": 10,
            },
            task_id="approval-task",
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert executions == ["agent/example.py"]
        assert len(approvals) == 1
        assert approvals[0]["tool_name"] == "edit_file"
        assert grants == [("agent/example.py", "agent/example.py", "once")]

    asyncio.run(scenario())


def test_structured_tool_call_during_finalization_retries_as_plain_report(process_manager):
    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {"content": "", "tool_calls": [{
                "id": "forbidden-final-call",
                "name": "read_file",
                "arguments": "{}",
            }]},
            {"content": "Conclusion: README inspected.", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "max_turns": 2, "timeout": 10},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert payload["error_code"] == ""
        assert payload["result"] == "Conclusion: README inspected."
        assert payload["turns_used"] == 3

    asyncio.run(scenario())


def test_output_limited_final_report_retries_as_dense_complete_report(process_manager):
    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {
                "content": "Long report that stops in the middle of",
                "tool_calls": [],
                "finish_reason": "length",
            },
            {
                "content": "Conclusion: README inspected; evidence is bounded and complete.",
                "tool_calls": [],
                "finish_reason": "stop",
            },
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "max_turns": 2, "timeout": 10},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert payload["result"].endswith("bounded and complete.")
        assert payload["turns_used"] == 3

    asyncio.run(scenario())


def test_delegate_retries_unparsed_dsml_finalization(process_manager):
    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {
                "content": "<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name=\"read_file\"/>",
                "tool_calls": [],
            },
            {"content": "Conclusion: README inspected.", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "max_turns": 2, "timeout": 10},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert payload["result"] == "Conclusion: README inspected."
        assert payload["turns_used"] == 3

    asyncio.run(scenario())


def test_unparsed_tool_markup_detection_does_not_reject_incidental_mentions():
    dsml = (
        "<｜｜DSML｜｜tool_calls>"
        "<｜｜DSML｜｜invoke name=\"read_file\"/>"
        "</｜｜DSML｜｜tool_calls>"
    )

    assert _is_unparsed_tool_markup(dsml)
    assert _is_unparsed_tool_markup("Still investigating.\n" + dsml)
    assert not _is_unparsed_tool_markup(
        "The provider marker `<｜｜DSML｜｜tool_calls>` should be treated as data."
    )


def test_delegate_returns_partial_report_after_repeated_unparsed_dsml(process_manager):
    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        dsml = "<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name=\"read_file\"/>"
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {"content": dsml, "tool_calls": []},
            {"content": dsml, "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "max_turns": 2, "timeout": 10},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "partial"
        assert payload["worker"]["status"] == "partial"
        assert payload["error_code"] == ""
        assert payload["partial"] is True
        assert "repeatedly emitted tool-call markup" in payload["warning"]
        assert "Partial report" in payload["result"]
        assert "read:README.md" in payload["result"]
        assert dsml not in payload["result"]

    asyncio.run(scenario())


def test_delegate_retries_empty_finalization_then_completes(process_manager):
    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {
                "content": "",
                "reasoning_content": "thought without answer",
                "finish_reason": "length",
                "usage": {"completion_tokens": 8192},
                "tool_calls": [],
            },
            {"content": "Conclusion: README inspected.", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "max_turns": 2, "timeout": 10},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert payload["result"] == "Conclusion: README inspected."
        assert payload["turns_used"] == 3

    asyncio.run(scenario())


def test_delegate_returns_partial_report_after_repeated_empty_finalization(process_manager):
    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [{
                    "id": "call-1",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "README.md"}),
                }],
            },
            {"content": "", "finish_reason": "length", "tool_calls": []},
            {"content": "", "finish_reason": "length", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "inspect", "max_turns": 2, "timeout": 10},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "partial"
        assert payload["worker"]["status"] == "partial"
        assert payload["error_code"] == ""
        assert payload["partial"] is True
        assert "no visible report" in payload["warning"]
        assert "Partial report" in payload["result"]
        assert "read:README.md" in payload["result"]
        assert "no visible final report" in payload["result"]

    asyncio.run(scenario())


def test_delegate_defaults_to_twelve_bounded_turns(process_manager):
    registry = ToolRegistry()
    register_delegate_tools(registry, llm_getter=lambda: SequenceLLM([]))

    default_spec = WorkerSpec(worker_type="explorer", goal="inspect", context="")
    assert default_spec.max_turns == 12
    assert default_spec.model == ""
    assert default_spec.keep_alive is False
    assert default_spec.workspace_root == ""
    assert default_spec.public()["keep_alive"] is False
    assert default_spec.public()["model"] == ""
    assert default_spec.public()["reasoning_effort"] == ""
    assert default_spec.public()["workspace_root"] == ""
    assert WorkerSpec(
        worker_type="explorer",
        goal="inspect",
        context="",
        model="  custom-model  ",
    ).public()["model"] == "custom-model"
    assert WorkerSpec(
        worker_type="explorer",
        goal="inspect",
        context="",
        reasoning_effort=" HIGH ",
    ).public()["reasoning_effort"] == "high"
    with pytest.raises(ValueError, match="reasoning_effort"):
        WorkerSpec(
            worker_type="explorer",
            goal="inspect",
            context="",
            reasoning_effort="hihg",
        )
    schema = registry.get("delegate_task").parameters
    assert schema["properties"]["max_turns"]["default"] == 12
    assert schema["properties"]["max_turns"]["maximum"] == 50
    assert schema["properties"]["timeout"]["maximum"] == 1800
    assert schema["properties"]["model"]["default"] == ""
    item_schema = schema["properties"]["tasks"]["items"]["properties"]
    assert "model" in item_schema
    assert item_schema["max_turns"]["maximum"] == 50
    assert item_schema["timeout"]["maximum"] == 1800
    assert registry.get("team_spawn").parameters["properties"]["model"]["default"] == ""
    workspace_schema = registry.get("team_spawn").parameters["properties"][
        "workspace_root"
    ]
    assert workspace_schema["type"] == "string"
    assert workspace_schema["default"] == ""


def test_hard_timeout_preserves_evidence_collected_before_slow_llm(process_manager):
    class EvidenceThenSlowLLM:
        def __init__(self):
            self.calls = 0

        async def chat(self, **kwargs):
            del kwargs
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": "",
                    "tool_calls": [{
                        "id": "evidence-call",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "README.md"}),
                    }],
                }
            await asyncio.sleep(5)
            return {"content": "too late", "tool_calls": []}

    async def scenario():
        registry = ToolRegistry()
        _register_read_tool(registry)
        llm = EvidenceThenSlowLLM()
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"goal": "collect then stall", "timeout": 1},
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "timed_out"
        assert payload["error_code"] == "timed_out"
        assert payload["turns_used"] == 1
        assert "read:README.md" in payload["result"]
        assert not payload["result"].startswith("(no result")

    asyncio.run(scenario())


def test_worker_approval_wait_is_cut_off_before_finalization(process_manager):
    class WorkerLLM:
        def __init__(self):
            self.calls = 0

        async def chat(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": "",
                    "tool_calls": [{
                        "id": "shell-call",
                        "name": "execute_shell",
                        "arguments": json.dumps({"command": "test-command"}),
                    }],
                }
            assert kwargs.get("tools") is None
            return {
                "content": "Blocked before execution; no files changed.",
                "tool_calls": [],
            }

    async def scenario():
        approval_cancelled = asyncio.Event()
        executions = []

        async def execute(command=""):
            executions.append(command)
            return "executed"

        async def wait_for_approval(request):
            del request
            try:
                await asyncio.sleep(5)
                return "once"
            except asyncio.CancelledError:
                approval_cancelled.set()
                raise

        registry = ToolRegistry()
        registry.register(ToolDef(
            name="execute_shell",
            description="execute",
            parameters={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            fn=execute,
            group="code",
            risk="execute",
            approval="on_risk",
            permission_check=lambda args: {
                "kind": "execution",
                "target": args["command"],
                "operation": "Execute command",
                "access": "execute",
                "reason": "test approval wait",
            },
        ))
        registry.set_approval_handler(wait_for_approval)
        register_delegate_tools(registry, llm_getter=lambda: WorkerLLM())

        result = await registry.execute(
            "delegate_task",
            {
                "goal": "run only if approved",
                "mode": "worker",
                "tools": ["execute_shell"],
                "timeout": 1,
            },
        )
        payload = json.loads(result["output"])

        assert payload["worker_status"] == "completed"
        assert payload["result"] == "Blocked before execution; no files changed."
        assert approval_cancelled.is_set()
        assert executions == []

    asyncio.run(scenario())


def test_delegate_batch_runs_two_workers_concurrently(process_manager):
    class ConcurrentLLM:
        def __init__(self):
            self.active = 0
            self.peak = 0

        async def chat(self, **kwargs):
            del kwargs
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.05)
            self.active -= 1
            return {"content": "done", "tool_calls": []}

    async def scenario():
        registry = ToolRegistry()
        llm = ConcurrentLLM()
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {"tasks": [{"goal": "first"}, {"goal": "second"}]},
            task_id="task-batch",
        )
        payload = json.loads(result["output"])

        assert result["error"] == ""
        assert payload["count"] == 2
        assert [item["result"] for item in payload["results"]] == ["done", "done"]
        assert llm.peak == 2

    asyncio.run(scenario())


def test_delegate_batch_item_model_overrides_top_level_default(process_manager):
    created_models = []

    class ModelProvider:
        def __init__(self, model):
            self.model = model

        async def chat(self, messages, tools):
            del messages, tools
            return {"content": self.model, "tool_calls": []}

    def provider_factory(config):
        created_models.append(config.model)
        return ModelProvider(config.model)

    async def scenario():
        registry = ToolRegistry()
        active = LLMClient(
            LLMConfig(base_url="https://example.test", model="root-model"),
            provider=ModelProvider("root-model"),
            provider_factory=provider_factory,
        )
        register_delegate_tools(registry, llm_getter=lambda: active)

        result = await registry.execute(
            "delegate_task",
            {
                "model": "research-pro",
                "tasks": [
                    {"goal": "inherit top-level model", "max_turns": 1},
                    {"goal": "use fast alias", "model": "flash", "max_turns": 1},
                ],
            },
            task_id="task-model-batch",
        )
        payload = json.loads(result["output"])

        assert result["error"] == ""
        assert [item["result"] for item in payload["results"]] == [
            "research-pro",
            "deepseek-flash",
        ]
        assert created_models == ["research-pro", "deepseek-flash"]

    asyncio.run(scenario())


def test_delegate_batch_validates_every_item_before_start(process_manager):
    async def scenario():
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: SequenceLLM([
                {"content": "should not run", "tool_calls": []},
            ]),
        )

        result = await registry.execute(
            "delegate_task",
            {"tasks": [{"goal": "valid"}, {"goal": ""}]},
            task_id="task-batch",
        )

        assert "non-empty goal" in result["error"]
        assert process_manager.list() == []

    asyncio.run(scenario())


def test_delegate_queue_time_counts_toward_total_timeout(
    process_manager, monkeypatch
):
    class VerySlowLLM:
        async def chat(self, **kwargs):
            del kwargs
            await asyncio.sleep(1)
            return {"content": "late", "tool_calls": []}

    async def scenario():
        monkeypatch.setenv("ASTRA_DELEGATE_CONCURRENCY", "1")
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=VerySlowLLM)
        loop = asyncio.get_running_loop()
        started = loop.time()

        result = await registry.execute(
            "delegate_task",
            {
                "tasks": [
                    {"goal": "first", "timeout": 1},
                    {"goal": "queued", "timeout": 1},
                ]
            },
        )
        elapsed = loop.time() - started
        payload = json.loads(result["output"])

        assert elapsed < 1.5
        assert all(item["worker_status"] == "timed_out" for item in payload["results"])

    asyncio.run(scenario())


def test_delegate_batch_runs_five_children_concurrently(process_manager, monkeypatch):
    class ConcurrentLLM:
        def __init__(self):
            self.active = 0
            self.peak = 0

        async def chat(self, **kwargs):
            del kwargs
            self.active += 1
            self.peak = max(self.peak, self.active)
            await asyncio.sleep(0.05)
            self.active -= 1
            return {"content": "report", "tool_calls": []}

    async def scenario():
        monkeypatch.setenv("ASTRA_DELEGATE_CONCURRENCY", "5")
        monkeypatch.setenv("ASTRA_DELEGATE_OWNER_CONCURRENCY", "5")
        registry = ToolRegistry()
        llm = ConcurrentLLM()
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute(
            "delegate_task",
            {
                "tasks": [{"goal": f"task-{index}", "max_turns": 1} for index in range(5)],
                "timeout": 5,
            },
        )
        payload = json.loads(result["output"])

        assert payload["count"] == 5
        assert all(item["worker_status"] == "completed" for item in payload["results"])
        assert llm.peak == 5

    asyncio.run(scenario())


def test_delegate_child_executes_read_calls_concurrently(process_manager):
    async def scenario():
        active = 0
        peak = 0

        async def read(path=""):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.05)
            active -= 1
            return f"read:{path}"

        registry = ToolRegistry()
        registry.register(ToolDef(
            name="read_file",
            description="read",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            fn=read,
            group="files",
            risk="read",
        ))
        llm = SequenceLLM([
            {
                "content": "",
                "tool_calls": [
                    {"id": "a", "name": "read_file", "arguments": '{"path":"a"}'},
                    {"id": "b", "name": "read_file", "arguments": '{"path":"b"}'},
                ],
            },
            {"content": "complete", "tool_calls": []},
        ])
        register_delegate_tools(registry, llm_getter=lambda: llm)

        result = await registry.execute("delegate_task", {"goal": "read both"})

        assert result["error"] == ""
        assert peak == 2

    asyncio.run(scenario())


def test_delegate_schema_and_description_encourage_bounded_parallelism():
    registry = ToolRegistry()
    register_delegate_tools(registry, llm_getter=SlowLLM)
    tool = registry.get("delegate_task")

    assert tool.parameters["required"] == []
    assert tool.parameters["properties"]["tasks"]["maxItems"] == 5
    assert tool.parameters["properties"]["fork_turns"]["default"] == "2"
    assert tool.parameters["properties"]["mode"]["default"] == "explorer"
    assert tool.parameters["properties"]["mode"]["enum"] == ["explorer", "worker"]
    assert "Proactively" in tool.description
    assert "disjoint files" in tool.description
    assert tool.max_calls_per_turn == 5


def test_delegate_cancel_awaits_and_reaches_cancelled(process_manager):
    async def scenario():
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=SlowLLM)

        started = await registry.execute(
            "delegate_task",
            {
                "goal": "wait",
                "context": "repo",
                "background": True,
                "timeout": 30,
            },
            task_id="task-1",
        )
        process_id = json.loads(started["output"])["process_id"]

        cancelled = await registry.execute(
            "delegate_cancel",
            {"process_id": process_id},
            task_id="task-1",
        )
        payload = json.loads(cancelled["output"])

        assert payload["status"] == "cancelled"
        assert payload["worker"]["status"] == "cancelled"
        assert process_manager.status(process_manager.get(process_id)) == "cancelled"

    asyncio.run(scenario())


def test_delegate_read_returns_json_incrementally(process_manager):
    async def scenario():
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=SlowLLM)

        started = await registry.execute(
            "delegate_task",
            {
                "goal": "wait",
                "context": "repo",
                "background": True,
                "timeout": 30,
            },
        )
        process_id = json.loads(started["output"])["process_id"]
        await asyncio.sleep(0)

        read_result = await registry.execute(
            "delegate_read",
            {"process_id": process_id, "offset": 0},
        )
        payload = json.loads(read_result["output"])

        assert "[subagent] starting: wait" in payload["content"]
        assert payload["next_offset"] > 0
        assert payload["worker"]["run_id"] == process_id
        assert payload["worker"]["status"] == "running"
        await registry.execute("delegate_cancel", {"process_id": process_id})

    asyncio.run(scenario())


def test_delegate_timeout_covers_llm_request(process_manager):
    async def scenario():
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=SlowLLM)

        result = await registry.execute(
            "delegate_task",
            {"goal": "wait", "context": "repo", "timeout": 1},
        )
        payload = json.loads(result["output"])

        assert "exceeded 1s" in payload["error"]
        assert payload["result"].startswith("(no result")
        assert payload["worker_status"] == "timed_out"
        assert payload["error_code"] == "timed_out"
        assert payload["worker"]["status"] == "timed_out"
        assert process_manager.list() == []

    asyncio.run(scenario())


def test_delegate_rejects_unavailable_tools(process_manager):
    async def scenario():
        registry = ToolRegistry()
        register_delegate_tools(
            registry,
            llm_getter=lambda: SequenceLLM([
                {"content": "unused", "tool_calls": []},
            ]),
        )

        result = await registry.execute(
            "delegate_task",
            {
                "goal": "inspect",
                "context": "repo",
                "tools": ["execute_shell"],
            },
        )

        assert "not available to subagents" in result["error"]
        assert process_manager.list() == []

    asyncio.run(scenario())


def test_parent_cancellation_cleans_unexposed_process(process_manager):
    async def scenario():
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=SlowLLM)
        delegate_task = registry.get("delegate_task")

        running = asyncio.create_task(delegate_task.fn(
            goal="wait",
            context="repo",
            timeout=30,
        ))
        await asyncio.sleep(0)
        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await running

        assert not process_manager._processes

    asyncio.run(scenario())


def test_delegate_task_uses_its_own_deadline():
    registry = ToolRegistry()
    register_delegate_tools(registry, llm_getter=SlowLLM)

    assert registry.get("delegate_task").timeout is None


def test_background_worker_manifest_has_public_spec_not_context(process_manager):
    async def scenario():
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=SlowLLM)

        started = await registry.execute(
            "delegate_task",
            {
                "goal": "inspect safely",
                "context": "private-context-marker",
                "background": True,
                "timeout": 30,
            },
            task_id="task-42",
        )
        payload = json.loads(started["output"])
        process = process_manager.get(payload["process_id"])
        manifest = json.loads(process.manifest_path.read_text(encoding="utf-8"))

        assert payload["worker"]["status"] == "running"
        assert payload["worker"]["task_id"] == "task-42"
        assert manifest["metadata"]["worker_spec"]["context_chars"] == len(
            _context_snapshot([], explicit_context="private-context-marker")
        )
        assert "private-context-marker" not in process.manifest_path.read_text(
            encoding="utf-8"
        )

        listed = await registry.execute("delegate_list", {}, task_id="task-42")
        listed_payload = json.loads(listed["output"])
        assert listed_payload[0]["worker"]["run_id"] == payload["process_id"]
        assert listed_payload[0]["worker"]["worker_type"] == "explorer"

        await registry.execute(
            "delegate_cancel",
            {"process_id": payload["process_id"]},
            task_id="task-42",
        )

    asyncio.run(scenario())


def test_delegate_controls_reject_cross_task_access(process_manager):
    async def scenario():
        registry = ToolRegistry()
        register_delegate_tools(registry, llm_getter=SlowLLM)
        started = await registry.execute(
            "delegate_task",
            {"goal": "private", "background": True, "timeout": 30},
            task_id="owner",
        )
        process_id = json.loads(started["output"])["process_id"]

        denied = await registry.execute(
            "delegate_read", {"process_id": process_id}, task_id="other"
        )
        hidden = await registry.execute("delegate_list", {}, task_id="other")

        assert "belongs to another task" in denied["error"]
        assert json.loads(hidden["output"]) == []
        await registry.execute(
            "delegate_cancel", {"process_id": process_id}, task_id="owner"
        )

    asyncio.run(scenario())


def test_background_completion_is_delivered_once_through_mailbox(process_manager, tmp_path):
    class BriefLLM:
        async def chat(self, **kwargs):
            del kwargs
            await asyncio.sleep(0.02)
            return {"content": "evidence from worker", "tool_calls": []}

    async def scenario():
        registry = ToolRegistry()
        mailbox = register_delegate_tools(
            registry,
            llm_getter=BriefLLM,
            session_id_getter=lambda: "session-mail",
            on_session_event=lambda _session_id, _event: (
                tmp_path / "session-mail.subagents.jsonl"
            ),
        )
        started = await registry.execute(
            "delegate_task",
            {"goal": "research branch", "background": True, "timeout": 5},
            task_id="mail-task",
        )
        process_id = json.loads(started["output"])["process_id"]

        envelopes = await mailbox.wait_and_drain("mail-task", timeout=1)

        assert len(envelopes) == 1
        assert "Message Type: FINAL_ANSWER" in envelopes[0]
        assert "research branch" in envelopes[0]
        assert "evidence from worker" in envelopes[0]
        assert process_id in envelopes[0]
        assert "Session transcript:" in envelopes[0]
        assert "session-mail.subagents.jsonl" in envelopes[0]
        assert mailbox.drain("mail-task") == []
        assert not mailbox.has_running("mail-task")

    asyncio.run(scenario())


@pytest.mark.parametrize("response_delay", [0.02, 0.1])
def test_background_completion_follows_session_across_parent_tasks(process_manager, response_delay):
    class BriefLLM:
        async def chat(self, **kwargs):
            del kwargs
            await asyncio.sleep(response_delay)
            return {"content": "cross-turn evidence", "tool_calls": []}

    async def scenario():
        registry = ToolRegistry()
        mailbox = register_delegate_tools(
            registry,
            llm_getter=BriefLLM,
            session_id_getter=lambda: "session-a",
        )
        started = await registry.execute(
            "delegate_task",
            {"goal": "finish after parent", "background": True, "timeout": 5},
            task_id="old-parent-task",
        )
        assert not started["error"], started
        process = process_manager.get(json.loads(started["output"])["process_id"])
        # Wait without delegate_poll, which acknowledges/consumes the mailbox
        # result. A fixed sleep does not guarantee completion on a busy runner.
        assert await process_manager.wait(process, 5000)
        assert process_manager.status(process) == "completed"

        assert mailbox.drain_for("another-task", "session-b") == []
        envelopes = mailbox.drain_for("new-parent-task", "session-a")

        assert len(envelopes) == 1
        assert "cross-turn evidence" in envelopes[0]
        assert mailbox.drain_for("another-task", "session-b") == []
        assert mailbox.drain_for("another-task", "session-a") == []

    asyncio.run(scenario())


def test_mailbox_cancel_task_cascades_to_background_worker(process_manager):
    async def scenario():
        registry = ToolRegistry()
        mailbox = register_delegate_tools(registry, llm_getter=SlowLLM)
        started = await registry.execute(
            "delegate_task",
            {"goal": "long branch", "background": True, "timeout": 30},
            task_id="cancel-owner",
        )
        process_id = json.loads(started["output"])["process_id"]

        await mailbox.cancel_task("cancel-owner")

        assert process_manager.status(process_manager.get(process_id)) == "cancelled"
        assert not mailbox.has_running("cancel-owner")
        assert mailbox.drain("cancel-owner") == []

    asyncio.run(scenario())


def test_manual_terminal_poll_acknowledges_mailbox_result(process_manager):
    class BriefLLM:
        async def chat(self, **kwargs):
            del kwargs
            await asyncio.sleep(0.02)
            return {"content": "manually collected", "tool_calls": []}

    async def scenario():
        registry = ToolRegistry()
        mailbox = register_delegate_tools(registry, llm_getter=BriefLLM)
        started = await registry.execute(
            "delegate_task",
            {"goal": "manual branch", "background": True, "timeout": 5},
            task_id="manual-task",
        )
        process_id = json.loads(started["output"])["process_id"]

        polled = await registry.execute(
            "delegate_poll",
            {"process_id": process_id, "wait_ms": 1000},
            task_id="manual-task",
        )

        assert json.loads(polled["output"])["status"] == "completed"
        assert mailbox.drain("manual-task") == []

    asyncio.run(scenario())
