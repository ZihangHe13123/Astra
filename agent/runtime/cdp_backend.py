"""CDP-based browser backend using Chrome/Edge headless mode.

Implements the BrowserBackend protocol from browser_session.py using
Chrome's built-in headless mode (--headless=new).

Two modes of operation:
  1. Stateless: extract() / screenshot() spawn a fresh Chrome per call
  2. Interactive: CdpConnection holds a persistent Chrome + WebSocket for
     click / type / evaluate via the Chrome DevTools Protocol

Execution ladder support:
  READ_ONLY  → static HTTP (handled by legacy adapter, not this backend)
  HEADLESS   → --headless=new --dump-dom (stateless extract)
  SCREENSHOT → --headless=new --screenshot (stateless screenshot)
  INTERACTIVE→ CdpConnection + Runtime.evaluate (click/type/navigate)
  TAKEOVER   → launch headed Chrome for the user (launch_headed())

Dependencies: subprocess, optional BeautifulSoup, optional websockets (for
interactive mode).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import socket
import tempfile
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any

from .process_env import browser_child_environment, hidden_process_creationflags
from .browser_session import BackendCapabilities

logger = logging.getLogger(__name__)

# Chrome/Edge candidate paths (Windows/WSL paths, macOS bundles, then Linux)
_CHROME_CANDIDATES = [
    # Native Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    # Windows (from WSL)
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
    # macOS application bundles
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    # Linux
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
]

# Default virtual time budget (ms) for JS execution before DOM dump
_DEFAULT_VIRTUAL_TIME_BUDGET = 5000

# Default timeout for Chrome subprocess (seconds)
_DEFAULT_TIMEOUT = 30

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

_HEADED_START_PAGE = (
    "data:text/html;charset=utf-8,"
    "%3Cbody%20style%3D%22font%3A24px%20system-ui%3Bbackground%3A%2314213d%3B"
    "color%3A%23fca311%3Bpadding%3A48px%22%3EAgent%20browser%20is%20connecting...%3C%2Fbody%3E"
)


def interactive_dependency_available() -> bool:
    """Return whether the optional CDP WebSocket transport is importable."""
    return importlib.util.find_spec("websockets") is not None


def _make_temp_profile() -> str:
    """Create an ephemeral Chrome profile outside the project tree.

    Chromium may harden profile ACLs for its sandbox on Windows. Putting
    throwaway profiles under ``.astra`` can therefore make the repo
    directory impossible to clean from the parent process. Named profiles
    still live in the project; only disposable runtime profiles use TEMP.

    On WSL the profile **must** live on the Windows filesystem (e.g.
    ``%TEMP%``).  A UNC path like ``\\\\wsl.localhost\\...`` causes Chrome's
    GPU process to crash (exit_code=-2147483645) during compositing /
    screenshot because the sandbox cannot lock files on the 9p mount.
    """
    configured = os.getenv("AGENT_BROWSER_RUNTIME_DIR", "").strip()
    if configured:
        root = os.path.abspath(os.path.expanduser(configured))
    elif _is_wsl():
        # Use the Windows-side TEMP so Chrome gets a native NTFS path
        root = _get_windows_temp()
        if not root:
            root = str(Path(tempfile.gettempdir()) / "agent-lab-browser-runtime")
    else:
        root = str(Path(tempfile.gettempdir()) / "agent-lab-browser-runtime")
    os.makedirs(root, exist_ok=True)
    return tempfile.mkdtemp(prefix="profile-", dir=root)


def _to_windows_path(path: str) -> str:
    """Convert a WSL Linux path to a Windows path for Chrome.

    Chrome runs as a Windows process and cannot write to /tmp/... paths.
    Uses ``wslpath -w`` when available; falls back to the original path.
    """
    import subprocess
    if not path.startswith("/"):
        return path  # already a Windows or relative path
    try:
        result = subprocess.run(
            ["wslpath", "-w", path],
            env=browser_child_environment(), capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return path


def find_chrome() -> str:
    """Find a Chrome or Edge executable. Returns empty string if not found."""
    # Check env override first
    env_path = os.getenv("CHROME_PATH", "").strip()
    if env_path:
        if os.path.isfile(env_path) or shutil.which(env_path):
            return env_path

    for candidate in _CHROME_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return ""


def _html_to_text(html: str) -> str:
    """Convert HTML to readable plain text.

    Uses BeautifulSoup if available, otherwise falls back to a simple
    regex-based tag stripper.
    """
    if not html:
        return ""

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # Remove script, style, noscript, svg elements
        for tag in soup(["script", "style", "noscript", "svg", "head"]):
            tag.decompose()

        # Get text with newlines between block elements
        text = soup.get_text(separator="\n")
    except ImportError:
        # Fallback: strip tags with regex
        text = re.sub(r"<(script|style|noscript|svg|head)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</(p|div|h[1-6]|li|tr|blockquote)>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)

    # Clean up whitespace: collapse runs of blank lines, strip each line
    lines = [line.strip() for line in text.splitlines()]
    cleaned: list[str] = []
    prev_blank = False
    for line in lines:
        if not line:
            if not prev_blank:
                cleaned.append("")
            prev_blank = True
        else:
            cleaned.append(line)
            prev_blank = False

    return "\n".join(cleaned).strip()


# ---------------------------------------------------------------------------
# CdpConnection — persistent Chrome + WebSocket for interactive work
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _force_kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Force-stop a browser subprocess and its descendants.

    Chromium is a multi-process application. On Windows, terminating only the
    launcher PID leaves renderer/GPU processes (and sometimes the visible
    window) behind. ``taskkill /T`` is deliberately used only for a process
    that this module launched and still owns.
    """
    if proc.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill.exe",
            "/PID",
            str(proc.pid),
            "/T",
            "/F",
            env=browser_child_environment(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=hidden_process_creationflags(),
        )
        await killer.communicate()
    else:
        with suppress(ProcessLookupError, OSError):
            proc.kill()
    with suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
        await asyncio.wait_for(proc.wait(), timeout=5)


async def _open_cdp_websocket(url: str, *, max_size: int, timeout: float) -> Any:
    """Open a local DevTools socket, tolerating Chrome's startup race."""
    import websockets

    deadline = asyncio.get_running_loop().time() + max(timeout, 0.5)
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            # websockets 15 consults proxy environment variables by default.
            # CDP is always a direct local connection in this backend.
            return await websockets.connect(
                url,
                max_size=max_size,
                open_timeout=min(2, timeout),
                proxy=None,
            )
        except TypeError as exc:
            # websockets 14 connects directly and doesn't accept ``proxy``.
            if "proxy" not in str(exc):
                raise
            return await websockets.connect(
                url,
                max_size=max_size,
                open_timeout=min(2, timeout),
            )
        except Exception as exc:
            last_error = exc
            await asyncio.sleep(0.1)
    raise RuntimeError(f"DevTools WebSocket unavailable: {last_error}")


def _get_windows_gateway() -> str:
    """Get the Windows host gateway IP (for WSL NAT networking).

    In WSL2 NAT mode, the Windows host is reachable via the default
    gateway IP (typically 172.x.x.1). Returns empty string if not
    running under WSL or if detection fails.
    """
    # Cache the result — gateway doesn't change during a session
    if hasattr(_get_windows_gateway, "_cached"):
        return _get_windows_gateway._cached  # type: ignore[attr-defined]

    gateway = ""
    try:
        # Check if we're in WSL
        with open("/proc/version", "r") as f:
            if "microsoft" not in f.read().lower():
                _get_windows_gateway._cached = ""  # type: ignore[attr-defined]
                return ""
        # Get default gateway
        import subprocess
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            env=browser_child_environment(), capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split()
            if len(parts) >= 3 and parts[0] == "default":
                gateway = parts[2]
                # 仅在探测成功时写缓存；失败返回空串但不缓存，允许下次重试
                _get_windows_gateway._cached = gateway  # type: ignore[attr-defined]
                return gateway
    except Exception:
        pass

    return gateway


def _is_wsl() -> bool:
    """Return True if running inside WSL (Windows Subsystem for Linux)."""
    try:
        with open("/proc/version", "r") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _find_windows_python() -> str:
    """Find a Windows-side Python executable for the TCP relay.

    Returns the Windows-native path (e.g. ``D:\\Python-3.12-64bit\\python.exe``)
    or empty string if not found.
    """
    if hasattr(_find_windows_python, "_cached"):
        return _find_windows_python._cached  # type: ignore[attr-defined]
    import subprocess
    result = ""
    try:
        proc = subprocess.run(
            ["cmd.exe", "/c", "where", "python"],
            env=browser_child_environment(), capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0:
            for line in proc.stdout.strip().splitlines():
                line = line.strip()
                # Skip WindowsApps stubs (they redirect to MS Store)
                if line and "WindowsApps" not in line and line.endswith(".exe"):
                    result = line
                    break
    except Exception:
        pass
    _find_windows_python._cached = result  # type: ignore[attr-defined]
    return result


def _get_windows_temp() -> str:
    """Get the Windows-side TEMP directory as a WSL-accessible path.

    Returns e.g. ``/mnt/c/Users/Admin/AppData/Local/Temp`` or empty string
    on failure.  Cached after first call.
    """
    if hasattr(_get_windows_temp, "_cached"):
        return _get_windows_temp._cached  # type: ignore[attr-defined]
    result = ""
    try:
        import subprocess
        proc = subprocess.run(
            ["cmd.exe", "/c", "echo", "%TEMP%"],
            env=browser_child_environment(), capture_output=True, text=True, timeout=5,
        )
        win_temp = proc.stdout.strip()
        if win_temp and proc.returncode == 0:
            # Convert to WSL path: C:\Users\... -> /mnt/c/Users/...
            conv = subprocess.run(
                ["wslpath", "-u", win_temp],
                env=browser_child_environment(), capture_output=True, text=True, timeout=3,
            )
            if conv.returncode == 0 and conv.stdout.strip():
                result = conv.stdout.strip()
    except Exception:
        pass
    _get_windows_temp._cached = result  # type: ignore[attr-defined]
    return result


def _build_wsl_relay_script(chrome_port: int, bind_host: str) -> str:
    """Build a narrow Windows-side CDP relay bound only to WSL's host NIC."""
    bind_literal = json.dumps(bind_host)
    return (
        "import socket,threading\n"
        f"BIND_HOST={bind_literal}\n"
        "def pipe(s,d):\n"
        "    try:\n"
        "        while True:\n"
        "            x=s.recv(65536)\n"
        "            if not x:break\n"
        "            d.sendall(x)\n"
        "    except Exception:pass\n"
        "    finally:\n"
        "        try:d.shutdown(socket.SHUT_WR)\n"
        "        except Exception:pass\n"
        "def handle(c):\n"
        f"    try:r=socket.create_connection(('127.0.0.1',{chrome_port}))\n"
        "    except Exception:c.close();return\n"
        "    t1=threading.Thread(target=pipe,args=(c,r),daemon=True)\n"
        "    t2=threading.Thread(target=pipe,args=(r,c),daemon=True)\n"
        "    t1.start();t2.start();t1.join();t2.join()\n"
        "    try:c.close()\n"
        "    except Exception:pass\n"
        "    try:r.close()\n"
        "    except Exception:pass\n"
        "srv=socket.socket(socket.AF_INET,socket.SOCK_STREAM)\n"
        "srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
        "srv.bind((BIND_HOST,0))\n"
        "srv.listen(5)\n"
        "print(f'RELAY_READY {srv.getsockname()[1]}',flush=True)\n"
        "while True:\n"
        "    c,_=srv.accept()\n"
        "    threading.Thread(target=handle,args=(c,),daemon=True).start()\n"
    )


class CdpConnection:
    """Persistent headless Chrome process with a CDP WebSocket connection.

    Supports interactive page operations: navigate, evaluate JS, click,
    type, screenshot — all through the Chrome DevTools Protocol.

    Usage::

        conn = CdpConnection("/path/to/chrome")
        await conn.start()
        await conn.navigate("https://example.com")
        text = await conn.evaluate("document.body.innerText")
        await conn.close()
    """

    def __init__(
        self,
        chrome_path: str,
        *,
        headless: bool = True,
        timeout: int = 30,
        user_data_dir: str = "",
        page_ws_url: str = "",
        browser_ws_url: str = "",
        target_id: str = "",
    ):
        self._chrome_path = chrome_path
        self._headless = headless
        self._timeout = timeout
        self._process: asyncio.subprocess.Process | None = None
        self._ws: Any = None  # websockets.WebSocketClientProtocol
        self._msg_id = 0
        self._port: int = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._stderr_drain_task: asyncio.Task | None = None
        self._started = False
        self._event_waiters: dict[str, list[asyncio.Future]] = {}
        self._page_ws_url = page_ws_url
        self._browser_ws_url = browser_ws_url
        self._target_id = target_id
        self._owns_process = not bool(page_ws_url)
        self._owned_profile = self._owns_process and not bool(user_data_dir)
        self._user_data_dir = (
            user_data_dir or (_make_temp_profile() if self._owns_process else "")
        )
        # WSL TCP relay: Chrome binds 127.0.0.1 on Windows, unreachable from
        # WSL2 NAT. A Windows-side Python relay on 0.0.0.0:relay_port forwards
        # to 127.0.0.1:chrome_port so WSL can connect via the gateway IP.
        self._relay_process: Any = None  # subprocess.Popen
        self._relay_port: int = 0

    @property
    def is_connected(self) -> bool:
        return self._started and self._ws is not None

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def browser_ws_url(self) -> str:
        return self._browser_ws_url

    async def start(self) -> None:
        """Launch Chrome and establish the CDP WebSocket connection."""
        if self._started:
            return

        if self._page_ws_url:
            await self._connect_page(self._page_ws_url)
            return

        # Native Windows uses Chrome's active-port marker instead of a stderr
        # pipe. This avoids Proactor pipe warnings when many short-lived
        # browser sessions are exercised in separate event loops.
        self._port = 0 if os.name == "nt" else _find_free_port()
        active_port_file = Path(self._user_data_dir) / "DevToolsActivePort"
        if os.name == "nt":
            with suppress(OSError):
                active_port_file.unlink()
        args = [self._chrome_path]
        if self._headless:
            args.append("--headless=new")
        args += [
            f"--remote-debugging-port={self._port}",
            "--remote-debugging-address=0.0.0.0",
            f"--user-data-dir={_to_windows_path(self._user_data_dir)}",
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-dialogs",
            "--disable-background-networking",
            "--disable-sync",
            f"--user-agent={_USER_AGENT}",
            "about:blank" if self._headless else _HEADED_START_PAGE,
        ]

        self._process = await asyncio.create_subprocess_exec(
            *args,
            env=browser_child_environment(),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=(
                asyncio.subprocess.DEVNULL
                if os.name == "nt"
                else asyncio.subprocess.PIPE
            ),
            creationflags=hidden_process_creationflags(),
        )

        try:
            raw_ws_url = await self._wait_for_debugger()
            # On WSL, start a Windows-side TCP relay so we can reach Chrome
            await self._start_wsl_relay()
            browser_ws_url = self._fix_wsl_url(raw_ws_url)
            self._browser_ws_url = browser_ws_url
            page_ws_url = await self._find_page_websocket(browser_ws_url)
            self._page_ws_url = page_ws_url
            self._target_id = page_ws_url.rsplit("/", 1)[-1]
            await self._connect_page(page_ws_url)
        except ImportError as exc:
            await self.close()
            raise RuntimeError(
                "websockets package required for interactive CDP. "
                "Install: pip install websockets"
            ) from exc
        except Exception:
            await self.close()
            raise
        logger.info("CDP connection established on port %d", self._port)

    async def _connect_page(self, page_ws_url: str) -> None:
        deadline = asyncio.get_running_loop().time() + min(self._timeout, 8)
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                self._ws = await _open_cdp_websocket(
                    page_ws_url,
                    max_size=50 * 1024 * 1024,
                    timeout=min(2, self._timeout),
                )
                self._started = True
                self._reader_task = asyncio.create_task(self._read_loop())
                await self.send("Page.enable")
                await self.send("Runtime.enable")
                return
            except Exception as exc:
                last_error = exc
                await self._reset_page_transport()
                await asyncio.sleep(0.1)
        raise RuntimeError(f"Chrome page target unavailable: {last_error}")

    async def _reset_page_transport(self) -> None:
        """Drop a failed page socket without terminating its browser host."""
        self._started = False
        reader = self._reader_task
        self._reader_task = None
        if reader is not None and not reader.done():
            reader.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reader
        ws = self._ws
        self._ws = None
        if ws is not None:
            with suppress(Exception):
                await ws.close()
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def _find_page_websocket(self, browser_ws_url: str) -> str:
        """Resolve a page target through the browser WebSocket.

        Chrome's ``/json/list`` HTTP endpoint is surprisingly sensitive to
        local proxy and sandbox rules on Windows. Target.getTargets travels
        over the already-advertised DevTools socket and avoids that failure
        mode. The HTTP endpoint remains a compatibility fallback.
        """
        from urllib.parse import urlsplit

        parsed = urlsplit(browser_ws_url)
        deadline = asyncio.get_running_loop().time() + min(self._timeout, 6)
        last_error = "no page target"
        while asyncio.get_running_loop().time() < deadline:
            try:
                control = await _open_cdp_websocket(
                    browser_ws_url,
                    max_size=4 * 1024 * 1024,
                    timeout=min(2, self._timeout),
                )
                async with control:
                    await control.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
                    target_id = ""
                    while not target_id:
                        response = json.loads(
                            await asyncio.wait_for(control.recv(), timeout=3)
                        )
                        if response.get("id") != 1:
                            continue
                        infos = response.get("result", {}).get("targetInfos", [])
                        page = next(
                            (item for item in infos if item.get("type") == "page"),
                            None,
                        )
                        if page:
                            target_id = str(page.get("targetId") or "")
                        break
                    if not target_id:
                        await control.send(json.dumps({
                            "id": 2,
                            "method": "Target.createTarget",
                            "params": {"url": "about:blank"},
                        }))
                        while not target_id:
                            response = json.loads(
                                await asyncio.wait_for(control.recv(), timeout=3)
                            )
                            if response.get("id") == 2:
                                target_id = str(
                                    response.get("result", {}).get("targetId") or ""
                                )
                    if target_id:
                        return f"{parsed.scheme}://{parsed.netloc}/devtools/page/{target_id}"
            except Exception as exc:
                last_error = str(exc)
                await asyncio.sleep(0.1)

        logger.debug("Target.getTargets failed; falling back to /json/list: %s", last_error)

        endpoint = f"http://{parsed.hostname}:{parsed.port}/json/list"
        deadline = asyncio.get_running_loop().time() + min(self._timeout, 5)
        while asyncio.get_running_loop().time() < deadline:
            try:
                def _read_targets() -> list[dict[str, Any]]:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(endpoint, timeout=2) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    return payload if isinstance(payload, list) else []

                targets = await asyncio.to_thread(_read_targets)
                for target in targets:
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        return self._fix_wsl_url(str(target["webSocketDebuggerUrl"]))
            except Exception as exc:
                last_error = str(exc)
            await asyncio.sleep(0.1)
        raise RuntimeError(f"Chrome page target unavailable: {last_error}")

    async def _browser_command(self, method: str, params: dict | None = None) -> dict:
        """Send one command on the browser-level DevTools socket."""
        if not self._browser_ws_url:
            raise RuntimeError("Browser DevTools socket is unavailable")
        control = await _open_cdp_websocket(
            self._browser_ws_url,
            max_size=4 * 1024 * 1024,
            timeout=min(3, self._timeout),
        )
        async with control:
            payload: dict[str, Any] = {"id": 1, "method": method}
            if params:
                payload["params"] = params
            await control.send(json.dumps(payload))
            while True:
                response = json.loads(await asyncio.wait_for(control.recv(), timeout=self._timeout))
                if response.get("id") != 1:
                    continue
                if "error" in response:
                    raise RuntimeError(f"CDP browser error: {response['error']}")
                return dict(response.get("result") or {})

    async def create_page(self, url: str = "about:blank") -> "CdpConnection":
        """Create another page target inside this connection's Chrome process."""
        initial = _HEADED_START_PAGE if not self._headless else "about:blank"
        result = await self._browser_command("Target.createTarget", {"url": initial})
        target_id = str(result.get("targetId") or "")
        if not target_id:
            raise RuntimeError("Chrome did not return a target id")
        from urllib.parse import urlsplit

        parsed = urlsplit(self._browser_ws_url)
        page_ws_url = f"{parsed.scheme}://{parsed.netloc}/devtools/page/{target_id}"
        page = CdpConnection(
            self._chrome_path,
            headless=self._headless,
            timeout=self._timeout,
            page_ws_url=page_ws_url,
            browser_ws_url=self._browser_ws_url,
            target_id=target_id,
        )
        try:
            await page.start()
            if url and url != initial:
                await page.navigate(url)
            return page
        except Exception:
            with suppress(Exception):
                await self.close_target(target_id)
            raise

    async def close_target(self, target_id: str) -> None:
        """Close one page target without shutting down the shared browser."""
        if target_id:
            await self._browser_command("Target.closeTarget", {"targetId": target_id})

    async def _wait_for_debugger(self) -> str:
        """Wait for Chrome to report its DevTools WebSocket URL.

        Native Windows reads Chrome's ``DevToolsActivePort`` marker to avoid
        an asyncio Proactor pipe. Other platforms parse the equivalent stderr
        line. Returns the raw URL (127.0.0.1); the caller applies WSL relay
        rewriting after the relay is started.
        """
        import re as _re

        deadline = asyncio.get_event_loop().time() + min(self._timeout, 15)
        stderr_buf = ""

        while asyncio.get_event_loop().time() < deadline:
            marker = Path(self._user_data_dir) / "DevToolsActivePort"
            try:
                lines = marker.read_text(encoding="utf-8").splitlines()
                if len(lines) >= 2 and lines[0].isdigit():
                    self._port = int(lines[0])
                    return f"ws://127.0.0.1:{self._port}{lines[1]}"
            except OSError:
                pass

            # Try to read a chunk from stderr (non-blocking-ish)
            if self._process and self._process.stderr:
                try:
                    chunk = await asyncio.wait_for(
                        self._process.stderr.read(4096), timeout=1.0,
                    )
                    if chunk:
                        stderr_buf += chunk.decode("utf-8", errors="replace")
                except asyncio.TimeoutError:
                    pass

            # Look for the DevTools listening line
            match = _re.search(r"DevTools listening on (ws://[^\s]+)", stderr_buf)
            if match:
                ws_url = match.group(1)
                if self._process and self._process.stderr:
                    self._stderr_drain_task = asyncio.create_task(
                        self._drain_process_stderr(self._process.stderr)
                    )
                return ws_url

            await asyncio.sleep(0.2)

        await self._kill_process()
        raise RuntimeError(
            f"Chrome did not report DevTools URL within timeout. "
            f"stderr: {stderr_buf[:500]}"
        )

    @staticmethod
    async def _drain_process_stderr(stderr: asyncio.StreamReader) -> None:
        """Drain Chrome diagnostics so the Windows pipe closes cleanly."""
        with suppress(Exception):
            while await stderr.read(4096):
                pass

    def _fix_wsl_url(self, url: str) -> str:
        """Rewrite 127.0.0.1 URLs to go through the WSL TCP relay.

        Chrome binds DevTools to 127.0.0.1 on the Windows side, which is
        unreachable from WSL2 NAT.  When a relay is running we replace
        ``127.0.0.1:<chrome_port>`` with ``<gateway>:<relay_port>``.
        """
        if "127.0.0.1" not in url:
            return url
        gateway = _get_windows_gateway()
        if not gateway:
            return url
        if self._relay_port and self._port:
            # Route through the TCP relay
            url = url.replace(f"127.0.0.1:{self._port}", f"{gateway}:{self._relay_port}")
            # Catch any remaining 127.0.0.1 (e.g. different port in path)
            url = url.replace("127.0.0.1", gateway)
        else:
            url = url.replace("127.0.0.1", gateway)
        return url

    async def _start_wsl_relay(self) -> None:
        """Start a Windows-side TCP relay so WSL can reach Chrome DevTools.

        Chrome ignores ``--remote-debugging-address=0.0.0.0`` in recent
        versions and always binds to 127.0.0.1.  Windows Firewall blocks
        inbound connections from the WSL NAT subnet. The relay binds only to
        the Windows host address on the WSL virtual interface and forwards to
        ``127.0.0.1:<chrome_port>``. It never listens on LAN-facing interfaces.
        """
        if not _is_wsl() or not self._port:
            return
        win_python = _find_windows_python()
        if not win_python:
            logger.warning("WSL detected but no Windows Python found for TCP relay")
            return
        gateway = _get_windows_gateway()
        if not gateway:
            raise RuntimeError("WSL gateway is unavailable; refusing to expose a CDP relay")

        self._relay_port = 0
        # Threaded raw-socket relay — more reliable than asyncio for large
        # transfers (screenshot base64 can be 500KB+).  asyncio StreamReader
        # buffer limits and drain() backpressure caused mid-transfer drops.
        relay_script = _build_wsl_relay_script(self._port, gateway)

        # Write relay script to the profile dir (accessible from Windows)
        relay_path = Path(self._user_data_dir) / "_wsl_relay.py"
        relay_path.write_text(relay_script, encoding="utf-8")
        win_relay = _to_windows_path(str(relay_path))

        # Launch Windows Python directly (not through cmd.exe — more reliable)
        # Convert Windows path to WSL-accessible path for Popen
        import subprocess as _sp
        launch_python = win_python
        if "\\" in win_python:
            # D:\Python\python.exe -> /mnt/d/Python/python.exe
            try:
                r = _sp.run(["wslpath", "-u", win_python], env=browser_child_environment(), capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and r.stdout.strip():
                    launch_python = r.stdout.strip()
            except Exception:
                pass
        self._relay_process = _sp.Popen(
            [launch_python, win_relay],
            env=browser_child_environment(),
            stdout=_sp.PIPE,
            stderr=_sp.DEVNULL,
        )
        # Wait for relay to be ready (up to 5s)
        deadline = asyncio.get_event_loop().time() + 5.0
        ready = False
        while asyncio.get_event_loop().time() < deadline:
            if self._relay_process.poll() is not None:
                logger.warning("WSL relay exited early (code %d)", self._relay_process.returncode)
                self._relay_process = None
                self._relay_port = 0
                return
            try:
                line = await asyncio.wait_for(
                    asyncio.to_thread(self._relay_process.stdout.readline),
                    timeout=1.0,
                )
                match = re.search(rb"RELAY_READY\s+(\d+)", line)
                if match:
                    self._relay_port = int(match.group(1))
                    ready = True
                    break
            except asyncio.TimeoutError:
                pass
        if not ready:
            self._stop_wsl_relay()
            raise RuntimeError("WSL relay did not report a bound port in time")
        logger.info(
            "WSL TCP relay started: %s:%d -> 127.0.0.1:%d",
            gateway, self._relay_port, self._port,
        )

    def _stop_wsl_relay(self) -> None:
        """Kill the Windows-side TCP relay if running."""
        if self._relay_process is not None:
            with suppress(Exception):
                self._relay_process.kill()
            self._relay_process = None
        self._relay_port = 0

    async def _read_loop(self) -> None:
        """Background task: read CDP messages and resolve pending futures."""
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if not future.done():
                        if "error" in msg:
                            future.set_exception(
                                RuntimeError(f"CDP error: {msg['error']}")
                            )
                        else:
                            future.set_result(msg.get("result", {}))
                method = msg.get("method")
                if method and method in self._event_waiters:
                    waiters = self._event_waiters.pop(method)
                    for waiter in waiters:
                        if not waiter.done():
                            waiter.set_result(msg.get("params", {}))
        except Exception:
            # Connection closed or error — resolve all pending with error
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("CDP connection closed"))
            self._pending.clear()
        finally:
            self._started = False

    def _event_future(self, method: str) -> asyncio.Future:
        future = asyncio.get_running_loop().create_future()
        self._event_waiters.setdefault(method, []).append(future)
        return future

    async def send(self, method: str, params: dict | None = None) -> dict:
        """Send a CDP command and wait for its response."""
        if not self.is_connected:
            raise RuntimeError("CDP not connected. Call start() first.")

        self._msg_id += 1
        msg_id = self._msg_id
        message = {"id": msg_id, "method": method}
        if params:
            message["params"] = params

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = future

        try:
            await self._ws.send(json.dumps(message))
            return await asyncio.wait_for(future, timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"CDP command {method} timed out after {self._timeout}s") from exc
        except Exception:
            self._pending.pop(msg_id, None)
            raise

    # ── Page operations ────────────────────────────────────────────

    async def navigate(self, url: str, *, wait_ms: int = 2000) -> str:
        """Navigate to a URL and wait for the page to settle."""
        loaded = self._event_future("Page.loadEventFired")
        result = await self.send("Page.navigate", {"url": url})
        try:
            await asyncio.wait_for(loaded, timeout=max(0.1, wait_ms / 1000.0))
        except asyncio.TimeoutError:
            waiters = self._event_waiters.get("Page.loadEventFired", [])
            if loaded in waiters:
                waiters.remove(loaded)
        frame_id = result.get("frameId", "")
        return frame_id

    async def evaluate(self, expression: str) -> Any:
        """Execute JavaScript and return the result value."""
        result = await self.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        remote = result.get("result", {})
        if remote.get("subtype") == "error":
            raise RuntimeError(f"JS error: {remote.get('description', remote)}")
        return remote.get("value")

    async def get_text(self) -> str:
        """Get the page's visible text content."""
        text = await self.evaluate("document.body ? document.body.innerText : ''")
        return str(text or "")

    async def get_html(self) -> str:
        """Get the page's full HTML."""
        html = await self.evaluate("document.documentElement.outerHTML")
        return str(html or "")

    async def page_operation(self, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        """Run our bounded helper in one named isolated world, never retry writes."""
        params = dict(args)  # Capture request-local approval before the first await.
        writing = operation in {"click", "type", "fill", "check", "select"}
        dispatched = False
        try:
            tree = await self.send("Page.getFrameTree")
            frame_id = tree["frameTree"]["frame"]["id"]
            world = await self.send("Page.createIsolatedWorld", {
                "frameId": frame_id, "worldName": "astra-browser-control",
            })
            context_id = world["executionContextId"]
            source = (Path(__file__).resolve().parents[2] / "browser-control-extension" / "page.js").read_text(encoding="utf-8")
            injected = await self.send("Runtime.evaluate", {
                "expression": source, "contextId": context_id,
                "returnByValue": True, "awaitPromise": True,
            })
            if injected.get("exceptionDetails"):
                raise RuntimeError("Page helper injection failed")
            expression = f"globalThis.__astraBrowserPage({json.dumps(operation)}, {json.dumps(params)})"
            dispatched = writing
            result = await self.send("Runtime.evaluate", {
                "expression": expression, "contextId": context_id,
                "returnByValue": True, "awaitPromise": True,
            })
            if result.get("exceptionDetails") or result.get("result", {}).get("subtype") == "error":
                raise RuntimeError("Page operation execution failed")
            value = result.get("result", {}).get("value")
            if not isinstance(value, dict):
                raise RuntimeError("Page helper returned no structured result")
            return value
        except Exception as exc:
            if writing:
                return {"status": "unknown_outcome" if dispatched else "error",
                        "message": "Action may have executed; do not replay automatically" if dispatched else str(exc)}
            raise

    async def click(self, selector: str, *, expected_origin: str = "") -> str:
        return json.dumps(await self.page_operation("click", {"selector": selector, "expectedOrigin": expected_origin}), ensure_ascii=False)

    async def type_text(self, selector: str, text: str, *, expected_origin: str = "") -> str:
        return json.dumps(await self.page_operation("type", {"selector": selector, "text": text, "expectedOrigin": expected_origin}), ensure_ascii=False)

    async def select(self, selector: str, value: str, *, expected_origin: str = "") -> str:
        return json.dumps(await self.page_operation("select", {"selector": selector, "value": value, "expectedOrigin": expected_origin}), ensure_ascii=False)

    async def wait_for(
        self,
        *,
        selector: str = "",
        text: str = "",
        url_contains: str = "",
        timeout_ms: int = 10000,
    ) -> str:
        """Poll page state until all supplied conditions are true."""
        if not any((selector, text, url_contains)):
            raise ValueError("selector, text, or url_contains is required")
        waited = max(0.1, min(timeout_ms, 30000) / 1000)
        deadline = asyncio.get_running_loop().time() + waited
        while asyncio.get_running_loop().time() < deadline:
            probe = await self.page_operation("probe", {
                "selector": selector, "text": text, "urlContains": url_contains,
            })
            if probe.get("status"):
                return json.dumps(probe, ensure_ascii=False)
            if probe.get("matched") is True:
                return "Wait condition satisfied"
            await asyncio.sleep(0.2)
        return json.dumps({"status": "timeout", "message": f"Wait condition timed out after {round(waited * 1000)} ms"})

    async def screenshot(self, *, output_path: str = "") -> str:
        """Capture a screenshot of the current page via CDP."""
        import base64

        result = await self.send("Page.captureScreenshot", {"format": "png"})
        data = base64.b64decode(result.get("data", ""))
        if not data:
            return "[CDP Error] Empty screenshot data"

        if not output_path:
            fd, output_path = tempfile.mkstemp(suffix=".png", prefix="cdp_page_")
            os.close(fd)

        with open(output_path, "wb") as f:
            f.write(data)
        return output_path

    async def get_url(self) -> str:
        """Get the current page URL."""
        url = await self.evaluate("window.location.href")
        return str(url or "")

    # ── Lifecycle ──────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the page transport and, for an owner, the whole browser."""
        if self.is_connected and self._owns_process:
            # Browser.close belongs to the browser target. Sending it through
            # the page socket is not reliable and caused orphan Chrome trees.
            with suppress(Exception):
                await self._browser_command("Browser.close")
            if self._process is not None:
                with suppress(asyncio.TimeoutError, ProcessLookupError, OSError):
                    await asyncio.wait_for(self._process.wait(), timeout=3)
        self._started = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._owns_process:
            await self._kill_process()
        stderr_task = self._stderr_drain_task
        self._stderr_drain_task = None
        if stderr_task is not None:
            try:
                await asyncio.wait_for(stderr_task, timeout=2)
            except asyncio.TimeoutError:
                stderr_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await stderr_task
        self._pending.clear()
        for waiters in self._event_waiters.values():
            for future in waiters:
                if not future.done():
                    future.cancel()
        self._event_waiters.clear()
        self._stop_wsl_relay()
        if self._owned_profile and self._user_data_dir:
            shutil.rmtree(self._user_data_dir, ignore_errors=True)
        logger.info("CDP connection closed")

    async def _kill_process(self) -> None:
        if self._process:
            await _force_kill_process_tree(self._process)
            self._process = None


class CdpBrowserBackend:
    """Browser backend using Chrome/Edge headless mode.

    Implements the BrowserBackend protocol (extract + status) and adds
    interactive capabilities via CdpConnection (click/type/evaluate),
    screenshot(), and launch_headed() for human takeover.
    """

    name = "cdp-headless"

    structured_snapshots = True

    def __init__(
        self,
        chrome_path: str = "",
        *,
        timeout: int = _DEFAULT_TIMEOUT,
        virtual_time_budget: int = _DEFAULT_VIRTUAL_TIME_BUDGET,
        user_data_dir: str = "",
    ):
        self._chrome_path = chrome_path or find_chrome()
        self._timeout = timeout
        self._virtual_time_budget = virtual_time_budget
        self._user_data_dir = user_data_dir
        interactive = bool(self._chrome_path) and interactive_dependency_available()
        self.capabilities = BackendCapabilities(
            read=bool(self._chrome_path), interactive=interactive, takeover=interactive
        )
        self._host: CdpConnection | None = None
        self._host_tab_id = ""
        self._host_headed = False
        self._host_profile_dir = ""
        self._connections: dict[str, CdpConnection] = {}
        self._connection_lock = asyncio.Lock()

    @property
    def chrome_available(self) -> bool:
        return bool(self._chrome_path)

    @property
    def chrome_path(self) -> str:
        return self._chrome_path

    # ------------------------------------------------------------------
    # BrowserBackend protocol
    # ------------------------------------------------------------------

    async def extract(self, url: str, *, max_length: int = 12000) -> str:
        """Extract page text using headless Chrome --dump-dom.

        Launches a fresh headless Chrome process, navigates to the URL,
        waits for JS execution (virtual time budget), dumps the DOM,
        and converts to plain text.
        """
        if not self._chrome_path:
            return "[CDP Error] Chrome/Edge not found. Set CHROME_PATH or install Chrome."
        if not url or not str(url).strip():
            return "[CDP Error] url is required"

        args = self._base_args(headless=True)
        args += [
            f"--virtual-time-budget={self._virtual_time_budget}",
            "--dump-dom",
            str(url),
        ]

        html, err = await self._run_chrome(args)
        if html is None:
            return f"[CDP Error] Chrome failed: {err}"

        text = _html_to_text(html)
        if not text:
            return f"[CDP Error] No text content extracted from {url}"

        if max_length > 0 and len(text) > max_length:
            text = text[:max_length] + "\n…[truncated]"
        return text

    async def status(self) -> tuple[bool, str]:
        """Check if Chrome is available and report its path."""
        if not self._chrome_path:
            return False, "Chrome/Edge not found. Set CHROME_PATH or install Chrome."
        if os.name == "nt" and Path(self._chrome_path).is_file():
            # Running chrome.exe --version on Windows may delegate to an
            # existing browser process and leave an asyncio pipe transport.
            return True, f"Chrome available: {self._chrome_path}"
        # Verify the binary is actually executable
        try:
            proc = await asyncio.create_subprocess_exec(
                self._chrome_path, "--version",
                env=browser_child_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            version = stdout.decode("utf-8", errors="replace").strip()
            if version:
                return True, f"{version} ({self._chrome_path})"
            return True, f"Chrome available: {self._chrome_path}"
        except asyncio.TimeoutError:
            return True, f"Chrome found but --version timed out: {self._chrome_path}"
        except Exception as e:
            return False, f"Chrome found but not runnable: {e}"

    # ------------------------------------------------------------------
    # Extended capabilities (beyond protocol)
    # ------------------------------------------------------------------

    async def screenshot(
        self,
        url: str,
        *,
        output_path: str = "",
        width: int = 1280,
        height: int = 900,
    ) -> str:
        """Take a screenshot using headless Chrome --screenshot.

        Returns the path to the saved PNG file, or an error string.
        """
        if not self._chrome_path:
            return "[CDP Error] Chrome/Edge not found."
        if not url or not str(url).strip():
            return "[CDP Error] url is required"

        if not output_path:
            fd, output_path = tempfile.mkstemp(suffix=".png", prefix="cdp_shot_")
            os.close(fd)

        # Chrome is a Windows process — convert WSL paths
        win_path = _to_windows_path(output_path)

        args = self._base_args(headless=True)
        args += [
            f"--window-size={width},{height}",
            f"--screenshot={win_path}",
            f"--virtual-time-budget={self._virtual_time_budget}",
            str(url),
        ]

        _, err = await self._run_chrome(args)
        if os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            return output_path
        return f"[CDP Error] Screenshot failed: {err or 'empty output'}"

    def launch_headed(self, url: str, *, profile_dir: str = "") -> str:
        """Launch a headed (visible) Chrome window for human takeover.

        Returns a status message. The Chrome process is detached — it
        stays open for the user to interact with.
        """
        if not self._chrome_path:
            return "[CDP Error] Chrome/Edge not found."

        args = [self._chrome_path]
        # No --headless flag → headed mode
        args += [
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-agent={_USER_AGENT}",
        ]
        if profile_dir:
            args.append(f"--user-data-dir={profile_dir}")
        elif self._user_data_dir:
            args.append(f"--user-data-dir={self._user_data_dir}")
        args.append(str(url))

        try:
            # Detach: don't wait for Chrome to exit
            import subprocess
            subprocess.Popen(
                args,
                env=browser_child_environment(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return f"Headed Chrome launched at {url}. Complete the action, then call browser_resume."
        except Exception as e:
            return f"[CDP Error] Failed to launch headed Chrome: {e}"

    # ------------------------------------------------------------------
    # Interactive capabilities (via persistent CdpConnection)
    # ------------------------------------------------------------------

    async def ensure_connection(
        self,
        tab_id: str = "default",
        *,
        url: str = "",
        headed: bool = False,
        profile_dir: str = "",
    ) -> CdpConnection:
        """Bind one logical tab to a page target in the shared Chrome process."""
        if not self.capabilities.interactive:
            if not self._chrome_path:
                raise RuntimeError("Chrome/Edge not found. Set CHROME_PATH.")
            raise RuntimeError("websockets is required for interactive browser tools")
        key = tab_id or "default"
        async with self._connection_lock:
            if self._host is not None and not self._host.is_connected:
                await self._shutdown_host_locked()
            if self._host is None:
                self._host = CdpConnection(
                    self._chrome_path,
                    headless=not headed,
                    timeout=self._timeout,
                    user_data_dir=profile_dir or self._user_data_dir,
                )
                await self._host.start()
                self._host_headed = headed
                self._host_profile_dir = profile_dir or self._user_data_dir
            conn = self._connections.get(key)
            if conn is not None and not conn.is_connected:
                self._connections.pop(key, None)
                conn = None
            if conn is None:
                if not self._host_tab_id:
                    conn = self._host
                    self._host_tab_id = key
                else:
                    conn = await self._host.create_page(url or "about:blank")
                self._connections[key] = conn
            if url:
                current = await conn.get_url()
                if current in {"", "about:blank"} or current != url:
                    await conn.navigate(url)
            return conn

    async def close_connection(self, tab_id: str = "") -> None:
        """Close one page target, or the shared browser host when omitted."""
        async with self._connection_lock:
            if not tab_id:
                await self._shutdown_host_locked()
                return
            key = tab_id or "default"
            conn = self._connections.pop(key, None)
            if conn is None:
                return
            if conn is self._host:
                self._host_tab_id = ""
                with suppress(Exception):
                    await conn.navigate(
                        _HEADED_START_PAGE if self._host_headed else "about:blank",
                        wait_ms=500,
                    )
                return
            target_id = conn.target_id
            await conn.close()
            if self._host is not None and self._host.is_connected:
                with suppress(Exception):
                    await self._host.close_target(target_id)

    async def _shutdown_host_locked(self) -> None:
        """Close every page and the one shared Chrome process. Lock must be held."""
        host = self._host
        for conn in set(self._connections.values()):
            if conn is host:
                continue
            target_id = conn.target_id
            await conn.close()
            if host is not None and host.is_connected:
                with suppress(Exception):
                    await host.close_target(target_id)
        self._connections.clear()
        self._host_tab_id = ""
        if host is not None:
            await host.close()
        self._host = None
        self._host_headed = False
        self._host_profile_dir = ""

    async def connect_existing(
        self,
        *,
        port: int = 0,
        host: str = "",
        tab_id: str = "default",
    ) -> str:
        """Connect to an already-running Chrome with DevTools enabled.

        This reuses the user's real browser session — cookies, login state,
        extensions — instead of launching a clean headless instance.

        If ``port`` is 0, auto-discovers by scanning common debug ports.
        Returns a status message with available page targets.
        """
        # ── Discover if needed ──
        if not port:
            instances = await discover_chrome_debug_ports()
            if not instances:
                return (
                    "[Browser] No running Chrome with DevTools found. "
                    "Launch Chrome with: chrome --remote-debugging-port=9222"
                )
            inst = instances[0]
            port = inst["port"]
            host = host or inst["host"]
        else:
            host = host or "127.0.0.1"

        # ── List existing page targets ──
        targets = await list_existing_targets(host, port)
        if not targets:
            return f"[Browser] Chrome found on {host}:{port} but no page targets available."

        # ── Connect to the first page target via its WS URL ──
        # Pick the first non-blank page, or the first page
        target = next(
            (t for t in targets if t["url"] not in ("", "about:blank", "chrome://newtab/")),
            targets[0],
        )
        ws_url = target.get("ws_url", "")
        if not ws_url:
            return f"[Browser] Chrome on {host}:{port} has pages but no WebSocket URLs."

        # Normalise for WSL
        if host != "127.0.0.1":
            ws_url = ws_url.replace("127.0.0.1", host)

        key = tab_id or "default"
        async with self._connection_lock:
            conn = CdpConnection(
                self._chrome_path,
                headless=False,
                timeout=self._timeout,
                page_ws_url=ws_url,
                target_id=target.get("id", ""),
            )
            await conn.start()
            self._connections[key] = conn

        lines = [
            f"Connected to existing Chrome on {host}:{port}",
            f"Attached to: {target['title'] or '(untitled)'} — {target['url']}",
            f"Other open pages ({len(targets) - 1}):",
        ]
        for t in targets:
            if t["id"] != target.get("id"):
                lines.append(f"  - {t['title'] or '(untitled)'}: {t['url']}")
        return "\n".join(lines)

    async def interactive_navigate(
        self, url: str, *, tab_id: str = "default", wait_ms: int = 2000
    ) -> str:
        """Navigate the CDP page bound to a logical tab."""
        conn = await self.ensure_connection(tab_id)
        await conn.navigate(url, wait_ms=wait_ms)
        title = await conn.evaluate("document.title")
        return f"Navigated to {url} (title: {title})"

    @staticmethod
    def _write_origin(url: str) -> str:
        from .tools.approval import normalized_origin

        expected = normalized_origin(url)
        if url and not expected:
            raise RuntimeError("Invalid approved URL; renewed approval required")
        return expected

    async def interactive_click(self, selector: str, *, tab_id: str = "default", url: str = "") -> str:
        """Click on the CDP page bound to a logical tab."""
        conn = self._connections.get(tab_id or "default")
        if conn is None or not conn.is_connected:
            raise RuntimeError("Browser connection lost; reconnect before writing")
        return await conn.click(selector, expected_origin=self._write_origin(url))

    async def interactive_type(
        self, selector: str, text: str, *, tab_id: str = "default", url: str = ""
    ) -> str:
        """Type on the CDP page bound to a logical tab."""
        conn = self._connections.get(tab_id or "default")
        if conn is None or not conn.is_connected:
            raise RuntimeError("Browser connection lost; reconnect before writing")
        return await conn.type_text(selector, text, expected_origin=self._write_origin(url))

    async def interactive_select(
        self, selector: str, value: str, *, tab_id: str = "default", url: str = ""
    ) -> str:
        conn = self._connections.get(tab_id or "default")
        if conn is None or not conn.is_connected:
            raise RuntimeError("Browser connection lost; reconnect before writing")
        return await conn.select(selector, value, expected_origin=self._write_origin(url))

    async def _target_operation(self, operation: str, *, tab_id: str, url: str, **args) -> str:
        conn = self._connections.get(tab_id or "default")
        if conn is None or not conn.is_connected:
            raise RuntimeError("Browser connection lost; reconnect before interacting")
        result = await conn.page_operation(operation, {**args, "expectedOrigin": self._write_origin(url)})
        return json.dumps(result, ensure_ascii=False)

    async def interactive_fill(self, selector: str, text: str, *, tab_id: str = "default", url: str = "") -> str:
        return await self._target_operation("fill", tab_id=tab_id, url=url, selector=selector, text=text)

    async def interactive_read(self, selector: str, *, tab_id: str = "default", url: str = "", offset: int = 0) -> str:
        return await self._target_operation("read", tab_id=tab_id, url=url, selector=selector,
                                            **({"offset": offset} if offset else {}))

    async def interactive_check(self, selector: str = "", *, checked: bool = True,
        checks: list[dict[str, object]] | None = None, tab_id: str = "default", url: str = "") -> str:
        args = {"checks": checks} if checks is not None else {"selector": selector, "checked": checked}
        return await self._target_operation("check", tab_id=tab_id, url=url, **args)

    async def interactive_snapshot(self, *, tab_id: str = "default", url: str = "", **options) -> str:
        return await self._target_operation("snapshot", tab_id=tab_id, url=url, **options)

    async def interactive_wait(
        self,
        *,
        tab_id: str = "default",
        url: str = "",
        selector: str = "",
        text: str = "",
        url_contains: str = "",
        timeout_ms: int = 10000,
    ) -> str:
        conn = self._connections.get(tab_id or "default")
        if conn is None or not conn.is_connected:
            raise RuntimeError("Browser connection lost; reconnect before observing")
        return await conn.wait_for(
            selector=selector, text=text, url_contains=url_contains, timeout_ms=timeout_ms
        )

    async def interactive_evaluate(
        self, expression: str, *, tab_id: str = "default", url: str = ""
    ) -> str:
        """Evaluate JavaScript on the persistent CDP page."""
        conn = await self.ensure_connection(tab_id, url=url)
        result = await conn.evaluate(expression)
        return str(result) if result is not None else "(undefined)"

    async def interactive_get_text(self, *, tab_id: str = "default", url: str = "") -> str:
        """Get visible text from the persistent CDP page."""
        conn = await self.ensure_connection(tab_id, url=url)
        return await conn.get_text()

    async def interactive_screenshot(
        self, *, tab_id: str = "default", url: str = "", output_path: str = ""
    ) -> str:
        """Screenshot the persistent CDP page."""
        conn = self._connections.get(tab_id or "default")
        if conn is None or not conn.is_connected:
            raise RuntimeError("Browser connection lost; reconnect before observing")
        return await conn.screenshot(output_path=output_path)

    async def assert_origin(self, *, tab_id: str, expected_url: str) -> None:
        """Preflight the live origin; write dispatch carries its own expected origin."""
        from .tools.approval import normalized_origin

        conn = self._connections.get(tab_id or "default")
        if conn is None or not conn.is_connected:
            raise RuntimeError("Browser connection lost; reconnect and renew approval")
        expected = normalized_origin(expected_url)
        if not expected or normalized_origin(await conn.get_url()) != expected:
            raise RuntimeError("Page origin changed; renewed approval required")

    async def interactive_state(
        self, *, tab_id: str = "default", url: str = ""
    ) -> tuple[str, str, str]:
        """Return the live structured page observation without navigating."""
        conn = self._connections.get(tab_id or "default")
        if conn is None or not conn.is_connected:
            raise RuntimeError("Browser connection lost; reconnect before taking a snapshot")
        snapshot = await conn.page_operation("snapshot", {})
        return str(snapshot.get("url", "")), str(snapshot.get("title", "")), json.dumps(snapshot, ensure_ascii=False)

    async def interactive_handoff(
        self, *, tab_id: str, url: str, profile_dir: str = ""
    ) -> str:
        """Migrate all logical tabs once into one managed visible Chrome."""
        urls: dict[str, str] = {}
        for key, connection in list(self._connections.items()):
            try:
                urls[key] = await connection.get_url()
            except Exception:
                pass
        urls[tab_id] = urls.get(tab_id) or url
        if not self._host_headed or (
            profile_dir and self._host_profile_dir != profile_dir
        ):
            await self.close_connection()
            for key, current_url in urls.items():
                await self.ensure_connection(
                    key,
                    url=current_url,
                    headed=True,
                    profile_dir=profile_dir,
                )
        else:
            await self.ensure_connection(tab_id, url=url)
        return f"Headed Chrome is ready at {urls[tab_id]}"

    async def interactive_resume(
        self, *, tab_id: str, url: str, profile_dir: str = ""
    ) -> str:
        """Resume automation on the exact visible target the user operated."""
        connection = await self.ensure_connection(tab_id, url="")
        current_url = await connection.get_url()
        return f"Automation resumed on the existing headed page at {current_url or url}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _base_args(self, *, headless: bool) -> list[str]:
        """Common Chrome arguments."""
        args = [self._chrome_path]
        if headless:
            args.append("--headless=new")
        args += [
            "--disable-gpu",
            "--disable-software-rasterizer",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-extensions",
            "--disable-dialogs",
            f"--user-agent={_USER_AGENT}",
        ]
        if self._user_data_dir:
            args.append(f"--user-data-dir={self._user_data_dir}")
        else:
            # Use a temporary profile to avoid polluting the user's profile
            args.append("--incognito")
        return args

    async def _run_chrome(self, args: list[str]) -> tuple[str | None, str]:
        """Run Chrome as a subprocess. Returns (stdout, stderr)."""
        run_args = list(args)
        owned_profile = ""
        if not any(arg.startswith("--user-data-dir=") for arg in run_args):
            owned_profile = _make_temp_profile()
            run_args.insert(1, f"--user-data-dir={_to_windows_path(owned_profile)}")
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *run_args,
                env=browser_child_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=hidden_process_creationflags(),
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout,
            )
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0 and not out:
                return None, f"exit {proc.returncode}: {err[:500]}"
            return out, err
        except asyncio.TimeoutError:
            if proc is not None:
                await _force_kill_process_tree(proc)
            return None, f"Chrome timed out after {self._timeout}s"
        except FileNotFoundError:
            return None, f"Chrome binary not found: {self._chrome_path}"
        except Exception as e:
            return None, str(e)
        finally:
            if owned_profile:
                shutil.rmtree(owned_profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# Discover already-running Chrome with DevTools enabled
# ---------------------------------------------------------------------------

# Common Chrome DevTools debugging ports to scan
_DEFAULT_DEBUG_PORTS = list(range(9222, 9231))


async def discover_chrome_debug_ports(
    ports: list[int] | None = None,
    *,
    timeout: float = 1.5,
) -> list[dict[str, Any]]:
    """Scan for already-running Chrome instances with DevTools enabled.

    Tries ``http://<host>:<port>/json/version`` on each candidate port.
    On WSL, also tries the Windows gateway IP since Chrome runs on the
    Windows side.

    Returns a list of dicts: ``{"port": int, "host": str, "browser_ws_url": str,
    "version": str}`` for each live instance found.
    """
    candidates = ports or _DEFAULT_DEBUG_PORTS
    hosts = ["127.0.0.1"]

    # On WSL, Chrome listens on the Windows side — also try the gateway
    try:
        with open("/proc/version", "r") as f:
            if "microsoft" in f.read().lower():
                import subprocess as _sp
                gw = _sp.run(
                    ["ip", "route", "show", "default"],
                    env=browser_child_environment(), capture_output=True, text=True, timeout=3,
                )
                if gw.returncode == 0:
                    parts = gw.stdout.strip().split()
                    if len(parts) >= 3:
                        hosts.append(parts[2])
    except Exception:
        pass

    found: list[dict[str, Any]] = []
    for host in hosts:
        for port in candidates:
            url = f"http://{host}:{port}/json/version"
            try:
                resp = await asyncio.wait_for(
                    asyncio.to_thread(urllib.request.urlopen, url, timeout=timeout),
                    timeout=timeout + 0.5,
                )
                data = json.loads(resp.read().decode())
                ws_url = data.get("webSocketDebuggerUrl", "")
                if ws_url:
                    # Normalise WSL gateway IP back so websockets can connect
                    found.append({
                        "port": port,
                        "host": host,
                        "browser_ws_url": ws_url.replace("127.0.0.1", host) if host != "127.0.0.1" else ws_url,
                        "version": data.get("Browser", ""),
                    })
            except Exception:
                continue
        if found:
            break  # found on this host, no need to try others
    return found


async def list_existing_targets(
    host: str = "127.0.0.1",
    port: int = 9222,
    *,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
    """List open page targets on a running Chrome DevTools endpoint.

    Returns a list of dicts with ``id``, ``title``, ``url``, ``ws_url``
    for each page target.
    """
    url = f"http://{host}:{port}/json/list"
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(urllib.request.urlopen, url, timeout=timeout),
            timeout=timeout + 0.5,
        )
        targets = json.loads(resp.read().decode())
        pages = []
        for t in targets:
            if t.get("type") == "page":
                pages.append({
                    "id": t.get("id", ""),
                    "title": t.get("title", ""),
                    "url": t.get("url", ""),
                    "ws_url": t.get("webSocketDebuggerUrl", ""),
                })
        return pages
    except Exception:
        return []
