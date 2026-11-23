"""Host dispatch is an in-memory recorder. No command or Python snippet runs."""
import asyncio

import pytest

from agent.runtime.tools.code import register_code_tools
from agent.runtime.tools.grants import request_keys
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.script_ui import inline_python_action, inline_shell_action


class RecordingHost:
    def __init__(self, workdir):
        self.workdir = str(workdir)
        self.dispatched = []

    async def execute_shell_stream(self, command, environment, on_output):
        self.dispatched.append(("shell", command))
        return {"output": "fixture", "error": "", "exit_code": 0}

    async def execute_python_stream(self, code, on_output):
        self.dispatched.append(("python", code))
        return {"output": "fixture", "error": "", "exit_code": 0}

    async def execute_persistent_bash_stream(self, command, on_output):
        self.dispatched.append(("bash", command))
        return {"output": "fixture", "error": "", "exit_code": 0}


@pytest.mark.parametrize(("name", "args"), [
    ("execute_shell", {"command": """osascript -e 'tell application "System Events" to click fixtureButton'"""}),
    ("execute_shell", {"command": """osascript -l JavaScript -e 'Application("Fixture").activate()'"""}),
    ("execute_shell", {"command": """osascript -e 'tell application "Fixture" to activate'"""}),
    ("execute_python", {"code": "import pyautogui as gui\ngui.press(fixtureKey)"}),
    ("bash", {"command": "cliclick c:fixture"}),
])
def test_registered_host_ui_action_needs_each_human_once_even_in_yolo(tmp_path, name, args):
    registry = ToolRegistry()
    host = RecordingHost(tmp_path)
    register_code_tools(registry, host)
    registry.yolo = True
    registry.approved_permission_scopes.update({
        "computer-foreground:fixture:wps", "computer-write:fixture:wps",
    })
    requests = []
    answers = iter(["deny", "once", "session", "deny"])

    async def answer(request):
        requests.append(request)
        return next(answers)

    registry.set_approval_handler(answer)
    before = set(registry.approved_permission_scopes)
    def call():
        return asyncio.run(registry.execute(name, args))

    assert call().get("error_type") == "approval_denied"
    assert not host.dispatched
    registry.grants.remember("session", request_keys(name, requests[0]))
    assert not call()["error"]
    assert call().get("error_type") == "approval_denied"
    assert call().get("error_type") == "approval_denied"
    assert len(host.dispatched) == 1
    assert len(requests) == len({card["request_id"] for card in requests}) == 4
    for card in requests:
        assert card["kind"] == "script_ui_control" and card["human_only"] is True
        assert card["choices"] == ["once", "deny"] and card["yolo_bypass_allowed"] is False
        assert "焦点" in card["approval_effect"]
    assert registry.approved_permission_scopes == before


@pytest.mark.parametrize("command", ["open -a Fixture sample.txt", "pytest -q tests", "cmake --build build",
                                    """osascript -e 'tell application "System Events" to get name of every process'""",
                                    "printf '%s' 'cliclick c:fixture'"])
def test_normal_host_commands_keep_their_existing_approval_policy(tmp_path, command):
    host = RecordingHost(tmp_path)
    registry = ToolRegistry()
    register_code_tools(registry, host)
    cards = []

    async def answer(request):
        cards.append(request)
        return "deny"

    registry.set_approval_handler(answer)
    args = {"command": command}
    assert asyncio.run(registry.execute("execute_shell", args)).get("error_type") == "approval_denied"
    assert [c["kind"] for c in cards] == ["host_execution"]
    registry.yolo = True
    assert not asyncio.run(registry.execute("execute_shell", args))["error"]
    assert len(cards) == 1 and len(host.dispatched) == 1


def test_unavailable_ui_approval_fails_closed(tmp_path):
    host = RecordingHost(tmp_path)
    registry = ToolRegistry()
    register_code_tools(registry, host)
    registry.yolo = True
    result = asyncio.run(registry.execute("execute_python", {"code": "import pyautogui\npyautogui.write(fixtureText)"}))
    assert result["code"] == "approval_required" and not host.dispatched


@pytest.mark.parametrize("code", [
    "from Quartz import CGEventPost as post\npost(fixtureTap, fixtureEvent)",
    "import ApplicationServices as ax\nax.AXUIElementPerformAction(fixture, action)",
    "from ApplicationServices import AXUIElementSetAttributeValue\nAXUIElementSetAttributeValue(fixture, attr, val)",
    "from AppKit import NSRunningApplication\napp=NSRunningApplication.runningApplicationWithProcessIdentifier_(fixturePid)\napp.activateWithOptions_(0)",
])
def test_public_desktop_mutators_are_recognized_by_their_operation(code):
    assert inline_python_action(code)


@pytest.mark.parametrize("code", [
    "import pyautogui\nsize = pyautogui.size()",
    "from Quartz import CGWindowListCopyWindowInfo\nwins = CGWindowListCopyWindowInfo(flags, owner)",
    "print('CGEventPost(ignored)')",
])
def test_read_only_python_and_literal_text_do_not_request_ui_control(code):
    assert inline_python_action(code) is None


@pytest.mark.parametrize("verb", ["keystroke fixture", "key code fixture", "perform action fixtureAction of fixture",
                                  "set value of fixture to fixtureValue"])
def test_system_events_input_verbs(verb):
    assert inline_shell_action(f"""osascript -e 'tell application "System Events" to {verb}'""")


def test_jxa_system_events_accessor_distinguishes_get_and_set():
    app = """Application("System Events").processes.byName("Fixture")"""
    assert inline_shell_action(f"""osascript -l JavaScript -e '{app}.textFields[0].value()'""") is None
    assert inline_shell_action(f"""osascript -l JavaScript -e '{app}.textFields[0].value = fixtureValue'""")
