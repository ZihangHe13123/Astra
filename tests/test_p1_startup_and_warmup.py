"""Regression tests for P1 startup import compression and background OpenAI SDK warmup."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from agent.runtime import llm as llm_module
from agent.runtime.llm import LLMConfig, OpenAICompatibleProvider

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_clean_python(code: str, *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_backend_import_defers_optional_startup_modules() -> None:
    expected_unloaded = [
        "openai",
        "aiohttp",
        "bs4",
        "markdownify",
        "PIL",
        "agent.mail163",
        "agent.mail163.service",
        "agent.channels.onebot",
        "agent.channels.weixin",
        "agent.cli.diagnostics",
        "agent.runtime.vision_preprocessor",
        "agent.launcher.installation",
        "agent.launcher.common",
    ]
    if sys.platform != "darwin":
        expected_unloaded.append("agent.runtime.tools.computer")

    proc = _run_clean_python(
        "import json, sys, agent.cli.backend\n"
        f"watch = {expected_unloaded!r}\n"
        "loaded = [name for name in watch if name in sys.modules]\n"
        "print(json.dumps(loaded))\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == []


def test_openai_warmup_in_background_loads_once_on_daemon_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "_OPENAI_MODULE", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "AsyncOpenAI", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "DefaultAsyncHttpxClient", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "_OPENAI_WARMUP_THREAD", None)

    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, bool]] = []

    def fake_ensure_loaded(*, only_if_imported: bool = False) -> None:
        assert not only_if_imported
        thread = threading.current_thread()
        calls.append((thread.name, thread.daemon))
        entered.set()
        assert release.wait(timeout=5)
        llm_module._OPENAI_MODULE = types.SimpleNamespace()
        llm_module.AsyncOpenAI = object()
        llm_module.DefaultAsyncHttpxClient = object()

    monkeypatch.setattr(llm_module, "_ensure_openai_loaded", fake_ensure_loaded)

    setup_only = OpenAICompatibleProvider(
        LLMConfig(model="setup", base_url="http://127.0.0.1:9/v1", api_key="", connection_required=True)
    )
    assert setup_only.warmup_in_background() is False
    assert calls == []

    provider = OpenAICompatibleProvider(
        LLMConfig(model="deepseek-flash", base_url="http://127.0.0.1:9/v1", api_key="k")
    )
    assert provider.warmup_in_background() is True
    assert entered.wait(timeout=5)
    # Concurrent call while warmup is active is a no-op.
    assert provider.warmup_in_background() is False

    thread = llm_module._OPENAI_WARMUP_THREAD
    assert thread is not None
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    assert calls == [("astra-openai-warmup", True)]
    # Subsequent call after SDK is loaded is a no-op.
    assert provider.warmup_in_background() is False


def test_openai_warmup_skips_when_load_lock_busy_or_symbols_monkeypatched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "_OPENAI_MODULE", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "_OPENAI_WARMUP_THREAD", None)
    monkeypatch.setattr(llm_module, "AsyncOpenAI", object())
    monkeypatch.setattr(llm_module, "DefaultAsyncHttpxClient", object())

    provider = OpenAICompatibleProvider(
        LLMConfig(model="deepseek-flash", base_url="http://127.0.0.1:9/v1", api_key="k")
    )
    assert provider.warmup_in_background() is False
    assert llm_module._OPENAI_WARMUP_THREAD is None

    # When symbols are unset but another thread is already importing openai under _OPENAI_LOAD_LOCK,
    # warmup_in_background returns False without spawning a redundant thread.
    monkeypatch.setattr(llm_module, "AsyncOpenAI", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "DefaultAsyncHttpxClient", llm_module._OPENAI_UNSET)

    held = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with llm_module._OPENAI_LOAD_LOCK:
            held.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    try:
        assert held.wait(timeout=5)
        assert provider.warmup_in_background() is False
        assert llm_module._OPENAI_WARMUP_THREAD is None
    finally:
        release.set()
        holder.join(timeout=5)


def test_openai_warmup_swallows_unexpected_background_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "_OPENAI_MODULE", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "AsyncOpenAI", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "DefaultAsyncHttpxClient", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "_OPENAI_WARMUP_THREAD", None)

    attempts = 0

    def failing_once(*, only_if_imported: bool = False) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient import failure")
        llm_module._OPENAI_MODULE = types.SimpleNamespace()
        llm_module.AsyncOpenAI = object()
        llm_module.DefaultAsyncHttpxClient = object()

    monkeypatch.setattr(llm_module, "_ensure_openai_loaded", failing_once)

    provider = OpenAICompatibleProvider(
        LLMConfig(model="deepseek-flash", base_url="http://127.0.0.1:9/v1", api_key="k")
    )
    assert provider.warmup_in_background() is True
    thread = llm_module._OPENAI_WARMUP_THREAD
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert attempts == 1
    assert llm_module._OPENAI_MODULE is llm_module._OPENAI_UNSET

    # A later warmup or explicit load can still succeed.
    assert provider.warmup_in_background() is True
    thread2 = llm_module._OPENAI_WARMUP_THREAD
    assert thread2 is not None
    thread2.join(timeout=5)
    assert attempts == 2
    assert llm_module._OPENAI_MODULE is not llm_module._OPENAI_UNSET


def test_openai_warmup_handles_thread_start_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "_OPENAI_MODULE", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "AsyncOpenAI", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "DefaultAsyncHttpxClient", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "_OPENAI_WARMUP_THREAD", None)

    real_thread = llm_module.threading.Thread

    class FailingStartThread(real_thread):
        def start(self) -> None:
            if self.name == "astra-openai-warmup":
                raise RuntimeError("can't start new thread")
            super().start()

    monkeypatch.setattr(llm_module.threading, "Thread", FailingStartThread)

    provider = OpenAICompatibleProvider(
        LLMConfig(model="deepseek-flash", base_url="http://127.0.0.1:9/v1", api_key="k")
    )
    assert provider.warmup_in_background() is False
    assert llm_module._OPENAI_WARMUP_THREAD is None


def test_openai_warmup_shares_load_lock_with_concurrent_first_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "_OPENAI_MODULE", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "APIError", llm_module._UnloadedAPIError)
    monkeypatch.setattr(llm_module, "APITimeoutError", llm_module._UnloadedAPITimeoutError)
    monkeypatch.setattr(llm_module, "BadRequestError", llm_module._UnloadedBadRequestError)
    monkeypatch.setattr(llm_module, "RateLimitError", llm_module._UnloadedRateLimitError)
    monkeypatch.setattr(llm_module, "AsyncOpenAI", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "DefaultAsyncHttpxClient", llm_module._OPENAI_UNSET)
    monkeypatch.setattr(llm_module, "_OPENAI_WARMUP_THREAD", None)

    import_started = threading.Event()
    release_import = threading.Event()
    import_count = 0

    class FakeHttpClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_openai = types.SimpleNamespace(
        APIError=type("APIError", (Exception,), {}),
        APITimeoutError=type("APITimeoutError", (Exception,), {}),
        BadRequestError=type("BadRequestError", (Exception,), {}),
        RateLimitError=type("RateLimitError", (Exception,), {}),
        AsyncOpenAI=FakeAsyncOpenAI,
        DefaultAsyncHttpxClient=FakeHttpClient,
    )

    real_import_module = llm_module.importlib.import_module

    def gated_import_module(name: str, package: str | None = None):
        nonlocal import_count
        if name == "openai":
            import_count += 1
            import_started.set()
            assert release_import.wait(timeout=5)
            return fake_openai
        return real_import_module(name, package)

    monkeypatch.setattr(llm_module.importlib, "import_module", gated_import_module)

    provider = OpenAICompatibleProvider(
        LLMConfig(model="deepseek-flash", base_url="http://127.0.0.1:9/v1", api_key="k")
    )

    async def exercise() -> None:
        assert provider.warmup_in_background() is True
        assert import_started.wait(timeout=5)
        warm_task = asyncio.create_task(provider._warm_client_if_needed())
        await asyncio.sleep(0.05)
        assert not warm_task.done()
        release_import.set()
        await warm_task

    asyncio.run(exercise())
    thread = llm_module._OPENAI_WARMUP_THREAD
    if thread is not None:
        thread.join(timeout=5)
    assert import_count == 1
    assert isinstance(provider._client_instance, FakeAsyncOpenAI)


def test_react_agent_defers_vision_preprocessor_on_text_turns(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    other_dir = tmp_path / "other"
    workdir.mkdir()
    other_dir.mkdir()
    stale_staging = workdir / ".astra" / "image-cache" / "tiles" / ".staging" / ("a" * 32)
    stale_staging.mkdir(parents=True)
    stale_source = stale_staging / "source.png"
    stale_source.write_bytes(b"crashed inline source bytes")
    code = f"""
import asyncio, os, sys
from pathlib import Path
from types import SimpleNamespace

os.chdir({str(workdir)!r})

from agent.core.msg import ContentBlock, Msg
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.vision_policy import VisionPreprocessPolicy

provider_calls = 0

class TextOnlyProvider:
    async def chat_stream(self, messages, tools=None, tool_choice=None, **kwargs):
        global provider_calls
        provider_calls += 1
        yield {{"type": "text", "content": "hello"}}
        yield {{"type": "done", "content": "hello", "usage": {{"prompt_tokens": 5, "completion_tokens": 2}}}}

config = LLMConfig(
    model="deepseek-flash",
    base_url="http://127.0.0.1:9/v1",
    api_key="k",
    capabilities=frozenset({{"tools", "vision", "streaming"}}),
    vision_preprocess=VisionPreprocessPolicy(strategy="tiled"),
)
llm = LLMClient(config, provider=TextOnlyProvider())
agent = ReActAgent("test", llm, ToolRegistry())
agent.context.set_session(str(Path({str(tmp_path)!r}) / "s.json"))

# Stale crash staging is cleaned on ReActAgent startup without loading PIL or vision_preprocessor.
assert not Path({str(stale_source)!r}).exists()
assert not (Path({str(workdir)!r}) / ".astra" / "image-cache" / "tiles" / ".staging").exists()
assert "agent.runtime.vision_preprocessor" not in sys.modules
assert "PIL" not in sys.modules

async def run_text_turn():
    msg = Msg(sender="user", role="user", content=[ContentBlock.text("just text")])
    events = [ev async for ev in agent.reply_stream(msg)]
    return events

events = asyncio.run(run_text_turn())
assert provider_calls == 1, events
assert any(ev.get("type") == "done" for ev in events), events
assert "agent.runtime.vision_preprocessor" not in sys.modules
assert "PIL" not in sys.modules

# Path.cwd() was captured at __init__ time, not on first lazy access.
os.chdir({str(other_dir)!r})
vp = agent.vision_preprocessor
assert vp.cache_root == Path({str(workdir)!r}) / ".astra" / "image-cache" / "tiles"
assert "agent.runtime.vision_preprocessor" in sys.modules

# Setter + release_request teardown + VisionPreprocessError handling still work.
released = []
from agent.runtime.vision_preprocessor import VisionPreprocessError
def failing_prepare(*args, **kwargs):
    raise VisionPreprocessError("bad local image")
agent.vision_preprocessor = SimpleNamespace(
    prepare_blocks=failing_prepare,
    validate_provider_prompt=lambda *_a, **_k: (0, 0),
    release_request=lambda sid, rid: released.append((sid, rid)),
)
async def run_failing_vision_turn():
    msg = Msg(
        sender="user",
        role="user",
        content=[ContentBlock.text("see"), ContentBlock.image_url("data:image/png;base64,AAAA")],
    )
    return [ev async for ev in agent.reply_stream(msg)]

vision_events = asyncio.run(run_failing_vision_turn())
assert any(ev.get("code") == "vision_preprocess_failed" for ev in vision_events), vision_events
assert len(released) == 1
print("OK")
"""
    proc = _run_clean_python(code)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_shared_cache_first_image_turn_does_not_delete_sibling_inflight_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    from io import BytesIO
    from PIL import Image
    from agent.core.msg import ContentBlock, Msg
    from agent.runtime.llm import LLMClient
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.registry import ToolRegistry
    from agent.runtime.vision_policy import VisionPreprocessPolicy
    from agent.runtime import vision_preprocessor as vp_module

    buf = BytesIO()
    Image.new("RGB", (10, 10), (12, 34, 56)).save(buf, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

    class EchoVisionProvider:
        async def chat_stream(self, messages, tools=None, tool_choice=None, **kwargs):
            yield {"type": "chunk", "content": "seen"}
            yield {"type": "done", "content": "seen", "usage": {"prompt_tokens": 10, "completion_tokens": 2}}

    def make_agent(name: str, cache_dir: Path) -> ReActAgent:
        cfg = LLMConfig(
            model="deepseek-flash",
            base_url="http://127.0.0.1:9/v1",
            api_key="k",
            capabilities=frozenset({"tools", "vision", "streaming"}),
            vision_preprocess=VisionPreprocessPolicy(strategy="tiled"),
        )
        ag = ReActAgent(name, LLMClient(cfg, provider=EchoVisionProvider()), ToolRegistry(), vision_cache_root=cache_dir)
        ag.context.set_session(str(tmp_path / f"{name}.json"))
        return ag

    shared_cache = tmp_path / "shared-tiles"
    agent_a = make_agent("agent-a", shared_cache)
    agent_b = make_agent("agent-b", shared_cache)
    assert agent_a._vision_staging_swept is True
    assert agent_b._vision_staging_swept is True
    assert agent_a._vision_preprocessor is None
    assert agent_b._vision_preprocessor is None

    orig_materialize = vp_module.VisionPreprocessor._materialize_data_url
    interleaved_b_events: list[dict] = []
    triggered = False

    def interleaving_materialize(self, url: str, *, validated=None) -> Path:
        nonlocal triggered
        path = orig_materialize(self, url, validated=validated)
        if not triggered:
            triggered = True
            assert path.is_file()
            assert agent_b._vision_preprocessor is None

            async def run_b() -> list[dict]:
                msg_b = Msg(
                    sender="user",
                    role="user",
                    content=[ContentBlock.text("b look"), ContentBlock.image_url(data_url)],
                )
                return [ev async for ev in agent_b.reply_stream(msg_b)]

            # Agent B's first image turn runs right after Agent A wrote its staged source.png.
            def run_b_in_thread() -> None:
                interleaved_b_events.extend(asyncio.run(run_b()))

            worker = threading.Thread(target=run_b_in_thread)
            worker.start()
            worker.join(timeout=10)
            assert not worker.is_alive()
            assert path.is_file(), "Agent B's first VisionPreprocessor init must not delete Agent A's active source.png"
        return path

    monkeypatch.setattr(vp_module.VisionPreprocessor, "_materialize_data_url", interleaving_materialize)

    async def run_a() -> list[dict]:
        msg_a = Msg(
            sender="user",
            role="user",
            content=[ContentBlock.text("a look"), ContentBlock.image_url(data_url)],
        )
        return [ev async for ev in agent_a.reply_stream(msg_a)]

    events_a = asyncio.run(run_a())
    assert triggered is True
    assert not any(ev.get("code") == "vision_preprocess_failed" for ev in interleaved_b_events), interleaved_b_events
    assert any(ev.get("type") == "vision_preprocess" for ev in interleaved_b_events), interleaved_b_events
    assert any(ev.get("type") == "chunk" and ev.get("content") == "seen" for ev in interleaved_b_events), interleaved_b_events
    assert any(ev.get("type") == "done" for ev in interleaved_b_events), interleaved_b_events
    assert not any(ev.get("code") == "vision_preprocess_failed" for ev in events_a), events_a
    assert any(ev.get("type") == "vision_preprocess" for ev in events_a), events_a
    assert any(ev.get("type") == "chunk" and ev.get("content") == "seen" for ev in events_a), events_a
    assert any(ev.get("type") == "done" for ev in events_a), events_a

    # When startup sweep fails on an unsafe staging entry, VisionPreprocessor retries on construction.
    unsafe_cache = tmp_path / "unsafe-tiles"
    bad_entry = unsafe_cache / ".staging" / "not-a-valid-32-hex-dir"
    bad_entry.mkdir(parents=True)
    agent_unsafe = make_agent("agent-unsafe", unsafe_cache)
    assert agent_unsafe._vision_staging_swept is False
    assert agent_unsafe.vision_preprocessor._cleanup_unhealthy is True


def test_transient_startup_staging_failure_retries_after_text_turn_without_loading_pil(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "transient-tiles"
    stale_dir = cache_dir / ".staging" / ("c" * 32)
    stale_dir.mkdir(parents=True)
    stale_source = stale_dir / "source.png"
    stale_source.write_bytes(b"crashed inline source bytes")

    code = f"""
import asyncio, sys
from pathlib import Path

from agent.core.msg import ContentBlock, Msg
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.react import ReActAgent
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.vision_policy import VisionPreprocessPolicy

cache_dir = Path({str(cache_dir)!r})
stale_source = Path({str(stale_source)!r})

orig_iterdir = Path.iterdir
failed_once = False
def flaky_iterdir(self):
    global failed_once
    if not failed_once and self.name == ".staging":
        failed_once = True
        raise PermissionError("transient directory read error at startup")
    return orig_iterdir(self)

Path.iterdir = flaky_iterdir
try:
    class TextProvider:
        async def chat_stream(self, messages, tools=None, tool_choice=None, **kwargs):
            yield {{"type": "chunk", "content": "ok"}}
            yield {{"type": "done", "content": "ok", "usage": {{"prompt_tokens": 5, "completion_tokens": 1}}}}

    cfg = LLMConfig(
        model="deepseek-flash",
        base_url="http://127.0.0.1:9/v1",
        api_key="k",
        capabilities=frozenset({{"tools", "vision", "streaming"}}),
        vision_preprocess=VisionPreprocessPolicy(strategy="tiled"),
    )
    agent = ReActAgent(
        "transient-agent",
        LLMClient(cfg, provider=TextProvider()),
        ToolRegistry(),
        vision_cache_root=cache_dir,
    )
    agent.context.set_session(str(Path({str(tmp_path)!r}) / "transient.json"))
finally:
    Path.iterdir = orig_iterdir

assert failed_once is True
assert agent._vision_staging_swept is False
assert stale_source.exists()

async def run_text():
    msg = Msg(sender="user", role="user", content=[ContentBlock.text("hello")])
    return [ev async for ev in agent.reply_stream(msg)]

events = asyncio.run(run_text())
assert any(ev.get("type") == "done" for ev in events), events
assert not stale_source.exists()
assert not (cache_dir / ".staging").exists()
assert agent._vision_staging_swept is True
assert agent._vision_preprocessor is None
assert "agent.runtime.vision_preprocessor" not in sys.modules
assert "PIL" not in sys.modules
print("OK")
"""
    proc = _run_clean_python(code)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_mail_tool_registration_defers_mail163_until_invoked(tmp_path: Path) -> None:
    code = f"""
import asyncio, sys
from pathlib import Path
from agent.runtime.tools.mail import SETUP, register_mail_tool
from agent.runtime.tools.registry import ToolRegistry

reg = ToolRegistry()
register_mail_tool(reg, project_root=Path({str(tmp_path)!r}))
assert "agent.mail163" not in sys.modules
assert "agent.mail163.service" not in sys.modules

tool = reg.get("mail")
assert tool is not None

async def call_mail():
    try:
        await tool.fn(action="status")
    except ValueError as exc:
        return str(exc)
    raise AssertionError("expected ValueError")

msg = asyncio.run(call_mail())
assert msg == SETUP
assert "agent.mail163.service" in sys.modules
print("OK")
"""
    env_extra = {
        "ASTRA_HOME": str(tmp_path / "home"),
        "ASTRA_ENV_FILE": str(tmp_path / "no.env"),
        "ASTRA_MAIL_ADDRESS": "",
        "ASTRA_MAIL_PASSWORD": "",
        "ASTRA_MAIL_ACCOUNTS": "",
    }
    proc = _run_clean_python(code, env_extra=env_extra)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_channels_and_weixin_commands_defer_adapters_when_disabled(tmp_path: Path) -> None:
    home = tmp_path / "home"
    channels_cfg = tmp_path / "channels.json"
    channels_cfg.write_text("{}", encoding="utf-8")
    code = f"""
import json, sys
from pathlib import Path
from agent.channels import ChannelManager, ChannelsConfig
from agent.cli.weixin_commands import WeixinCommands

events = []
async def handler(msg, sink):
    return None

manager = ChannelManager(ChannelsConfig(), handler)
assert manager.adapters == []
assert manager.weixin() is None

commands = WeixinCommands(lambda: manager, events.append)
ev = commands.status_event()
assert ev["type"] == "weixin_status"
assert ev["enabled"] is False
assert ev["logged_in"] is False
assert ev["account"] == ""
assert ev["state"] == "off"
commands.send_status()
commands.tick()

assert "agent.channels.onebot" not in sys.modules
assert "agent.channels.weixin" not in sys.modules

# Once an account.json exists, status_event lazily loads WeixinStore and masks account_id.
acct_dir = Path({str(home)!r}) / "channels" / "weixin"
acct_dir.mkdir(parents=True, exist_ok=True)
(acct_dir / "account.json").write_text(
    json.dumps({{"account_id": "wx_bot_12345", "token": "secret-tok"}}),
    encoding="utf-8",
)
ev2 = commands.status_event()
assert ev2["logged_in"] is True
assert ev2["account"] == "wx_b…"
assert "agent.channels.weixin" in sys.modules
print("OK")
"""
    proc = _run_clean_python(
        code,
        env_extra={"ASTRA_HOME": str(home), "ASTRA_CHANNEL_CONFIG": str(channels_cfg)},
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_paths_and_source_discover_defer_importlib_metadata() -> None:
    code = """
import sys
from agent.runtime.paths import state_dir

_ = state_dir()
assert "agent.launcher.installation" not in sys.modules
assert "importlib.metadata" not in sys.modules

from agent.launcher.installation import discover
install = discover()
assert install.kind == "source"
assert "importlib.metadata" not in sys.modules
print("OK")
"""
    proc = _run_clean_python(code)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "OK"


def test_backend_warms_openai_after_gui_ready_without_blocking_startup(tmp_path: Path) -> None:
    import time

    marker = tmp_path / "openai-imported.txt"
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text(
        "import sys, threading, time\n"
        "from pathlib import Path\n"
        f"marker = Path({str(marker)!r})\n"
        "class WatchOpenAI:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'openai' and not marker.exists():\n"
        "            t = threading.current_thread()\n"
        "            marker.write_text(f'{t.name}|{t.daemon}', encoding='utf-8')\n"
        "        return None\n"
        "sys.meta_path.insert(0, WatchOpenAI())\n",
        encoding="utf-8",
    )

    models = tmp_path / "models.yaml"
    models.write_text(
        "version: 1\nmodels:\n  fixture:\n    provider: openai-compatible\n"
        "    base_url: http://127.0.0.1:9/v1\n    api_key_env: ''\n"
        "    context_limit: 32768\n    capabilities: [tools, streaming]\n",
        encoding="utf-8",
    )
    channels = tmp_path / "channels.json"
    channels.write_text("{}", encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": os.pathsep.join([str(hook), str(PROJECT_ROOT)]),
            "PYTHONUNBUFFERED": "1",
            "ASTRA_HOME": str(tmp_path / "home"),
            "AGENT_SESSION_DIR": str(tmp_path / "sessions"),
            "AGENT_SESSION": "p1-warmup-test",
            "AGENT_MCP_CONFIG": str(tmp_path / "no-mcp.json"),
            "AGENT_MODELS_FILE": str(models),
            "AGENT_USER_MODELS_FILE": str(tmp_path / "no-models.yaml"),
            "ASTRA_CHANNEL_CONFIG": str(channels),
            "ASTRA_ENV_FILE": str(tmp_path / "no-env"),
            "LLM_MODEL": "fixture",
            "SANDBOX_DOCKER": "false",
            "ASTRA_UI_SURFACE": "gui",
            "ASTRA_GUI_DISABLE_APPSHOT": "1",
            "LEARNING_REVIEW_AUTO": "0",
            "ASTRA_CONTEXT_INDEX_EMBEDDING": "off",
        }
    )

    output = tmp_path / "stdout.jsonl"
    stderr_log = tmp_path / "stderr.log"

    def events() -> list[dict]:
        return [
            json.loads(line)
            for line in output.read_bytes().split(b"\n")[:-1]
            if line.startswith(b"{")
        ]

    with output.open("wb") as stdout, stderr_log.open("wb") as stderr:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent.cli.backend"],
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            deadline = time.monotonic() + 20
            while not any(e.get("type") == "gui_ready" for e in events()) and proc.poll() is None:
                assert time.monotonic() < deadline, stderr_log.read_text(encoding="utf-8", errors="replace")
                time.sleep(0.02)
            assert any(e.get("type") == "gui_ready" for e in events()), stderr_log.read_text(
                encoding="utf-8", errors="replace"
            )
            # Startup reached gui_ready before openai was imported.
            assert not marker.exists()

            # Staying idle triggers background warmup on a daemon thread without any user turn.
            warmup_deadline = time.monotonic() + 10
            while not marker.exists() and proc.poll() is None and time.monotonic() < warmup_deadline:
                time.sleep(0.05)
            assert marker.exists(), stderr_log.read_text(encoding="utf-8", errors="replace")
            assert marker.read_text(encoding="utf-8") == "astra-openai-warmup|True"
        finally:
            try:
                proc.communicate(b'{"type":"exit"}\n', timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                raise
    assert proc.returncode == 0, stderr_log.read_text(encoding="utf-8", errors="replace")

    # Even if OS thread creation fails for the optional warmup thread, /model succeeds and backend stays alive.
    (hook / "sitecustomize.py").write_text(
        "import threading\n"
        "_orig_start = threading.Thread.start\n"
        "def _guarded_start(self):\n"
        "    if getattr(self, 'name', '') == 'astra-openai-warmup':\n"
        "        raise RuntimeError('injected warmup thread start failure')\n"
        "    return _orig_start(self)\n"
        "threading.Thread.start = _guarded_start\n",
        encoding="utf-8",
    )
    models.write_text(
        "version: 1\nmodels:\n"
        "  fixture:\n    provider: openai-compatible\n"
        "    base_url: http://127.0.0.1:9/v1\n    api_key_env: ''\n"
        "    context_limit: 32768\n    capabilities: [tools, streaming]\n"
        "  fixture-next:\n    provider: openai-compatible\n"
        "    base_url: http://127.0.0.1:9/v1\n    api_key_env: ''\n"
        "    context_limit: 32768\n    capabilities: [tools, streaming]\n",
        encoding="utf-8",
    )
    output2 = tmp_path / "stdout2.jsonl"
    stderr_log2 = tmp_path / "stderr2.log"

    def events2() -> list[dict]:
        return [
            json.loads(line)
            for line in output2.read_bytes().split(b"\n")[:-1]
            if line.startswith(b"{")
        ]

    with output2.open("wb") as stdout, stderr_log2.open("wb") as stderr:
        proc2 = subprocess.Popen(
            [sys.executable, "-m", "agent.cli.backend"],
            cwd=PROJECT_ROOT,
            env=env,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
        )
        try:
            assert proc2.stdin is not None
            deadline = time.monotonic() + 20
            while not any(e.get("type") == "gui_ready" for e in events2()) and proc2.poll() is None:
                assert time.monotonic() < deadline, stderr_log2.read_text(encoding="utf-8", errors="replace")
                time.sleep(0.02)
            proc2.stdin.write(b'{"type":"command","cmd":"/model fixture-next","request_id":"sel-1"}\n')
            proc2.stdin.flush()
            switch_deadline = time.monotonic() + 10
            while (
                not any(
                    e.get("type") == "model_selection_result" and e.get("request_id") == "sel-1"
                    for e in events2()
                )
                and proc2.poll() is None
            ):
                assert time.monotonic() < switch_deadline, stderr_log2.read_text(encoding="utf-8", errors="replace")
                time.sleep(0.05)
            sel = next(
                e
                for e in events2()
                if e.get("type") == "model_selection_result" and e.get("request_id") == "sel-1"
            )
            assert sel.get("error") == "", sel
            assert "Model connection switched to: fixture-next" in sel.get("output", ""), sel
        finally:
            try:
                proc2.communicate(b'{"type":"exit"}\n', timeout=20)
            except subprocess.TimeoutExpired:
                proc2.kill()
                proc2.wait(timeout=5)
                raise
    assert proc2.returncode == 0, stderr_log2.read_text(encoding="utf-8", errors="replace")


