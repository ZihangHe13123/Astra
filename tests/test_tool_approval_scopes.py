import asyncio
import hashlib
import sys
from dataclasses import dataclass
from typing import ClassVar

import pytest

from agent.runtime.browser_session import BackendCapabilities, BrowserSessionManager
from agent.runtime.hooks import HookRegistry
from agent.runtime.mcp import MCPManager
from agent.runtime.metrics import runtime_metrics
from agent.runtime.tools.browser import register_browser_tools
from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.files import FilesystemPolicy, register_file_tools
from agent.runtime.tools.policy import PolicyRule, ToolPolicy
from agent.runtime.tools.registry import (
    _MAX_APPROVAL_JUSTIFICATION_CHARS,
    ToolDef,
    ToolRegistry,
    _normalize_approval_justification,
    approval_justification_schema,
)
from agent.runtime.tools.web import register_web_tools
from agent.sandbox.docker import DockerSandbox


def run(coro):
    return asyncio.run(coro)


def test_approval_justification_schema_requests_declarative_explanation():
    description = approval_justification_schema()["reason"]["description"]
    description_lower = description.lower()

    assert "why this exact operation is needed" in description
    assert "explanation" in description_lower
    assert "concise" in description_lower
    assert "declarative" in description_lower
    assert "yes/no" not in description_lower
    assert "approval question" not in description_lower
    assert "ending in '?'" not in description
    assert "ending in '？'" not in description


def test_host_code_tool_descriptions_request_declarative_explanations(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

    registry = ToolRegistry()
    register_code_tools(registry, HostSandbox())
    schemas = {
        item["function"]["name"]: item["function"]
        for item in registry.to_openai_tools(names={"execute_python", "execute_shell"})
    }

    for name in ("execute_python", "execute_shell"):
        description = schemas[name]["description"].lower()
        assert "concise user-facing explanation" in description
        assert "why this exact operation is needed" in description
        assert "yes/no" not in description
        assert "approval question" not in description


def test_approval_justification_normalization_keeps_legacy_questions_safe():
    legacy_question = "是否允许我运行这条命令？\n用于验证现有行为。\x00\x1f"

    normalized, error = _normalize_approval_justification(legacy_question)

    assert error is None
    assert normalized == "是否允许我运行这条命令？ 用于验证现有行为。"
    assert all(char == "\t" or ord(char) >= 32 for char in normalized)

    bounded, error = _normalize_approval_justification(
        "x" * (_MAX_APPROVAL_JUSTIFICATION_CHARS + 1)
    )
    assert error is None
    assert len(bounded) == _MAX_APPROVAL_JUSTIFICATION_CHARS


def test_approval_audit_records_asked_and_decided_without_model_payload():
    audits = []
    registry = ToolRegistry(policy=ToolPolicy(mode="locked"))
    registry.set_approval_audit_handler(audits.append)
    registry.set_approval_handler(lambda request: asyncio.sleep(0, result="once"))
    registry.register(ToolDef(
        name="dangerous_action",
        description="requires approval",
        parameters={"type": "object", "properties": {"secret": {"type": "string"}}},
        fn=lambda secret: f"used {len(secret)} chars",
        risk="execute",
    ))

    result = run(registry.execute(
        "dangerous_action", {"secret": "do-not-log"}, call_id="call-approval",
    ))

    assert result["error"] == ""
    assert [event["type"] for event in audits] == ["approval_asked", "approval_decided"]
    assert audits[0]["request_id"] == audits[1]["request_id"]
    assert audits[1]["decision"] == "once"
    assert "do-not-log" not in str(audits)


def test_runtime_event_hook_is_public_bounded_and_redacts_private_computer_fields():
    hooks = HookRegistry()
    observed = []
    hooks.on_runtime_event(observed.append)

    hooks.dispatch_runtime_event({
        "type": "foreground_takeover_begin",
        "application": "WPS Office" + "x" * 500,
        "action_classes": ["scroll", "click", "unknown-private-class"],
        "window_title": "/Users/private/report.docx",
        "path": "/Users/private/report.docx",
        "pid": 4242,
        "app_ref": "private-app-ref",
        "snapshot_id": "private-snapshot",
        "plan_ref": "private-plan-token",
        "takeover_ref": "private-takeover-token",
        "digest": "private-digest",
        "text": "DO-NOT-RETAIN-TEXT",
        "screenshot": "base64-private",
        "ax_tree": {"value": "private"},
    })

    assert observed == [{
        "type": "foreground_takeover_begin",
        "application": "WPS Office" + "x" * 150,
        "action_classes": ["scroll", "click"],
    }]
    assert hooks.counts["runtime_event"] == 1

    hooks.dispatch_runtime_event({
        "type": "foreground_takeover_end",
        "application": "/Users/private/app-label",
        "action_classes": ["text"],
    })
    assert observed[-1] == {
        "type": "foreground_takeover_end",
        "action_classes": ["text"],
    }


def test_missing_approval_ui_still_closes_audit_pair_as_unavailable():
    audits = []
    registry = ToolRegistry(policy=ToolPolicy(mode="locked"))
    registry.set_approval_audit_handler(audits.append)
    registry.register(ToolDef(
        name="needs_ui",
        description="requires approval",
        parameters={"type": "object", "properties": {}},
        fn=lambda: "not run",
        risk="execute",
    ))

    result = run(registry.execute("needs_ui", {}))

    assert result["error_type"] == "approval_required"
    assert [event["type"] for event in audits] == ["approval_asked", "approval_decided"]
    assert audits[-1]["decision"] == "unavailable"


def test_host_shell_denial_once_and_session_scope_pause_the_original_call(tmp_path):
    runtime_metrics.reset()
    class HostSandbox:
        workdir = str(tmp_path)

        def __init__(self):
            self.commands = []

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            self.commands.append((command, environment))
            on_output("stdout", f"ran:{command}")
            return {
                "output": f"ran:{command}",
                "error": "",
                "exit_code": 0,
                "environment": environment,
            }

        async def execute_python(self, code):
            return {"output": "", "error": "", "exit_code": 0}

    sandbox = HostSandbox()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)
    decisions = iter(["deny", "once", "session"])
    requests = []

    async def approve(request):
        requests.append(request)
        return next(decisions)

    registry.set_approval_handler(approve)

    denied = run(registry.execute(
        "execute_shell",
        {
            "command": "first",
            "environment": "windows",
            "justification": "读取当前工作区状态，确认这次审批对应的实际命令。",
        },
        call_id="shell-1",
    ))
    assert denied["error_type"] == "approval_denied"
    assert sandbox.commands == []

    allowed_once = run(registry.execute(
        "execute_shell",
        {"command": "second", "environment": "windows"},
        call_id="shell-2",
    ))
    assert allowed_once["output"] == "ran:second"

    allowed_session = run(registry.execute(
        "execute_shell",
        {"command": "third", "environment": "windows"},
        call_id="shell-3",
    ))
    assert allowed_session["output"] == "ran:third"

    reused = run(registry.execute(
        "execute_shell",
        {"command": "third", "environment": "windows"},
        call_id="shell-4",
    ))
    assert reused["output"] == "ran:third"
    assert len(requests) == 3
    assert runtime_metrics.snapshot()["approval_wait_ms_count"] == 3
    assert requests[0]["kind"] == "host_execution"
    assert requests[0]["operation"] == "在受保护主机上执行 shell 命令"
    assert requests[0]["target"] == "first"
    assert requests[0]["agent_reason"] == "读取当前工作区状态，确认这次审批对应的实际命令。"
    assert requests[0]["reason_source"] == "agent"
    assert requests[0]["approval_title"] == "在受保护主机上运行 shell 命令"
    assert "运行所请求的 shell 命令" in requests[0]["approval_summary"]
    assert requests[0]["approval_question"] == "是否允许我在宿主机上运行所请求的 shell 命令？"
    assert "可能读取或修改主机状态" in requests[0]["approval_effect"]
    assert sandbox.commands == [
        ("second", "windows"),
        ("third", "windows"),
        ("third", "windows"),
    ]

    registry.policy.deny("execute_shell")
    hard_denied = run(registry.execute(
        "execute_shell",
        {"command": "third", "environment": "windows"},
        call_id="shell-5",
    ))
    assert hard_denied["error_type"] == "policy_denied"
    assert len(sandbox.commands) == 3


def test_host_shell_approval_uses_semantic_copy_for_compound_inspection(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            raise AssertionError("denied approval must stop before host execution")

    registry = ToolRegistry()
    register_code_tools(registry, HostSandbox())
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    result = run(registry.execute(
        "execute_shell",
        {
            "command": "git status --short; Select-String -Path files.py -Pattern approval",
            "environment": "windows",
        },
    ))

    assert result["error_type"] == "approval_denied"
    assert len(requests) == 1
    request = requests[0]
    assert request["approval_title"] == "检查工作区状态与源码"
    assert "检查 Git 工作区状态" in request["approval_summary"]
    assert "搜索源码或文本" in request["approval_summary"]
    assert request["compound_commands"] == [
        "git status --short",
        "Select-String -Path files.py -Pattern approval",
    ]
    assert request["target"].startswith("git status --short")


@pytest.mark.parametrize(
    ("command", "second_summary"),
    [
        ("git status --short; deploy-production", "运行所请求的 shell 命令"),
        ("git status --short; echo git status", "运行所请求的 shell 命令"),
        ("Get-Content config.yaml\r\nSet-Content config.yaml changed", "运行所请求的 shell 命令"),
        ("rg approval agent & npm run build", "运行项目命令"),
    ],
)
def test_host_shell_mixed_operations_are_not_presented_as_inspection_only(
    tmp_path,
    command,
    second_summary,
):
    class HostSandbox:
        workdir = str(tmp_path)

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            raise AssertionError("denied approval must stop before host execution")

    registry = ToolRegistry()
    register_code_tools(registry, HostSandbox())
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    result = run(registry.execute(
        "execute_shell",
        {"command": command, "environment": "windows"},
    ))

    assert result["error_type"] == "approval_denied"
    assert len(requests) == 1
    request = requests[0]
    assert request["approval_title"] == "在受保护主机上运行 shell 命令"
    assert second_summary in request["approval_summary"]
    assert "可能读取或修改主机状态" in request["approval_effect"]
    assert request["compound_command_count"] == 2


def test_host_shell_approval_marks_long_command_preview_as_truncated(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            raise AssertionError("denied approval must stop before host execution")

    registry = ToolRegistry()
    register_code_tools(registry, HostSandbox())
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    command = "printf start-sentinel-" + "x" * 1_200 + "-tail-sentinel"

    result = run(registry.execute(
        "execute_shell",
        {"command": command, "environment": "auto"},
    ))

    assert result["error_type"] == "approval_denied"
    assert len(requests) == 1
    request = requests[0]
    assert request["operation"] == "在受保护主机上执行 shell 命令"
    assert request["approval_summary"] == "运行所请求的 shell 命令"
    assert request["target"].startswith("[命令已截断：省略 ")
    assert "start-sentinel" in request["target"]
    assert request["target"].endswith("tail-sentinel")
    assert len(request["target"]) <= 1_000
    assert request["arguments"]["command"] == request["target"]
    marker, visible = request["target"].split("] ", 1)
    omitted = int(marker.split("省略 ", 1)[1].split(" 字符", 1)[0])
    head, tail = visible.split(" … ", 1)
    assert omitted == len(command) - len(head) - len(tail)


def test_host_python_approval_previews_real_code_without_changing_execution_or_hash(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        def __init__(self):
            self.python_bodies = []

        async def execute_python(self, code):
            self.python_bodies.append(code)
            return {"output": "python-complete", "error": "", "exit_code": 0}

    sandbox = HostSandbox()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)
    requests = []

    async def allow_once(request):
        requests.append(request)
        return "once"

    registry.set_approval_handler(allow_once)
    code = "print('python-head-sentinel')\n" + "value = 1\n" * 80 + "print('python-tail-sentinel')"

    result = run(registry.execute("execute_python", {"code": code}))

    assert result["output"] == "python-complete"
    assert sandbox.python_bodies == [code]
    assert len(requests) == 1
    request = requests[0]
    assert request["scope"] == (
        "host-execution:python:" + hashlib.sha256(code.encode("utf-8")).hexdigest()
    )
    preview = request["arguments"]["code"]
    assert preview.startswith("[代码已截断：省略 ")
    assert "python-head-sentinel" in preview
    assert preview.endswith("python-tail-sentinel')")
    assert len(preview) <= 240
    assert "chars preserved" not in preview
    marker, visible = preview.split("] ", 1)
    omitted = int(marker.split("省略 ", 1)[1].split(" 字符", 1)[0])
    head, tail = visible.split(" … ", 1)
    assert omitted == len(code) - len(head) - len(tail)


def test_host_shell_approval_bounds_long_compound_operation_previews(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            raise AssertionError("denied approval must stop before host execution")

    registry = ToolRegistry()
    register_code_tools(registry, HostSandbox())
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    first = "printf compound-start-" + "x" * 1_200 + "-compound-tail"
    command = first + " && echo second-operation"

    result = run(registry.execute(
        "execute_shell",
        {"command": command, "environment": "auto"},
    ))

    assert result["error_type"] == "approval_denied"
    request = requests[0]
    assert request["target"].startswith("[命令已截断：省略 ")
    assert len(request["target"]) <= 1_000
    assert len(request["compound_commands"]) == 2
    assert request["compound_commands"][0].startswith("[命令已截断：省略 ")
    assert "compound-start" in request["compound_commands"][0]
    assert request["compound_commands"][0].endswith("compound-tail")
    assert request["compound_commands"][1] == "echo second-operation"
    assert all(len(part) <= 240 for part in request["compound_commands"])
    assert sum(map(len, request["compound_commands"])) <= 16 * 240
    assert "x" * 100 not in request["detail"]
    assert "复合命令共 2 项" in request["detail"]


def test_host_shell_approval_preserves_full_compound_operation_count(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            raise AssertionError("denied approval must stop before host execution")

    registry = ToolRegistry()
    register_code_tools(registry, HostSandbox())
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    command = " && ".join(f"echo operation-{index}" for index in range(20))

    result = run(registry.execute(
        "execute_shell",
        {"command": command, "environment": "auto"},
    ))

    assert result["error_type"] == "approval_denied"
    request = requests[0]
    assert request["compound_command_count"] == 20
    assert len(request["compound_commands"]) == 16
    assert "复合命令共 20 项" in request["detail"]


def test_docker_shell_does_not_request_host_execution_approval(tmp_path):
    class FakeDocker(DockerSandbox):
        def __init__(self):
            super().__init__(workdir=str(tmp_path))
            self.called = False

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            self.called = True
            on_output("stdout", "inside docker")
            return {"output": "inside docker", "error": "", "exit_code": 0}

        async def execute_python_stream(self, code, on_output=None):
            return {"output": "", "error": "", "exit_code": 0}

    sandbox = FakeDocker()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)

    async def unexpected(_request):
        raise AssertionError("isolated Docker execution must not request host approval")

    registry.set_approval_handler(unexpected)
    result = run(registry.execute("execute_shell", {"command": "echo ok"}))
    assert result["output"] == "inside docker"
    assert sandbox.called is True


def test_host_shell_session_approval_reuses_safe_test_prefix(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        def __init__(self):
            self.commands = []

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            self.commands.append(command)
            return {"output": "passed", "error": "", "exit_code": 0}

        async def execute_python(self, code):
            return {"output": code, "error": "", "exit_code": 0}

    sandbox = HostSandbox()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)
    requests = []

    async def approve(request):
        requests.append(request)
        return "session"

    registry.set_approval_handler(approve)
    python = f'"{sys.executable}"'
    first = f"{python} -m pytest tests/test_alpha.py -q"
    second = f"{python} -m pytest tests/test_beta.py -q"

    assert run(registry.execute(
        "execute_shell", {"command": first, "environment": "windows"}
    ))["error"] == ""
    assert run(registry.execute(
        "execute_shell", {"command": second, "environment": "windows"}
    ))["error"] == ""

    assert len(requests) == 1
    assert requests[0]["session_scope_label"].endswith("-m pytest")
    assert "选择会话授权同时允许" in requests[0]["detail"]
    assert sandbox.commands == [first, second]


@pytest.mark.parametrize(
    ("environment", "separator", "second_operation"),
    [
        ("posix", "\n", "printf second-host-command"),
        ("windows", "\r\n", "echo second-host-command"),
        ("windows", " & ", "echo second-host-command"),
    ],
)
def test_host_shell_session_prefix_does_not_reuse_for_separator_commands(
    tmp_path,
    environment,
    separator,
    second_operation,
):
    class HostSandbox:
        workdir = str(tmp_path)

        def __init__(self):
            self.commands = []

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            self.commands.append((command, environment))
            return {"output": "passed", "error": "", "exit_code": 0}

    sandbox = HostSandbox()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)
    requests = []
    decisions = iter(["session", "deny"])

    async def decide(request):
        requests.append(request)
        return next(decisions)

    registry.set_approval_handler(decide)
    python = f'"{sys.executable}"'
    safe = f"{python} -m pytest tests/test_alpha.py -q"
    compound = f"{python} -m pytest tests/test_beta.py -q{separator}{second_operation}"

    allowed = run(registry.execute(
        "execute_shell",
        {"command": safe, "environment": environment},
    ))
    denied = run(registry.execute(
        "execute_shell",
        {"command": compound, "environment": environment},
    ))

    assert allowed["error"] == ""
    assert denied["error_type"] == "approval_denied"
    assert sandbox.commands == [(safe, environment)]
    assert len(requests) == 2
    assert "session_scope" not in requests[1]
    assert requests[1]["scope"] == (
        f"host-execution:shell:{environment}:"
        + hashlib.sha256(compound.encode("utf-8")).hexdigest()
    )
    assert requests[1]["compound_command_count"] == 2
    assert requests[1]["compound_commands"] == [
        f"{python} -m pytest tests/test_beta.py -q",
        second_operation,
    ]


@pytest.mark.parametrize(
    ("environment", "substitution"),
    [
        ("posix", "$(printf second-host-command)"),
        ("wsl", "`printf second-host-command`"),
    ],
)
def test_host_shell_session_prefix_does_not_reuse_for_command_substitution(
    tmp_path,
    environment,
    substitution,
):
    class HostSandbox:
        workdir = str(tmp_path)

        def __init__(self):
            self.commands = []

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            self.commands.append((command, environment))
            return {"output": "passed", "error": "", "exit_code": 0}

    sandbox = HostSandbox()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)
    requests = []
    decisions = iter(["session", "deny"])

    async def decide(request):
        requests.append(request)
        return next(decisions)

    registry.set_approval_handler(decide)
    python = f'"{sys.executable}"'
    safe = f"{python} -m pytest tests/test_alpha.py -q"
    substituted = f"{python} -m pytest tests/test_beta.py -q {substitution}"

    assert run(registry.execute(
        "execute_shell", {"command": safe, "environment": environment}
    ))["error"] == ""
    denied = run(registry.execute(
        "execute_shell", {"command": substituted, "environment": environment}
    ))

    assert denied["error_type"] == "approval_denied"
    assert sandbox.commands == [(safe, environment)]
    assert len(requests) == 2
    assert "session_scope" not in requests[1]
    assert requests[1]["scope"] == (
        f"host-execution:shell:{environment}:"
        + hashlib.sha256(substituted.encode("utf-8")).hexdigest()
    )
    assert requests[1]["approval_title"] == "在受保护主机上运行 shell 命令"


@pytest.mark.parametrize(
    ("environment", "separator", "second_operation"),
    [
        ("posix", "\n", "printf delegated-host-command"),
        ("windows", "\r\n", "echo delegated-host-command"),
        ("windows", " & ", "echo delegated-host-command"),
    ],
)
def test_delegated_worker_does_not_auto_allow_separator_commands(
    tmp_path,
    environment,
    separator,
    second_operation,
):
    class HostSandbox:
        workdir = str(tmp_path)

        def __init__(self):
            self.commands = []

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            self.commands.append((command, environment))
            return {"output": "passed", "error": "", "exit_code": 0}

    sandbox = HostSandbox()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    python = f'"{sys.executable}"'
    first_operation = f"{python} -m pytest tests/test_worker.py -q"
    compound = f"{first_operation}{separator}{second_operation}"

    result = run(registry.execute(
        "execute_shell",
        {"command": compound, "environment": environment},
        execution_origin="delegate:worker",
    ))

    assert result["error_type"] == "approval_denied"
    assert sandbox.commands == []
    assert len(requests) == 1
    assert "session_scope" not in requests[0]
    assert requests[0]["scope"] == (
        f"host-execution:shell:{environment}:"
        + hashlib.sha256(compound.encode("utf-8")).hexdigest()
    )
    assert requests[0]["compound_command_count"] == 2
    assert requests[0]["compound_commands"] == [first_operation, second_operation]


@pytest.mark.parametrize(
    ("environment", "substitution"),
    [
        ("posix", "$(printf delegated-host-command)"),
        ("wsl", "`printf delegated-host-command`"),
    ],
)
def test_delegated_worker_does_not_auto_allow_command_substitution(
    tmp_path,
    environment,
    substitution,
):
    class HostSandbox:
        workdir = str(tmp_path)

        def __init__(self):
            self.commands = []

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            self.commands.append((command, environment))
            return {"output": "passed", "error": "", "exit_code": 0}

    sandbox = HostSandbox()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    python = f'"{sys.executable}"'
    command = f"{python} -m pytest tests/test_worker.py -q {substitution}"

    result = run(registry.execute(
        "execute_shell",
        {"command": command, "environment": environment},
        execution_origin="delegate:worker",
    ))

    assert result["error_type"] == "approval_denied"
    assert sandbox.commands == []
    assert len(requests) == 1
    assert "session_scope" not in requests[0]
    assert requests[0]["scope"] == (
        f"host-execution:shell:{environment}:"
        + hashlib.sha256(command.encode("utf-8")).hexdigest()
    )
    assert requests[0]["approval_title"] == "在受保护主机上运行 shell 命令"


def test_host_shell_escaped_quote_does_not_hide_mixed_operation(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            raise AssertionError("denied approval must stop before host execution")

    registry = ToolRegistry()
    register_code_tools(registry, HostSandbox())
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    command = r'git status \" ; echo second-host-command'

    result = run(registry.execute(
        "execute_shell", {"command": command, "environment": "posix"}
    ))

    assert result["error_type"] == "approval_denied"
    assert len(requests) == 1
    assert requests[0]["approval_title"] == "在受保护主机上运行 shell 命令"
    assert requests[0]["approval_summary"] == (
        "检查 Git 工作区状态; 运行所请求的 shell 命令（2 项操作）"
    )
    assert requests[0]["compound_commands"] == [
        r'git status \"',
        "echo second-host-command",
    ]


def test_host_shell_malformed_quote_retains_control_syntax_as_generic(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            raise AssertionError("denied approval must stop before host execution")

    registry = ToolRegistry()
    register_code_tools(registry, HostSandbox())
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    command = 'git status " ; echo second-host-command'

    result = run(registry.execute(
        "execute_shell", {"command": command, "environment": "posix"}
    ))

    assert result["error_type"] == "approval_denied"
    assert len(requests) == 1
    assert requests[0]["approval_title"] == "在受保护主机上运行 shell 命令"
    assert requests[0]["approval_summary"] == "检查 Git 工作区状态"
    assert "可能读取或修改主机状态" in requests[0]["approval_effect"]
    assert "compound_commands" not in requests[0]


def test_host_shell_session_approval_covers_workspace_test_scripts_only(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        def __init__(self):
            self.commands = []

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            self.commands.append(command)
            return {"output": "passed", "error": "", "exit_code": 0}

        async def execute_python(self, code):
            return {"output": code, "error": "", "exit_code": 0}

    sandbox = HostSandbox()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)
    requests = []
    decisions = iter(["session", "deny"])

    async def approve(request):
        requests.append(request)
        return next(decisions)

    registry.set_approval_handler(approve)
    python = f'"{sys.executable}"'
    first = f'{python} "{tmp_path / "test_alpha.py"}"'
    second = f'{python} "{tmp_path / "test_beta.py"}"'
    non_test = f'{python} "{tmp_path / "deploy.py"}"'

    assert run(registry.execute(
        "execute_shell", {"command": first, "environment": "windows"}
    ))["error"] == ""
    assert run(registry.execute(
        "execute_shell", {"command": second, "environment": "windows"}
    ))["error"] == ""
    denied = run(registry.execute(
        "execute_shell", {"command": non_test, "environment": "windows"}
    ))

    assert denied["error_type"] == "approval_denied"
    assert len(requests) == 2
    assert requests[0]["session_scope_label"].endswith(
        "<any workspace test script>"
    )
    assert "session_scope" not in requests[1]
    assert sandbox.commands == [first, second]


def test_worker_auto_allows_safe_workspace_test_but_not_arbitrary_host_script(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        def __init__(self):
            self.commands = []

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            self.commands.append(command)
            return {"output": "passed", "error": "", "exit_code": 0}

        async def execute_python(self, code):
            return {"output": code, "error": "", "exit_code": 0}

    sandbox = HostSandbox()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    python = f'"{sys.executable}"'
    safe_test = f'{python} "{tmp_path / "test_worker.py"}"'
    arbitrary = f'{python} "{tmp_path / "deploy.py"}"'

    worker_test = run(registry.execute(
        "execute_shell",
        {"command": safe_test, "environment": "windows"},
        execution_origin="delegate:worker",
    ))
    parent_test = run(registry.execute(
        "execute_shell",
        {"command": safe_test, "environment": "windows"},
    ))
    worker_arbitrary = run(registry.execute(
        "execute_shell",
        {"command": arbitrary, "environment": "windows"},
        execution_origin="delegate:worker",
    ))

    assert worker_test["error"] == ""
    assert parent_test["error_type"] == "approval_denied"
    assert worker_arbitrary["error_type"] == "approval_denied"
    assert sandbox.commands == [safe_test]
    assert len(requests) == 2


def test_session_and_restart_clear_exact_host_execution_approval(tmp_path):
    class HostSandbox:
        workdir = str(tmp_path)

        def __init__(self):
            self.commands = []

        async def execute_shell_stream(self, command, environment="auto", on_output=None):
            self.commands.append(command)
            return {"output": command, "error": "", "exit_code": 0}

        async def execute_python(self, code):
            return {"output": code, "error": "", "exit_code": 0}

    sandbox = HostSandbox()
    registry = ToolRegistry()
    register_code_tools(registry, sandbox)
    decisions = iter(["session", "deny"])
    requests = []

    async def decide(request):
        requests.append(request)
        return next(decisions)

    registry.set_approval_handler(decide)
    assert run(registry.execute("execute_shell", {"command": "build"}))["error"] == ""
    assert run(registry.execute("execute_shell", {"command": "build"}))["error"] == ""
    assert len(requests) == 1

    registry.hooks.dispatch_session_end("alpha", "session_switch")
    denied = run(registry.execute("execute_shell", {"command": "build"}))
    assert denied["error_type"] == "approval_denied"
    assert len(requests) == 2
    assert sandbox.commands == ["build", "build"]

    restarted = ToolRegistry()
    register_code_tools(restarted, sandbox)
    restart_requests = []

    async def deny_after_restart(request):
        restart_requests.append(request)
        return "deny"

    restarted.set_approval_handler(deny_after_restart)
    denied_after_restart = run(restarted.execute("execute_shell", {"command": "build"}))
    assert denied_after_restart["error_type"] == "approval_denied"
    assert len(restart_requests) == 1
    assert sandbox.commands == ["build", "build"]


def test_filesystem_session_grant_is_revoked_before_next_session(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target = outside / "result.txt"
    registry = ToolRegistry()
    register_file_tools(
        registry,
        str(workspace),
        policy=FilesystemPolicy(workspace),
    )
    decisions = iter(["session", "deny"])

    async def decide(_request):
        return next(decisions)

    registry.set_approval_handler(decide)
    first = run(registry.execute(
        "write_file",
        {"path": str(target), "content": "first"},
    ))
    assert first["error"] == ""
    assert target.read_text(encoding="utf-8") == "first"

    registry.hooks.dispatch_session_end("alpha", "session_switch")
    second = run(registry.execute(
        "write_file",
        {"path": str(target), "content": "second"},
    ))
    assert second["error_type"] == "approval_denied"
    assert target.read_text(encoding="utf-8") == "first"


def test_filesystem_external_read_write_and_directory_scope_are_reviewed(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    folder = outside / "reports"
    workspace.mkdir()
    folder.mkdir(parents=True)
    target = folder / "result.txt"
    target.write_text("before", encoding="utf-8")
    registry = ToolRegistry()
    register_file_tools(registry, str(workspace), policy=FilesystemPolicy(workspace))
    requests = []

    async def decide(request):
        requests.append(request)
        if request["scope_kind"] == "directory":
            return "session"
        if request["access"] == "read" and len(requests) == 1:
            return "once"
        if request["access"] == "write" and len(requests) == 2:
            return "once"
        return "deny"

    registry.set_approval_handler(decide)

    read = run(registry.execute(
        "read_file",
        {
            "path": str(target),
            "justification": "读取这份报告以核对审批范围和当前文件内容。",
        },
    ))
    edited = run(registry.execute(
        "edit_file",
        {"path": str(target), "old": "before", "new": "after"},
    ))
    listed = run(registry.execute("search_files", {"pattern": "*.txt", "path": str(folder)}))
    nested_read = run(registry.execute("read_file", {"path": str(target)}))
    write_after_directory_read = run(registry.execute(
        "edit_file",
        {"path": str(target), "old": "after", "new": "after2"},
    ))

    assert read["output"] == "before"
    assert edited["error"] == ""
    assert listed["error"] == ""
    assert nested_read["output"] == "after"
    assert write_after_directory_read["error_type"] == "approval_denied"
    assert [request["access"] for request in requests] == ["read", "write", "read", "write"]
    assert requests[0]["outside_workspace"] is True
    assert requests[0]["agent_reason"] == "读取这份报告以核对审批范围和当前文件内容。"
    assert requests[0]["reason_source"] == "agent"
    assert requests[0]["scope_kind"] == "file"
    assert requests[1]["scope_kind"] == "file"
    assert requests[2]["scope_kind"] == "directory"
    assert requests[2]["target"] == str(folder.resolve())
    assert requests[3]["scope_kind"] == "file"
    assert requests[3]["access"] == "write"

    registry.hooks.dispatch_session_end("session-1", "session_switch")
    after_session = run(registry.execute("read_file", {"path": str(target)}))
    assert after_session["error_type"] == "approval_denied"
    assert len(requests) == 5


def test_headless_ask_returns_structured_pause_without_side_effect():
    calls = []
    policy = ToolPolicy(mode="permissive")
    policy.add_rule(PolicyRule(tool_pattern="deploy", action="ask"))
    registry = ToolRegistry(policy=policy)

    async def deploy():
        calls.append("deployed")
        return "done"

    registry.register(ToolDef(
        name="deploy",
        description="deploy",
        parameters={"type": "object", "properties": {}},
        fn=deploy,
        risk="execute",
    ))
    result = run(registry.execute("deploy", {}))
    assert result["code"] == "approval_required"
    assert result["recoverable"] is True
    assert calls == []


@pytest.mark.parametrize("authoritative,auto_grant,expected", [
    (False, False, True), (True, False, False), (True, True, True),
])
def test_pending_yolo_eligibility_comes_from_the_tool_boundary(authoritative, auto_grant, expected):
    registry = ToolRegistry()
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    async def execute():
        raise AssertionError("denied tool must not execute")

    registry.set_approval_handler(deny)
    registry.register(ToolDef(
        name="boundary_probe", description="probe", parameters={"type": "object"},
        fn=execute, risk="read", permission_authoritative=authoritative,
        permission_yolo_auto_grant=auto_grant,
        permission_check=lambda _: {"reason": "approval needed", "yolo_bypass_allowed": not expected},
        permission_grant=lambda *_: None,
    ))
    result = run(registry.execute("boundary_probe", {}))
    assert result["error_type"] == "approval_denied"
    assert requests[0]["yolo_bypass_allowed"] is expected


def test_yolo_bypass_does_not_persist_after_yolo_is_disabled():
    calls = []
    policy = ToolPolicy(mode="permissive")
    policy.add_rule(PolicyRule(tool_pattern="deploy", action="ask"))
    registry = ToolRegistry(policy=policy)

    async def deploy():
        calls.append("deployed")
        return "done"

    registry.register(ToolDef(
        name="deploy",
        description="deploy",
        parameters={"type": "object", "properties": {}},
        fn=deploy,
        risk="execute",
    ))
    registry.yolo = True
    assert run(registry.execute("deploy", {}))["output"] == "done"
    assert "deploy" not in registry.policy.allowed_tools
    assert "deploy" not in registry._session_policy_allows

    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.yolo = False
    registry.set_approval_handler(deny)
    denied = run(registry.execute("deploy", {}))
    assert denied["error_type"] == "approval_denied"
    assert len(requests) == 1
    assert calls == ["deployed"]


def test_network_reads_do_not_request_approval_across_browser_and_web(tmp_path):
    class FakeReadBackend:
        name = "fake-read"
        capabilities = BackendCapabilities(read=True, interactive=False, takeover=False)

        async def extract(self, url, *, max_length=12000):
            return "page"

        async def status(self):
            return True, "ready"

    registry = ToolRegistry()
    manager = BrowserSessionManager(path=tmp_path / "browser.db", backend=FakeReadBackend())
    register_browser_tools(registry, manager=manager, backend=manager.backend)
    register_web_tools(registry, object())
    requests = []

    async def deny(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(deny)
    opened = run(registry.execute(
        "browser_open",
        {"url": "https://example.com/path", "extract": False},
    ))
    assert opened["error"] == ""
    assert requests == []
    for name in (
        "browser_open",
        "browser_extract",
        "browser_connect",
        "search_web",
        "web_extract",
        "fetch_url",
        "search_status",
        "extract_url",
    ):
        tool = registry.get(name)
        assert tool is not None
        assert tool.risk == "network"
        assert tool.approval == "never"
        assert (tool.permission_check is not None) == (name == "fetch_url")


def test_browser_write_requires_distinct_scope_and_denial_has_no_side_effect(tmp_path):
    """Approval-free network reads must never authorize clicks or typing."""
    class FakeInteractiveBackend:
        name = "fake"
        capabilities = BackendCapabilities(read=True, interactive=True, takeover=False)

        def __init__(self):
            self.clicks = []
            self.typed = []

        async def status(self):
            return True, "ready"

        async def extract(self, url, *, max_length=12000):
            return "page"

        async def interactive_state(self, *, tab_id="", url=""):
            return url, "Example", "page"

        async def interactive_click(self, selector, *, tab_id="", url=""):
            self.clicks.append((selector, tab_id, url))
            return "clicked"

        async def interactive_type(self, selector, text, *, tab_id="", url=""):
            self.typed.append((selector, text, tab_id, url))
            return "typed"

        async def close_connection(self, tab_id=""):
            return None

    backend = FakeInteractiveBackend()
    registry = ToolRegistry()
    manager = BrowserSessionManager(path=tmp_path / "browser.db")
    register_browser_tools(registry, manager=manager, backend=backend)
    requests = []

    async def decide(request):
        requests.append(request)
        if request["operation"] == "Click page element":
            return "deny"
        return "once"

    registry.set_approval_handler(decide)
    run(registry.execute(
        "browser_open",
        {"url": "https://example.com", "extract": False},
    ))

    denied = run(registry.execute("browser_click", {"selector": "#purchase"}))
    assert denied["error_type"] == "approval_denied"
    assert backend.clicks == []

    typed = run(registry.execute(
        "browser_type",
        {"selector": "#note", "text": "private form contents"},
    ))
    assert typed["error"] == ""
    type_request = next(item for item in requests if item["operation"] == "Type into page")
    assert type_request["arguments"]["text"] == "<21 chars>"
    assert "private form contents" not in str(type_request)
    assert len(backend.typed) == 1
    assert backend.typed[0][1] == "private form contents"


def test_cross_origin_browser_open_does_not_request_network_approval(tmp_path):
    """Read-only browser navigation does not pause for per-origin approval."""
    class FakeInteractiveBackend:
        name = "fake"
        capabilities = BackendCapabilities(read=True, interactive=True, takeover=False)

        def __init__(self):
            self.clicks = []

        async def status(self):
            return True, "ready"

        async def extract(self, url, *, max_length=12000):
            return "page"

        async def interactive_state(self, *, tab_id="", url=""):
            return url or "https://example.com", "Example", "page"

        async def interactive_click(self, selector, *, tab_id="", url=""):
            self.clicks.append((selector, tab_id, url))
            return "clicked"

        async def close_connection(self, tab_id=""):
            return None

    backend = FakeInteractiveBackend()
    registry = ToolRegistry()
    manager = BrowserSessionManager(path=tmp_path / "browser.db")
    register_browser_tools(registry, manager=manager, backend=backend)
    requests = []

    async def decide(request):
        requests.append(request)
        return "deny"

    registry.set_approval_handler(decide)
    first = run(registry.execute(
        "browser_open",
        {"url": "https://example.com", "extract": False},
    ))
    second = run(registry.execute(
        "browser_open",
        {"url": "https://other.com", "extract": False},
    ))
    assert first["error"] == ""
    assert second["error"] == ""
    assert backend.clicks == []
    assert requests == []


def test_web_and_browser_network_tools_do_not_expose_permission_hooks(tmp_path):
    """Read-only network tools bypass interactive approval hooks."""
    class FakeReadBackend:
        name = "fake-read"
        capabilities = BackendCapabilities(read=True, interactive=False, takeover=False)

        async def extract(self, url, *, max_length=12000):
            return "page"

        async def status(self):
            return True, "ready"

    registry = ToolRegistry()
    manager = BrowserSessionManager(path=tmp_path / "browser.db", backend=FakeReadBackend())
    register_browser_tools(registry, manager=manager, backend=manager.backend)
    register_web_tools(registry, object())
    for name in (
        "browser_open",
        "browser_extract",
        "browser_connect",
        "search_web",
        "web_extract",
        "fetch_url",
        "search_status",
        "extract_url",
    ):
        tool = registry.get(name)
        assert tool is not None
        assert (tool.permission_check is not None) == (name == "fetch_url")
        assert (tool.permission_grant is not None) == (name == "fetch_url")


@dataclass
class FakeRemoteTool:
    name: str = "send_message"
    description: str = "send a message"
    inputSchema: dict | None = None

    def __post_init__(self):
        self.inputSchema = {
            "type": "object",
            "properties": {"body": {"type": "string"}},
            "required": ["body"],
        }


class FakeMcpResult:
    structuredContent: ClassVar[dict[str, bool]] = {"ok": True}
    content: ClassVar[list] = []


class FakeMcpSession:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return FakeMcpResult()


def test_mcp_declared_side_effect_requires_exact_tool_approval():
    registry = ToolRegistry()
    session = FakeMcpSession()
    manager = MCPManager()
    manager._register_tools(
        registry,
        "messaging",
        session,
        [FakeRemoteTool()],
        {"risk": "network", "tool_risks": {"send_message": "write"}},
    )
    decisions = iter(["deny", "once"])
    requests = []

    async def decide(request):
        requests.append(request)
        return next(decisions)

    registry.set_approval_handler(decide)
    invalid = run(registry.execute("mcp__messaging__send_message", {}))
    assert invalid["error_type"] == "invalid_input"
    assert requests == []
    assert session.calls == []

    denied = run(registry.execute(
        "mcp__messaging__send_message",
        {"body": "hello"},
    ))
    assert denied["error_type"] == "approval_denied"
    assert session.calls == []

    allowed = run(registry.execute(
        "mcp__messaging__send_message",
        {"body": "hello", "justification": "业务协议需要把这段说明原样传给远端工具。"},
    ))
    assert allowed["error"] == ""
    assert session.calls == [(
        "send_message",
        {"body": "hello", "justification": "业务协议需要把这段说明原样传给远端工具。"},
    )]
    assert requests[0]["kind"] == "mcp_side_effect"
    assert requests[0]["target"] == "messaging/send_message"


def test_cdp_fallback_blocks_cross_origin_redirect(tmp_path):
    """_fallback_extract CDP rung must verify final origin matches approved origin."""
    class FakeRedirectBackend:
        name = "fake-redirect"
        capabilities = BackendCapabilities(read=True, interactive=True, takeover=False)

        def __init__(self):
            self.navigated = []
            self.text_calls = 0

        async def status(self):
            return True, "ready"

        async def extract(self, url, *, max_length=12000):
            # Return too-short content to trigger CDP escalation
            return "short"

        async def interactive_navigate(self, url, *, tab_id="", wait_ms=2000):
            self.navigated.append((url, tab_id))
            return f"navigated to {url}"

        async def interactive_state(self, *, tab_id="", url=""):
            # Simulate redirect: navigate was to example.com,
            # but the page redirected to evil.com
            return "https://evil.com/phishing", "Evil Page", "phishing content"

        async def interactive_get_text(self, *, tab_id="", url=""):
            self.text_calls += 1
            return "should not be reached"

        async def interactive_click(self, selector, *, tab_id="", url=""):
            return "clicked"

        async def close_connection(self, tab_id=""):
            return None

    backend = FakeRedirectBackend()
    registry = ToolRegistry()
    manager = BrowserSessionManager(path=tmp_path / "browser.db")
    register_browser_tools(registry, manager=manager, backend=backend)

    async def approve(request):
        return "session"

    registry.set_approval_handler(approve)

    # browser_extract on example.com — approval passes, then CDP rung escalates
    result = run(registry.execute(
        "browser_extract",
        {"url": "https://example.com/article"},
    ))
    # The CDP fallback should detect the cross-origin redirect.
    # Error string is in result["output"] since _browser_extract returns it directly.
    error_text = result.get("error", "") or result.get("output", "")
    assert "Cross-origin redirect blocked" in error_text
    assert "example.com" in error_text
    assert "evil.com" in error_text
    # get_text must NOT have been called — the guard blocked before it
    assert backend.text_calls == 0

    # Verify the navigation did happen (rung 2 was attempted)
    assert len(backend.navigated) == 1
    assert backend.navigated[0][0] == "https://example.com/article"

    # Verify that a different origin that doesn't redirect works fine
    backend.navigated.clear()

    # For a URL where state returns matching origin, extraction should succeed
    # (but our fake always returns evil.com, so we've already covered the block path)


def test_postcondition_failure_metric_is_derived_from_real_verification():
    runtime_metrics.reset()
    registry = ToolRegistry()

    async def write_demo():
        return "claimed success"

    from agent.runtime.tools.registry import ToolDef

    registry.register(ToolDef(
        name="write_demo",
        description="demo",
        parameters={"type": "object", "properties": {}},
        fn=write_demo,
        risk="write",
        postcondition=lambda _args, _result: (False, "artifact missing"),
    ))
    result = run(registry.execute("write_demo", {}))
    assert result["error_type"] == "postcondition_failed"
    metrics = runtime_metrics.snapshot()
    assert metrics["postcondition_check_count"] == 1
    assert metrics["postcondition_failure_rate"] == 1.0


def test_catalog_bound_app_state_approval_scope_isolated_from_snapshot_scope():
    from agent.runtime.computer_policy import (
        app_state_approval_scope,
        snapshot_approval_scope,
    )

    context = {
        "session_id": "session-1",
        "app_ref": "app-1",
        "window_ref": "window-1",
    }
    app_state = app_state_approval_scope(
        context,
        catalog_generation=3,
        app_ref="app-1",
        window_ref="window-1",
        capture_scope="target_window",
        text_detail_mode="on",
    )
    snapshot = snapshot_approval_scope(
        context,
        capture_scope="target_window",
        text_detail_mode="on",
        target_binding="session-1:app-1:window-1:3",
    )

    assert app_state != snapshot
    assert app_state.startswith("computer-observe-app-state:")
    assert "session-1:3:app-1:window-1:target_window:on" in app_state


# ── normalized_origin regression ────────────────────────────────────────


def test_normalized_origin_strips_default_port_and_keeps_non_default():
    from agent.runtime.tools.approval import normalized_origin

    # HTTPS default port 443: must be stripped
    assert normalized_origin("HTTPS://Example.COM:443/path") == "https://example.com"
    assert normalized_origin("https://example.com:443") == "https://example.com"
    # HTTP default port 80: must be stripped
    assert normalized_origin("HTTP://Example.COM:80/path") == "http://example.com"
    assert normalized_origin("http://example.com:80") == "http://example.com"

    # Non-default ports: must be preserved
    assert normalized_origin("https://example.com:8443/path") == "https://example.com:8443"
    assert normalized_origin("http://example.com:8080/path") == "http://example.com:8080"

    # No port: no suffix added
    assert normalized_origin("https://example.com/path") == "https://example.com"
    assert normalized_origin("http://example.com/path") == "http://example.com"

    # Scheme and host always lowercased
    assert normalized_origin("HTTPS://Example.COM/path") == "https://example.com"

    # Invalid / non-HTTP
    assert normalized_origin("ftp://example.com/path") == ""
    assert normalized_origin("not-a-url") == ""


@pytest.mark.parametrize("opt_in", [False, True])
def test_yolo_exact_permission_opt_in_keeps_checks_grants_and_finalizers(opt_in):
    events = []
    registry = ToolRegistry()
    registry.yolo = True

    def check(args):
        events.append("check")
        assert args["__permission_call_id"]
        return {"reason": "exact test", "choices": ["once", "deny"]}

    def grant(args, request, decision):
        events.append(decision)
        return lambda: events.append("cleanup")

    async def execute():
        events.append("execute")
        return "done"

    registry.register(ToolDef(
        name="exact_tool", description="test", parameters={"type": "object", "properties": {}},
        fn=execute, permission_check=check, permission_grant=grant,
        permission_finalizer=lambda args: events.append("finalize"),
        permission_authoritative=True, permission_yolo_auto_grant=opt_in,
    ))
    result = run(registry.execute("exact_tool", {}))
    if opt_in:
        assert result["error"] == ""
        assert events == ["check", "once", "execute", "cleanup", "finalize"]
    else:
        assert result["code"] == "approval_required"
        assert events == ["check", "finalize"]
