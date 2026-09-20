"""AgentTUI — RichLog + Static 流式行，不闪屏"""

from agent.runtime.paths import sessions_dir

import asyncio
import os
import platform
import re
from pathlib import Path

from rich.text import Text as RichText
from textual.app import App, ComposeResult
from textual.widgets import Header, Input, RichLog, Static

from ..core.bus import MessageBus
from ..core.msg import ContentBlock, Msg
from ..runtime.context_index import create_context_index_broker
from ..runtime.llm import LLMClient, LLMConfig
from ..runtime.memory import MemoryStore
from ..runtime.react import ReActAgent
from ..runtime.skills import SkillStore
from ..runtime.tools.activity import register_activity_tools
from ..runtime.tools.code import register_code_tools
from ..runtime.tools.computer import register_local_computer_runtime
from ..runtime.tools.context_index import register_context_index_tools
from ..runtime.tools.files import register_file_tools
from ..runtime.tools.git import register_git_tools
from ..runtime.tools.image import register_image_tools, select_vision_tiles_from_holder
from ..runtime.tools.memory import register_memory_tools
from ..runtime.tools.registry import ToolRegistry
from ..runtime.tools.session_recall import register_session_recall_tools
from ..runtime.tools.skills import register_skill_tools
from ..runtime.tools.time import register_time_tools
from ..runtime.tools.web import register_web_tools
from ..sandbox.docker import DockerSandbox
from ..sandbox.local import LocalSandbox
from ..sandbox.router import SandboxRouter
from agent.cli.browser_commands import execute_browser_command
from .computer_commands import execute_computer_command
from .context_index_commands import execute_context_index_command
from .context_index_preferences import load_context_index_preferences
from .models import context_limit_for_model, prompt_token_budget
from .persona_preferences import startup_persona
from .render import render_tool_block, strip_ansi
from .sandbox_preferences import execute_sandbox_command, resolve_startup_sandbox_mode
from .vision_tile_preferences import (
    execute_vision_tiles_command,
    load_vision_tiles_enabled,
)

CONTEXT_LIMIT = 128_000
_MODEL_CONTEXT_MAP = {
    "deepseek-flash":    1_000_000,
    "deepseek-v4-pro":    1_000_000,
    "Qwen3.6-35B-A3B":     196_608,
}

def _resolve_context_limit_simple(model: str) -> int:
    import os
    env_limit = os.getenv("LLM_CONTEXT_LIMIT", "").strip()
    if env_limit and env_limit.isdigit():
        return int(env_limit)
    return context_limit_for_model(model)
SESSION_DIR = sessions_dir()
SESSION_DEFAULT = SESSION_DIR / "default.json"


# ── Markdown → 纯文本 ──────────────────────────────────────────

def _md_plain(text: str) -> str:
    text = re.sub(r'^(#{1,6})\s+(.+?)(?:\s+#{1,6})?$',
        lambda m: _render_hdr(len(m.group(1)), m.group(2).strip()),
        text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'`([^`]+)`', r"'\1'", text)
    text = re.sub(r'```\w*\n?', '', text)
    text = re.sub(r'^(\s*)-\s+', r'\1• ', text, flags=re.MULTILINE)
    return text

def _render_hdr(level: int, title: str) -> str:
    bars = {1:'─',2:'──',3:'───',4:'────',5:'─────',6:'──────'}
    b = bars.get(level, '───')
    return f" {b} {title} {b} "


# ── 颜色映射 ──────────────────────────────────────────────────

COLORS = {
    "user":      "cyan",
    "assistant": "magenta",
    "thinking":  "grey50",
    "error":     "red",
    "system":    "grey50",
    "tool":      "grey50",
}
PREFIX = {
    "user":      "You",
    "assistant": "Lyra",
    "thinking":  "▸",
    "error":     "⚠",
    "system":    "·",
    "tool":      "",
}

def _fmt_label(kind: str) -> str:
    p = PREFIX.get(kind, "")
    return f"{p}: " if p else ""


# ── StatusBar ─────────────────────────────────────────────────

class StatusBar(Static):
    @staticmethod
    def _fmt_size(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_048_576:.1f}M"
        return f"{n / 1024:.1f}K" if n >= 1024 else str(n)

    @staticmethod
    def _ctx_color(pct: float) -> str:
        if pct > 85:
            return "bold red"
        if pct > 60:
            return "yellow"
        return "green"

    def refresh_bar(self, agent: ReActAgent, status: str = "ready"):
        ctx = agent.context
        used = ctx.estimate_prompt_tokens()
        limit = ctx.max_prompt_tokens
        pct = (used / limit * 100) if limit > 0 else 0.0
        status_color = "green" if status == "ready" else "bold yellow"
        bar = (
            f"[{status_color}]● {status}[/{status_color}]"
            f" · [bold]{agent.llm.config.model}[/bold]"
            f" · [dim]ctx[/dim] [{self._ctx_color(pct)}]{self._fmt_size(used)}/{self._fmt_size(limit)} {pct:.0f}%[/{self._ctx_color(pct)}]"
            f" · [dim]Σ {self._fmt_size(ctx.total_tokens)}[/dim]"
        )
        self.update(RichText.from_markup(bar))


# ── TUI App ───────────────────────────────────────────────────

class AgentTUI(App):
    TITLE = "Agent System v0.2"
    BINDINGS = [
        ("ctrl+shift+c", "copy_all", "Copy all"),
        ("escape", "cancel_stream", "Cancel"),
        ("up", "history_up", "Previous input"),
        ("down", "history_down", "Next input"),
    ]
    CSS = """
    #chat {
        height: 1fr;
        border: none;
    }
    #stream-line {
        height: auto;
        max-height: 5;
    }
    StatusBar {
        background: $surface;
        color: $text;
        height: 1;
        padding: 0 1;
        text-style: bold;
    }
    Input {
        dock: bottom;
        margin: 0 1 1 1;
    }
    """

    def __init__(self, agent: ReActAgent, bus: MessageBus):
        super().__init__()
        self.agent = agent
        self.bus = bus
        self._task = None
        self._plain_buf = ""   # 纯文本副本，供复制
        self._history: list[str] = []
        self._history_idx = len(self._history)

    # ── 写 RichLog（完整行） ──────────────────────────────────

    def _write_rich(self, text: str, kind: str = "assistant"):
        """写一行到 RichLog"""
        chat = self.query_one("#chat", RichLog)
        clean = _md_plain(strip_ansi(text))
        label = _fmt_label(kind)
        color = COLORS.get(kind, "")

        t = RichText(f"{label}{clean}")
        if color:
            t.stylize(color)
        chat.write(t)
        self._plain_buf += f"{label}{clean}\n"

    # ── 写 Static（流式行） ──────────────────────────────────

    def _stream_begin(self, text: str, kind: str = "assistant"):
        """开始流式行：清空 Static + 写入开头"""
        st = self.query_one("#stream-line", Static)
        clean = _md_plain(strip_ansi(text))
        label = _fmt_label(kind)
        color = COLORS.get(kind, "")
        t = RichText(f"{label}{clean}")
        if color:
            t.stylize(color)
        st.update(t)

    def _stream_append(self, chunk: str):
        """追加到流式行尾部"""
        st = self.query_one("#stream-line", Static)
        clean = _md_plain(strip_ansi(chunk))
        color = COLORS.get("assistant", "")
        current = st.renderable or RichText("")
        if isinstance(current, RichText):
            current.append(RichText(clean))
        else:
            t = RichText(f"{current}{clean}")
            if color:
                t.stylize(color)
            st.update(t)

    def _stream_commit(self, kind: str = "assistant"):
        """流式行完成：写入 RichLog + 清空 Static"""
        st = self.query_one("#stream-line", Static)
        content = st.renderable
        if content:
            # content 可能是 RichText 或 str
            if isinstance(content, RichText):
                text_content = content.plain
            else:
                text_content = str(content)
            if text_content.strip():
                self._write_rich(text_content, kind=kind)
            st.update("")

    # ── 生命周期 ─────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield RichLog(id="chat", highlight=True, markup=True, wrap=True)
        yield Static(id="stream-line")
        yield StatusBar(id="status-bar")
        yield Input(placeholder="Type your message here...", id="input")

    def action_copy_all(self):
        """Ctrl+Shift+C: 复制全部对话到剪贴板"""
        self.copy_to_clipboard(self._plain_buffer.strip())

    def action_cancel_stream(self):
        """Escape: 取消当前流式回复"""
        if self._task and not self._task.done():
            self._task.cancel()
            self._write_line("(cancelled)", color="grey50")

    def action_history_up(self):
        inp = self.query_one("#input", Input)
        if self._history_idx == len(self._history):
            self._draft = inp.value
        if self._history_idx > 0:
            self._history_idx -= 1
            inp.value = self._history[self._history_idx]
            inp.cursor_position = len(inp.value)

    def action_history_down(self):
        inp = self.query_one("#input", Input)
        if self._history_idx < len(self._history) - 1:
            self._history_idx += 1
            inp.value = self._history[self._history_idx]
        else:
            self._history_idx = len(self._history)
            inp.value = getattr(self, "_draft", "")
            self._draft = ""
        inp.cursor_position = len(inp.value)

    def on_mount(self):
        for msg in self.agent.context.messages:
            if msg.get("_meta", {}).get("type") == "reasoning_context":
                continue
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                self._write_rich(content, kind="user")
            elif role == "assistant":
                self._write_rich(content, kind="assistant")
        self.query_one(StatusBar).refresh_bar(self.agent)
        self.query_one("#input", Input).focus()

    # ── 输入 ─────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return
        self.query_one("#input", Input).clear()
        self._history.append(text)
        self._history_idx = len(self._history)
        if text.lower() in ("exit", "quit"):
            self.exit()
            return
        if text.startswith("/"):
            self._handle_slash(text)
            return
        self._write_rich(text, kind="user")
        self._task = asyncio.create_task(self._process_message(text))

    # ── 消息处理 ─────────────────────────────────────────────

    async def _process_message(self, text: str):
        status_bar = self.query_one(StatusBar)
        status_bar.refresh_bar(self.agent, status="thinking")
        msg = Msg(sender="user", role="user", content=[ContentBlock.text(text)])
        stream_started = False
        self._stream_text_buf = ""

        try:
            async for event in self.bus.send_stream("agent", msg):
                etype = event.get("type")

                if etype == "reasoning":
                    self._stream_text_buf += event["content"]
                    while "\n" in self._stream_text_buf:
                        idx = self._stream_text_buf.index("\n")
                        line = self._stream_text_buf[:idx]
                        self._stream_text_buf = self._stream_text_buf[idx+1:]
                        if line.strip():
                            self._write_rich(line.strip(), kind="thinking")

                elif etype == "chunk":
                    if not stream_started:
                        self._stream_begin("", kind="assistant")
                        stream_started = True
                    self._stream_append(event["content"])

                elif etype == "tool_result":
                    self._flush_reasoning()
                    self._stream_commit(kind="assistant")
                    stream_started = False
                    card = render_tool_block(
                        event.get("name", "tool"),
                        code=event.get("code", ""),
                        output=event.get("output", ""),
                        error=event.get("error", ""),
                        duration_ms=event.get("duration_ms", 0),
                    )
                    self._write_rich(card, kind="tool")

                elif etype == "vision_preprocess":
                    vision_message = event.get("message", "")
                    if vision_message:
                        self._write_rich(f"[vision] {vision_message}", kind="system")

                elif etype == "done":
                    self._flush_reasoning()
                    self._stream_commit(kind="assistant")
                    stream_started = False

        except Exception as exc:
            self._stream_commit(kind="assistant")
            self._write_rich(f"{exc}", kind="error")

        self.agent.context.save()
        status_bar.refresh_bar(self.agent, status="ready")

    def _flush_reasoning(self):
        if self._stream_text_buf.strip():
            self._write_rich(self._stream_text_buf.strip(), kind="thinking")
        self._stream_text_buf = ""

    # ── 斜杠命令 ─────────────────────────────────────────────

    def _handle_slash(self, cmd: str):
        parts = cmd.strip().split(maxsplit=1)
        command = parts[0].lower()

        if command == "/think":
            self.agent.context.show_reasoning = not self.agent.context.show_reasoning
            state = "ON" if self.agent.context.show_reasoning else "OFF"
            self._write_rich(f"Thinking display: {state}", kind="system")
        elif command == "/reset":
            self.agent.end_session("reset")
            self.agent.reset_conversation()
            self.agent.context.save()
            self.agent.begin_session()
            self.query_one("#chat", RichLog).clear()
            self._plain_buf = ""
            self._write_rich("Context cleared.", kind="system")
        elif command == "/tools":
            names = self.agent.tools.tool_names
            self._write_rich(f"Tools ({len(names)}):", kind="system")
            for n in sorted(names):
                self._write_rich(f"  {n}", kind="system")
        elif command == "/sandbox":
            router = getattr(self.agent, "_sandbox", None)
            if not isinstance(router, SandboxRouter):
                self._write_rich("Sandbox switching is unavailable.", kind="error")
            else:
                args = parts[1].split() if len(parts) > 1 else []
                output, error = execute_sandbox_command(router, args)
                self._write_rich(error or output, kind="error" if error else "system")
        elif command == "/vision-tiles":
            args = parts[1].split() if len(parts) > 1 else []
            output, error = execute_vision_tiles_command(self.agent, args)
            self._write_rich(error or output, kind="error" if error else "system")
        elif command == "/context-index":
            args = parts[1].split() if len(parts) > 1 else []
            output, error = execute_context_index_command(self.agent, args)
            self._write_rich(error or output, kind="error" if error else "system")
        elif command == "/browser":
            args = parts[1].split() if len(parts) > 1 else []

            async def run_browser_command() -> None:
                output, error = await execute_browser_command(args, self.agent.tools)
                self._write_rich(error or output, kind="error" if error else "system")

            asyncio.create_task(run_browser_command())
        elif command == "/computer":
            args = parts[1].split() if len(parts) > 1 else []

            async def run_computer_command() -> None:
                output, error = await execute_computer_command(
                    args,
                    getattr(self.agent, "_computer_runtime", None),
                )
                self._write_rich(error or output, kind="error" if error else "system")
                self.query_one(StatusBar).refresh_bar(self.agent)

            asyncio.create_task(run_computer_command())
        elif command == "/help":
            self._write_rich(
                "Commands:\n  /reset  /think  /tools  /sandbox [on|off]  "
                "/vision-tiles [on|off]  /context-index [on|off|session|all|status|why]  "
                "/browser [status|stop]  /computer [status|setup|stop]  /help  exit",
                kind="system",
            )
        else:
            self._write_rich(f"Unknown: {command}. Type /help", kind="error")

        self.query_one(StatusBar).refresh_bar(self.agent)

    async def on_unmount(self):
        self.agent.end_session("shutdown")
        computer_runtime = getattr(self.agent, "_computer_runtime", None)
        try:
            if computer_runtime is not None:
                await computer_runtime.shutdown()
        except Exception as exc:  # noqa: BLE001 - remaining shutdown cleanup must continue
            self._write_rich(
                f"Computer Use shutdown failed ({type(exc).__name__}).",
                kind="error",
            )
        finally:
            self.agent.context.save()
            await self.agent.close_external_memory()
            sandbox = getattr(self.agent, "_sandbox", None)
            close = getattr(sandbox, "close", None)
            if close is not None:
                await close()


# ── 启动 ─────────────────────────────────────────────────────

def _truthy_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in ("true", "1", "yes")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


_IS_WINDOWS = platform.system() == "Windows"


def _local_sandbox(timeout: int, workdir: str) -> LocalSandbox:
    return LocalSandbox(
        timeout=timeout,
        workdir=workdir,
        max_output_bytes=_int_env("LOCAL_SANDBOX_MAX_OUTPUT_BYTES", 200_000),
        max_memory_mb=_optional_int_env("LOCAL_SANDBOX_MAX_MEMORY_MB"),
        max_cpu_seconds=_optional_int_env("LOCAL_SANDBOX_MAX_CPU_SECONDS"),
    )


def create_agent(llm_config: LLMConfig, sandbox_timeout: int,
                 workdir: str) -> tuple:
    bus = MessageBus()
    initial_sandbox = _init_sandbox(
        resolve_startup_sandbox_mode(os.getenv("SANDBOX_DOCKER", "auto").lower()),
        sandbox_timeout,
        workdir,
    )
    sandbox = SandboxRouter(
        initial_sandbox,
        local_factory=lambda: _local_sandbox(sandbox_timeout, workdir),
        docker_factory=lambda: DockerSandbox(
            timeout=sandbox_timeout,
            workdir=workdir,
            network=_truthy_env("DOCKER_NETWORK"),
            reuse_container=_truthy_env("DOCKER_REUSE_CONTAINER"),
        ),
    )
    tools = ToolRegistry()
    memory_store = MemoryStore()
    skill_store = SkillStore()
    context_index_broker = create_context_index_broker(
        load_context_index_preferences(), workdir
    )
    context_index_broker.start_background(memory_store.path)
    agent_holder = {}
    register_code_tools(tools, sandbox)
    register_file_tools(tools, workdir=workdir, sandbox=sandbox)
    register_git_tools(tools, workdir=workdir)
    register_time_tools(tools)
    register_memory_tools(
        tools,
        memory_store,
        session_id=lambda: Path(agent_holder["agent"].context.session_path).stem if agent_holder else "default",
    )
    register_skill_tools(tools, skill_store)
    register_session_recall_tools(tools)
    register_activity_tools(tools)
    register_context_index_tools(tools, context_index_broker)
    register_web_tools(tools, sandbox)
    register_image_tools(
        tools,
        workdir=workdir,
        select_tiles=lambda tile_set_id, tile_ids: select_vision_tiles_from_holder(
            agent_holder,
            tile_set_id,
            tile_ids,
        ),
    )
    computer_runtime = register_local_computer_runtime(
        tools,
        model_capabilities=lambda: (
            agent_holder["agent"].llm.config.capabilities
            if "agent" in agent_holder
            else llm_config.capabilities
        ),
    )
    llm = LLMClient(llm_config)
    _, system_prompt, persona_state = startup_persona(os.getenv("AGENT_PERSONA"))
    agent = ReActAgent(
        name="agent",
        llm_client=llm,
        tool_registry=tools,
        system_prompt=system_prompt,
        max_iterations=max(0, _int_env("AGENT_MAX_REACT_ITERATIONS", 50)),
        tool_concurrency=max(1, int(os.getenv("TOOL_CONCURRENCY", "4"))),
        memory_store=memory_store,
        skill_store=skill_store,
        context_index_broker=context_index_broker,
    )
    agent.set_vision_tiles_enabled(load_vision_tiles_enabled())
    agent.set_persona(persona_state, system_prompt)
    agent_holder["agent"] = agent
    agent._computer_runtime = computer_runtime
    agent._sandbox = sandbox
    limit = _resolve_context_limit_simple(llm_config.model)
    agent.context.max_prompt_tokens = prompt_token_budget(
        limit, llm_config.max_tokens, model=llm_config.model, provider=llm_config.provider,
    )
    agent.llm.config.context_limit = limit
    agent.context.set_session(str(SESSION_DEFAULT))
    bus.register(agent)
    restored = agent.context.load()
    if restored:
        count = len(agent.context.messages) // 2
        print(f"  [Restored session with {count} previous exchanges]")
    return bus, agent

def _init_sandbox(mode: str, timeout: int, workdir: str):
    # Windows 上除非强制 Docker，否则用本地沙箱
    if _IS_WINDOWS and mode != "true":
        return _local_sandbox(timeout, workdir)
    if mode == "false":
        return _local_sandbox(timeout, workdir)
    try:
        import subprocess
        r = subprocess.run(["docker", "ps"], capture_output=True, timeout=10)
        if r.returncode == 0:
            sb = DockerSandbox(timeout=timeout, workdir=workdir,
                network=_truthy_env("DOCKER_NETWORK"),
                reuse_container=_truthy_env("DOCKER_REUSE_CONTAINER"))
            if mode == "true":
                return sb
            test = asyncio.run(sb.execute_python("print('ok')"))
            if test["exit_code"] == 0:
                return sb
            print("  [Docker available but Python test failed]")
        elif mode == "true":
            print("  [Docker not available (SANDBOX_DOCKER=true)]")
    except Exception:
        if mode == "true":
            print("  [Docker init failed]")
    if mode == "auto":
        print("  [Docker not available, using local sandbox]")
    return _local_sandbox(timeout, workdir)

def run_tui(llm_config: LLMConfig, sandbox_timeout: int, workdir: str):
    bus, agent = create_agent(llm_config, sandbox_timeout, workdir)
    app = AgentTUI(agent, bus)
    app.run()
