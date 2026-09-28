"""web tools: search_web, fetch_url, web_extract, opt-in extract_url (Exa + SearXNG)."""

import asyncio
import atexit
import hashlib
import html
import ipaddress
import json
import ntpath
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path

from ...sandbox.windows_job import WindowsJob
from ..process_env import hidden_process_creationflags

try:
    import httpx
except ImportError:  # pragma: no cover - exercised by monkeypatch in tests
    httpx = None

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - optional dependency
    BeautifulSoup = None

try:
    from markdownify import markdownify as markdownify_html
except ImportError:  # pragma: no cover - optional dependency
    markdownify_html = None

from ..network import active_proxy_for_url
from .approval import normalized_origin
from .registry import ToolDef, ToolRegistry
from .image_search import register_image_search
from .search_image_reader import load_search_image

_BROWSER_EXTRACT_STATUS_ARGUMENT = "__astra_browser_extractor_status__"
_DEFAULT_EXTRACT_SCRIPT = (
    "if command -v extract >/dev/null 2>&1; then "
    f"if [ \"$1\" = '{_BROWSER_EXTRACT_STATUS_ARGUMENT}' ]; then command -v extract; "
    "else exec extract \"$1\"; fi; "
    "elif [ -x \"$HOME/bin/extract\" ]; then "
    f"if [ \"$1\" = '{_BROWSER_EXTRACT_STATUS_ARGUMENT}' ]; then echo \"$HOME/bin/extract\"; "
    "else exec \"$HOME/bin/extract\" \"$1\"; fi; "
    "else echo 'ERROR: extract not found in PATH or $HOME/bin/extract' >&2; exit 127; fi"
)
_DEFAULT_WSL_EXTRACT_ARGV = ("wsl", "sh", "-lc", _DEFAULT_EXTRACT_SCRIPT, "extract")
_DEFAULT_POSIX_EXTRACT_ARGV = ("bash", "-lc", _DEFAULT_EXTRACT_SCRIPT, "extract")
_DEFAULT_EXTRACT_SCRIPT_QUOTED = _DEFAULT_EXTRACT_SCRIPT.replace('"', '\\"')
DEFAULT_WSL_EXTRACT_CMD = f'wsl sh -lc "{_DEFAULT_EXTRACT_SCRIPT_QUOTED}" extract'
DEFAULT_POSIX_EXTRACT_CMD = f'bash -lc "{_DEFAULT_EXTRACT_SCRIPT_QUOTED}" extract'
_POSIX_LEGACY_EXTRACT_SCRIPT = 'eval "$1 \\"\\$2\\""'
_POSIX_LEGACY_STATUS_SCRIPT = 'eval "$1"'
_WINDOWS_LEGACY_URL_ENV = "ASTRA_BROWSER_EXTRACT_URL"
_WINDOWS_LEGACY_UNSUPPORTED_URL_CHARS = frozenset({'"', "\r", "\n", "\0"})
_WINDOWS_LEGACY_CMD_PREFIX = ("cmd.exe", "/D", "/V:OFF", "/S", "/C")
_WEB_EXTRACT_RECOVERY_CANDIDATES = 2
_SEARCH_RESULT_URL_RE = re.compile(r"(?im)^\s*URL:\s*(https?://\S+)\s*$")


# ---------------------------------------------------------------------------
# Module-level browser extract factories (shared by web tools + browser tools)
# ---------------------------------------------------------------------------


def resolve_browser_extract_command(command: str | None = None) -> str:
    """Resolve the compatibility shell command string or portable default.

    Extractor execution also supports the preferred JSON-array setting
    ``BROWSER_EXTRACT_ARGV`` through ``_resolve_browser_extract_argv``. This
    string-returning helper remains for integrations that inspect the legacy
    ``BROWSER_EXTRACT_CMD`` / ``WSL_EXTRACT_CMD`` resolution.
    """
    if command and command.strip():
        return command.strip()
    configured = os.getenv("BROWSER_EXTRACT_CMD", "").strip()
    if configured:
        return configured
    legacy = os.getenv("WSL_EXTRACT_CMD", "").strip()
    if legacy:
        return legacy
    if sys.platform == "win32":
        return DEFAULT_WSL_EXTRACT_CMD
    return DEFAULT_POSIX_EXTRACT_CMD


def _legacy_extract_argv(command: str) -> tuple[str, ...]:
    """Wrap a legacy shell command while keeping the URL out of shell text."""
    if sys.platform == "win32":
        return (
            *_WINDOWS_LEGACY_CMD_PREFIX,
            f'{command} "%{_WINDOWS_LEGACY_URL_ENV}%"',
        )
    return (
        "bash", "-lc", _POSIX_LEGACY_EXTRACT_SCRIPT,
        "browser-extractor", command,
    )


def _legacy_status_argv(command: str) -> tuple[str, ...]:
    """Wrap an explicit status command without adding a URL or sentinel."""
    if sys.platform == "win32":
        return (*_WINDOWS_LEGACY_CMD_PREFIX, command)
    return (
        "bash", "-lc", _POSIX_LEGACY_STATUS_SCRIPT,
        "browser-extractor-status", command,
    )


class _WindowsLegacyBrowserProcess:
    """Run CMD's command text without C-runtime argv quoting or pipe workers."""

    def __init__(self, argv: tuple[str, ...], env: dict[str, str] | None):
        if len(argv) != 6 or argv[:5] != _WINDOWS_LEGACY_CMD_PREFIX:
            raise ValueError("Invalid Windows legacy extractor command wrapper")
        # An explicit CreateProcess application name does not search PATH.
        # Match Python's Windows shell resolution without consulting cwd/PATH.
        executable = os.environ.get("COMSPEC") or ntpath.join(
            os.environ.get("SystemRoot", ""), "System32", "cmd.exe",
        )
        if not ntpath.isabs(executable) or not ntpath.splitdrive(executable)[0]:
            raise FileNotFoundError("Windows CMD requires an absolute COMSPEC or SystemRoot path")
        # /S removes just the outer pair. Popen must receive a string: passing
        # argv to asyncio would apply list2cmdline and add literal backslashes.
        # The URL remains in the child environment, outside this command text.
        # CMD must wait before it can spawn the configured extractor, so the
        # entire Windows process tree joins our Job before it starts running.
        command_line = (
            'cmd.exe /D /V:OFF /S /C "set /p "__ASTRA_BROWSER_START_GATE=" >nul && '
            f'{argv[-1]}"'
        )
        self._files = ExitStack()
        self._closed = False
        self._process: subprocess.Popen[bytes] | None = None
        self._job: WindowsJob | None = None
        try:
            self._stdout = self._files.enter_context(tempfile.TemporaryFile())
            self._stderr = self._files.enter_context(tempfile.TemporaryFile())
            self._process = subprocess.Popen(
                command_line,
                executable=executable,
                shell=False,
                stdin=subprocess.PIPE,
                stdout=self._stdout,
                stderr=self._stderr,
                env=env,
                creationflags=hidden_process_creationflags(),
            )
            self._job = WindowsJob.create(
                process_handle=int(getattr(self._process, "_handle")),
                memory_mb=None,
                cpu_seconds=None,
                max_processes=64,
            )
            assert self._process.stdin is not None
            self._process.stdin.write(b"1\n")
            self._process.stdin.close()
        except BaseException:
            self._close()
            raise

    @property
    def returncode(self) -> int | None:
        return self._process.returncode if self._process is not None else None

    async def communicate(self) -> tuple[bytes, bytes]:
        assert self._process is not None
        try:
            while self._process.poll() is None:
                await asyncio.sleep(0.01)
            self._stdout.seek(0)
            self._stderr.seek(0)
            return self._stdout.read(), self._stderr.read()
        finally:
            self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # No background communicate thread survives cancellation. Close the
        # Job first to stop remaining Windows descendants, then reap the parent.
        try:
            if self._job is not None:
                self._job.close()
        finally:
            try:
                if self._process is not None:
                    if self._process.poll() is None:
                        self._process.kill()
                    self._process.wait()
                    if self._process.stdin is not None:
                        self._process.stdin.close()
            finally:
                self._files.close()


async def _create_windows_legacy_subprocess(
    *argv: str, env: dict[str, str] | None = None,
) -> _WindowsLegacyBrowserProcess:
    # Construction contains no suspension point, so cancellation cannot lose
    # a newly spawned process before communicate owns its cleanup.
    return _WindowsLegacyBrowserProcess(argv, env)


def _parse_browser_extract_argv(value: str) -> tuple[str, ...]:
    """Parse the explicit JSON argv configuration without shell tokenization."""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("BROWSER_EXTRACT_ARGV must be a JSON array of strings") from exc
    if (
        not isinstance(parsed, list)
        or not parsed
        or any(not isinstance(item, str) or not item or "\0" in item for item in parsed)
    ):
        raise ValueError("BROWSER_EXTRACT_ARGV must be a non-empty JSON array of non-empty strings")
    return tuple(parsed)


def _resolve_browser_extract_argv(command: str | None = None) -> tuple[tuple[str, ...], str]:
    """Return ``(argv, kind)`` for built-in, JSON argv, or legacy shell config."""
    if command and command.strip():
        kind = "legacy_windows" if sys.platform == "win32" else "legacy"
        return _legacy_extract_argv(command.strip()), kind

    configured_argv = os.getenv("BROWSER_EXTRACT_ARGV", "").strip()
    if configured_argv:
        return _parse_browser_extract_argv(configured_argv), "json"

    configured_command = os.getenv("BROWSER_EXTRACT_CMD", "").strip()
    if configured_command:
        kind = "legacy_windows" if sys.platform == "win32" else "legacy"
        return _legacy_extract_argv(configured_command), kind
    legacy_command = os.getenv("WSL_EXTRACT_CMD", "").strip()
    if legacy_command:
        kind = "legacy_windows" if sys.platform == "win32" else "legacy"
        return _legacy_extract_argv(legacy_command), kind

    if sys.platform == "win32":
        return _DEFAULT_WSL_EXTRACT_ARGV, "builtin"
    return _DEFAULT_POSIX_EXTRACT_ARGV, "builtin"


def create_browser_extract_fn(command: str | None = None):
    """Create an async extract function for the configured browser extractor.

    Returns ``async (url: str, max_length: int) -> str`` suitable for
    ``LegacyBrowserExtractBackend``.  No caching — callers (browser session
    manager) manage their own snapshot state.
    """
    try:
        command_argv, command_kind = _resolve_browser_extract_argv(command)
    except ValueError as exc:
        command_error = str(exc)
        command_argv = ()
        command_kind = ""
    else:
        command_error = ""

    async def _extract(url: str, max_length: int = 12000) -> str:
        if not url or not str(url).strip():
            return "[Browser Error] url is required"
        if command_error:
            return f"[Browser Error] {command_error}"
        url_text = str(url)
        if (
            command_kind == "legacy_windows"
            and any(char in url_text for char in _WINDOWS_LEGACY_UNSUPPORTED_URL_CHARS)
        ):
            return (
                "[Browser Error] Windows legacy browser extractor URL contains unsupported "
                "raw quote or line-control characters; percent-encode them or use BROWSER_EXTRACT_ARGV"
            )
        windows_process: _WindowsLegacyBrowserProcess | None = None
        try:
            process_argv = command_argv
            process_kwargs = {}
            if command_kind == "legacy_windows":
                child_env = os.environ.copy()
                # The closing CMD quote is also consumed by the target's
                # Windows argv parser: double terminal backslashes before it.
                trailing_backslashes = len(url_text) - len(url_text.rstrip("\\"))
                child_env[_WINDOWS_LEGACY_URL_ENV] = url_text + "\\" * trailing_backslashes
                process_kwargs["env"] = child_env
            else:
                process_argv = (*command_argv, url_text)
            if command_kind == "legacy_windows":
                windows_process = await _create_windows_legacy_subprocess(*process_argv, **process_kwargs)
                proc = windows_process
            else:
                proc = await asyncio.create_subprocess_exec(
                    *process_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=hidden_process_creationflags(),
                )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            text = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                return f"[Browser Error] Browser extractor failed (exit {proc.returncode}): {err[:300]}"
            if not text:
                return f"[Browser Error] Browser extractor returned empty content. stderr: {err[:300]}"
            if max_length > 0 and len(text) > max_length:
                text = text[:max_length] + "\n…[truncated]"
            return text
        except asyncio.TimeoutError:
            return "[Browser Error] Browser extractor timed out (60s)"
        except Exception as e:
            return f"[Browser Error] Browser extractor failed: {e}"
        finally:
            if windows_process is not None:
                windows_process._close()

    return _extract


def create_browser_status_fn(command: str | None = None):
    """Create an async status function for the configured browser extractor.

    Returns ``async () -> tuple[bool, str]`` suitable for
    ``LegacyBrowserExtractBackend``.
    """
    command_argv: tuple[str, ...]
    try:
        command_argv, command_kind = _resolve_browser_extract_argv(command)
    except ValueError as exc:
        command_error = str(exc)
        command_argv = ()
        command_kind = ""
    else:
        command_error = ""
    status_command = os.getenv("BROWSER_EXTRACT_STATUS_CMD", "").strip()

    async def _status() -> tuple[bool, str]:
        if command_error:
            return False, command_error
        if not command_argv:
            return False, "Browser extractor command is empty"
        if command_kind != "builtin" and not status_command:
            executable = os.path.expanduser(command_argv[0])
            if os.path.isfile(executable) or shutil.which(executable):
                return True, f"Browser extractor ready: {command_argv[0]}"
            return False, f"Browser extractor unavailable: {command_argv[0]}"

        status_argv = (
            (*command_argv, _BROWSER_EXTRACT_STATUS_ARGUMENT)
            if command_kind == "builtin"
            else _legacy_status_argv(status_command)
        )
        windows_process: _WindowsLegacyBrowserProcess | None = None
        try:
            if sys.platform == "win32" and command_kind != "builtin":
                windows_process = await _create_windows_legacy_subprocess(*status_argv)
                proc = windows_process
            else:
                proc = await asyncio.create_subprocess_exec(
                    *status_argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    creationflags=hidden_process_creationflags(),
                )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            output = stdout.decode("utf-8", errors="replace").strip()
            error = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode == 0:
                detail = output.splitlines()[0] if output else command_argv[0]
                return True, f"Browser extractor available: {detail}"
            detail = error or output or f"exit {proc.returncode}"
            return False, f"Browser extractor unavailable: {detail[:300]}"
        except asyncio.TimeoutError:
            return False, "Browser extractor status check timed out"
        except Exception as e:
            return False, f"Browser extractor status check failed: {e}"
        finally:
            if windows_process is not None:
                windows_process._close()

    return _status


# Compatibility aliases retained for existing callers and configuration docs.
create_wsl_extract_fn = create_browser_extract_fn
create_wsl_status_fn = create_browser_status_fn


def resolve_exa_api_key() -> tuple[str, str]:
    """Resolve Exa credentials from Astra's environment or an explicit dotenv.

    ``EXA_API_KEY`` has priority. ``EXA_ENV_FILE`` may point at a dotenv file
    outside the project when the user configures one explicitly; only the
    EXA_API_KEY entry is read and the secret is never included in diagnostics.
    """
    direct = os.getenv("EXA_API_KEY", "").strip()
    if direct:
        return direct, "EXA_API_KEY"

    source_file = os.getenv("EXA_ENV_FILE", "").strip()
    if not source_file:
        return "", "not configured"
    try:
        from dotenv import dotenv_values

        value = dotenv_values(Path(source_file)).get("EXA_API_KEY")
    except (ImportError, OSError, UnicodeError, ValueError):
        value = None
    resolved = str(value or "").strip()
    return (resolved, "EXA_ENV_FILE (EXA_API_KEY)") if resolved else ("", "EXA_ENV_FILE missing EXA_API_KEY")


def exa_configuration_status() -> tuple[bool, str]:
    key, source = resolve_exa_api_key()
    return bool(key), source


async def _is_safe_public_url(url: str) -> bool:
    """Reject credentials, localhost, and private/internal network targets."""
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False

    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False

    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
        except (OSError, socket.gaierror):
            return False
        addresses = []
        for info in infos:
            try:
                addresses.append(ipaddress.ip_address(info[4][0]))
            except ValueError:
                return False

    return bool(addresses) and all(address.is_global for address in addresses)


class SearchProviderState:
    """Mutable process-local default controlled by the /search command."""

    VALID = ("auto", "exa", "searxng")

    def __init__(self, provider: str = "auto"):
        self.set(provider)

    def set(self, provider: str) -> str:
        normalized = (provider or "auto").strip().lower()
        if normalized not in self.VALID:
            raise ValueError(f"Unknown search provider: {provider}")
        self.provider = normalized
        return normalized


def register_web_tools(registry: ToolRegistry, sandbox, default_provider: str | None = None) -> SearchProviderState:
    """注册联网工具"""
    searxng_url = os.getenv("SEARXNG_URL", "http://localhost:8080").rstrip("/")
    firecrawl_api_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    configured_firecrawl_url = os.getenv("FIRECRAWL_URL", "").strip()
    firecrawl_url = (
        configured_firecrawl_url
        or ("https://api.firecrawl.dev" if firecrawl_api_key else "http://localhost:3002")
    ).rstrip("/")
    browser_extract_fn = create_browser_extract_fn()
    browser_status_fn = create_browser_status_fn()
    search_cache_ttl = _positive_int_env("WEB_SEARCH_CACHE_TTL", 600)
    url_cache_ttl = _positive_int_env("WEB_URL_CACHE_TTL", 1800)
    search_timeout = _positive_int_env("WEB_SEARCH_TIMEOUT", 8)
    url_timeout = _positive_int_env("WEB_URL_TIMEOUT", 12)
    cache_max_entries = _positive_int_env("WEB_CACHE_MAX_ENTRIES", 256)
    inline_fetch_timeout_ms = _positive_int_env("WEB_INLINE_FETCH_TIMEOUT", 3000)
    firecrawl_timeout = _positive_int_env("FIRECRAWL_TIMEOUT", 60)
    exa_api_url = os.getenv("EXA_API_URL", "https://api.exa.ai").rstrip("/")
    exa_timeout = _positive_int_env("EXA_SEARCH_TIMEOUT", 15)
    tavily_api_url = os.getenv("TAVILY_API_URL", "https://api.tavily.com").rstrip("/")
    parallel_api_url = os.getenv("PARALLEL_API_URL", "https://api.parallel.ai").rstrip("/")
    extract_api_timeout = _positive_int_env("WEB_EXTRACT_API_TIMEOUT", 30)
    configured_extract_provider = os.getenv("WEB_EXTRACT_PROVIDER", "auto").strip().lower() or "auto"
    extract_cache_dir = Path(os.getenv("WEB_EXTRACT_CACHE_DIR", ".astra/cache/web")).expanduser()
    configured_provider = default_provider or os.getenv("WEB_SEARCH_PROVIDER", "auto")
    search_provider_state = SearchProviderState(configured_provider)
    exa_search_type = os.getenv("EXA_SEARCH_TYPE", "auto").strip().lower() or "auto"

    search_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
    url_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
    shared_clients = {}
    atexit.register(_close_httpx_clients_sync, shared_clients)
    # ── helpers ────────────────────────────────────────────────────

    def _is_chinese(text: str) -> bool:
        return bool(re.search(r"[\u4e00-\u9fff]", text))

    def _cache_get(cache: OrderedDict[str, tuple[float, str]], key: str, ttl: int) -> str | None:
        if ttl <= 0:
            return None
        item = cache.get(key)
        if not item:
            return None
        created_at, value = item
        if time.monotonic() - created_at > ttl:
            cache.pop(key, None)
            return None
        cache.move_to_end(key)
        return value

    def _cache_set(cache: OrderedDict[str, tuple[float, str]], key: str, value: str, ttl: int) -> None:
        if ttl > 0:
            cache[key] = (time.monotonic(), value)
            cache.move_to_end(key)
            while len(cache) > cache_max_entries:
                cache.popitem(last=False)

    def _client_kwargs(timeout: int, proxy_url: str | None) -> dict:
        kwargs = {
            "timeout": timeout,
            "follow_redirects": True,
            "trust_env": False,
            "headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                )
            },
        }
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return kwargs

    async def _http_get(
        url: str,
        timeout: int = 20,
        *,
        require_same_origin: bool = False,
    ) -> tuple[str | None, str | None]:
        """Async HTTP GET. Returns (body, error)."""
        if httpx is None:
            return await asyncio.to_thread(
                _http_get_urllib,
                url,
                timeout,
                active_proxy_for_url(url),
                require_same_origin,
            )
        try:
            proxy_url = active_proxy_for_url(url)
            client_key = proxy_url or "direct"
            if client_key not in shared_clients:
                shared_clients[client_key] = httpx.AsyncClient(
                    **_client_kwargs(max(search_timeout, url_timeout), proxy_url)
                )
            resp = await shared_clients[client_key].get(url, timeout=timeout)
            if hasattr(resp, "raise_for_status"):
                resp.raise_for_status()
            if require_same_origin:
                final_url = str(getattr(resp, "url", "") or "")
                if normalized_origin(final_url) != normalized_origin(url):
                    return None, f"Cross-origin redirect blocked: {url} -> {final_url or '(unknown)'}"
            return resp.text, None
        except Exception as e:
            return None, str(e)

    async def _http_post_json(
        url: str,
        payload: dict,
        headers: dict[str, str],
        timeout: int,
    ) -> tuple[dict | None, str | None]:
        if httpx is None:
            try:
                data = await asyncio.to_thread(
                    _http_post_json_urllib,
                    url,
                    payload,
                    timeout,
                    headers,
                    active_proxy_for_url(url),
                )
                return data, None
            except Exception as exc:
                return None, str(exc)
        try:
            proxy_url = active_proxy_for_url(url)
            client_key = proxy_url or "direct"
            if client_key not in shared_clients:
                shared_clients[client_key] = httpx.AsyncClient(
                    **_client_kwargs(max(search_timeout, url_timeout, exa_timeout), proxy_url)
                )
            response = await shared_clients[client_key].post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json(), None
        except Exception as exc:
            return None, str(exc)

    def _html_to_text(body: str) -> str:
        if BeautifulSoup is not None:
            try:
                soup = BeautifulSoup(body, "html.parser")
                for node in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
                    node.decompose()
                main = soup.find("article") or soup.find("main") or soup.body or soup
                return re.sub(r"\s+", " ", main.get_text(" ", strip=True)).strip()
            except Exception:
                pass
        body = re.sub(r"<script[^>]*>.*?</script>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        body = re.sub(r"<noscript[^>]*>.*?</noscript>", " ", body, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", body)
        text = html.unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    def _html_to_markdown(body: str) -> str:
        """Extract the main HTML region and preserve useful Markdown structure."""
        if BeautifulSoup is None or markdownify_html is None:
            return _html_to_text(body)
        try:
            soup = BeautifulSoup(body, "html.parser")
            for node in soup(["script", "style", "noscript", "nav", "footer", "header", "aside"]):
                node.decompose()
            main = soup.find("article") or soup.find("main") or soup.body or soup
            markdown = markdownify_html(
                str(main),
                heading_style="ATX",
                bullets="-",
                strip=["html", "body"],
            )
            markdown = re.sub(r"[ \t]+\n", "\n", markdown)
            markdown = re.sub(r"\n{3,}", "\n\n", markdown)
            return markdown.strip()
        except Exception:
            return _html_to_text(body)

    def _looks_like_spa_or_antibot(body: str, text: str) -> bool:
        if len(text) < 120:
            return True
        lower = body.lower()
        signals = (
            "id=\"root\"",
            "id=\"app\"",
            "__next",
            "nuxt",
            "cloudflare",
            "cf-chl",
            "enable javascript",
            "please enable js",
            "captcha",
        )
        return any(signal in lower for signal in signals) and len(text) < 1000

    async def _cached_url_text(url: str, timeout: int | None = None) -> tuple[str | None, str | None]:
        require_same_origin = registry.approval_handler is not None
        # An unguarded cache entry has no final-origin evidence. Keep it
        # separate from entries fetched while interactive approval is active.
        cache_key = f"{'guarded' if require_same_origin else 'raw'}:{url}"
        cached = _cache_get(url_cache, cache_key, url_cache_ttl)
        if cached is not None:
            return cached, None
        body, err = await _http_get(
            url,
            timeout or url_timeout,
            require_same_origin=require_same_origin,
        )
        if body is not None:
            _cache_set(url_cache, cache_key, body, url_cache_ttl)
        return body, err

    def _format_content(url: str, text: str, max_length: int) -> str:
        if len(text) > max_length:
            text = text[:max_length] + "\n\n... (truncated)"
        return f"Content from: {url}\n{'=' * 50}\n\n{text}"

    def _fetched_label() -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

    def _is_inline_fetchable_url(url: str) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        path = urllib.parse.urlparse(url).path.lower()
        blocked_suffixes = (
            ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".mp3",
            ".zip", ".rar", ".7z", ".tar", ".gz", ".doc", ".docx", ".xls", ".xlsx",
        )
        return not path.endswith(blocked_suffixes)

    async def _browser_extract(url: str, max_length: int = 12000) -> str:
        if registry.approval_handler is not None:
            return (
                "[Extract Error] Approved-origin extraction requires a backend that reports "
                "its final URL. The legacy browser extractor was not executed."
            )
        return await browser_extract_fn(url, max_length)

    async def _browser_extract_status() -> tuple[bool, str]:
        return await browser_status_fn()

    # ── search_web ──────────────────────────────────────────────────

    async def _inline_result_contents(urls: list[str], max_length: int) -> dict[str, str]:
        fetchable_urls = [url for url in urls if _is_inline_fetchable_url(url)]
        tasks = [asyncio.create_task(_fetch_result_content(url, max_length)) for url in fetchable_urls]
        if not tasks:
            return {}
        done, pending = await asyncio.wait(tasks, timeout=inline_fetch_timeout_ms / 1000)
        for task in pending:
            task.cancel()
        inline_texts = {}
        for url, task in zip(fetchable_urls, tasks):
            if task in done and not task.cancelled():
                try:
                    text = task.result()
                except Exception:
                    text = ""
                if text:
                    inline_texts[url] = text
        return inline_texts

    async def _fetch_result_content(url: str, max_length: int) -> str:
        if not _is_inline_fetchable_url(url):
            return ""
        body, err = await _cached_url_text(url)
        if err or not body:
            return ""
        text = _html_to_text(body)
        if len(text) < 50:
            return ""
        return text[:max_length] + ("..." if len(text) > max_length else "")

    def _auto_prefers_exa(query: str, category: str) -> bool:
        if category in {"science", "news"}:
            return True
        signals = (
            "今日", "今天", "最新", "新闻", "研究", "论文", "报告", "公司", "行业",
            "模型", "发布", "测评", "评价", "今日", "今天", "最新", "新闻", "研究", "论文", "报告", "公司", "行业",
            "model", "release", "review", "today", "latest", "news", "research", "paper", "report", "company",
            "benchmark", "market", "arxiv",
        )
        lowered = query.lower()
        return any(signal in lowered for signal in signals)

    def _select_search_provider(query: str, category: str, requested: str) -> tuple[str, bool]:
        requested = (requested or "auto").strip().lower()
        configured = search_provider_state.provider if requested == "auto" else requested
        if configured not in {"auto", "exa", "searxng"}:
            configured = "auto"
        if configured == "exa":
            return "exa", False
        if configured == "searxng":
            return "searxng", False
        exa_key, _ = resolve_exa_api_key()
        if exa_key and _auto_prefers_exa(query, category):
            return "exa", True
        return "searxng", True

    async def _search_exa(query: str, max_results: int, category: str) -> tuple[str | None, str | None]:
        api_key, _ = resolve_exa_api_key()
        if not api_key:
            return None, "EXA_API_KEY is not configured"

        payload: dict = {
            "query": query,
            "numResults": min(max_results, 100),
            "type": exa_search_type,
            "contents": {"highlights": True},
        }
        category_map = {"science": "research paper", "news": "news"}
        if category in category_map:
            payload["category"] = category_map[category]

        data, error = await _http_post_json(
            f"{exa_api_url}/search",
            payload,
            {"x-api-key": api_key, "Content-Type": "application/json"},
            exa_timeout,
        )
        if error or not data:
            return None, error or "empty Exa response"
        results = data.get("results", [])
        if not isinstance(results, list):
            return None, "invalid Exa response"

        lines = [f"Search: {query}", f"Provider: Exa ({data.get('resolvedSearchType') or exa_search_type})", ""]
        count = 0
        for result in results:
            if not isinstance(result, dict):
                continue
            url = str(result.get("url") or "").strip()
            title = str(result.get("title") or "").strip()
            if not url or not (title or result.get("highlights")):
                continue
            count += 1
            highlights = result.get("highlights") or []
            if isinstance(highlights, str):
                highlights = [highlights]
            summary = " ".join(str(item).strip() for item in highlights if str(item).strip())
            if not summary:
                summary = str(result.get("text") or "").strip()
            lines.append(f"[{count}] {title or '(no title)'}")
            lines.append(f"    URL: {url}")
            if result.get("publishedDate"):
                lines.append(f"    Published: {result['publishedDate']}")
            if result.get("author"):
                lines.append(f"    Author: {result['author']}")
            if summary:
                lines.append(f"    Snippet: {summary[:700]}" + ("..." if len(summary) > 700 else ""))
            lines.append("")
            if count >= max_results:
                break
        if count == 0:
            return f"No Exa results for '{query}'.", None
        return "\n".join(lines).strip(), None

    async def _search_web(
        query: str,
        max_results: int = 5,
        language: str = "auto",
        category: str = "general",
        engine: str = "",
        page: int = 1,
        include_content: bool = False,
        content_results: int = 2,
        content_max_length: int = 1200,
        provider: str = "auto",
    ) -> str:
        """Return lightweight candidates from Exa or SearXNG."""
        if not query.strip():
            return "[Error] Empty query"

        max_results = min(max(max_results, 1), 50)
        page = min(max(page, 1), 5)
        content_results = min(max(content_results, 0), max_results)
        content_max_length = min(max(content_max_length, 200), 5000)

        if language == "auto":
            language = "zh" if _is_chinese(query) else "en"

        selected_provider, allow_fallback = _select_search_provider(query, category, provider)
        if selected_provider == "exa":
            exa_cache_key = json.dumps(
                {
                    "provider": "exa",
                    "query": query,
                    "max_results": max_results,
                    "category": category,
                    "type": exa_search_type,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            cached = _cache_get(search_cache, exa_cache_key, search_cache_ttl)
            if cached is not None:
                return cached
            exa_output, exa_error = await _search_exa(query, max_results, category)
            if exa_output is not None:
                if not (allow_fallback and exa_output.startswith("No Exa results")):
                    _cache_set(search_cache, exa_cache_key, exa_output, search_cache_ttl)
                    return exa_output
            if not allow_fallback:
                return f"[Exa Error] {exa_error}"

        params = {
            "q": query,
            "format": "json",
            "language": language,
            "safesearch": 0,
            "categories": category,
            "pageno": str(page),
        }
        if engine:
            params["engines"] = engine

        cache_key = json.dumps(
            {
                "params": params,
                "max_results": max_results,
                "include_content": include_content,
                "content_results": content_results,
                "content_max_length": content_max_length,
                "provider": "searxng",
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        cached = _cache_get(search_cache, cache_key, search_cache_ttl)
        if cached is not None:
            return cached

        url = f"{searxng_url}/search?{urllib.parse.urlencode(params)}"
        body, err = await _http_get(url, search_timeout)
        if err or not body:
            if allow_fallback:
                exa_output, exa_error = await _search_exa(query, max_results, category)
                if exa_output is not None:
                    return exa_output
                return f"[Search Error] SearXNG unavailable ({err or 'empty response'}); Exa: {exa_error}"
            return f"[Network Error] Cannot reach SearXNG ({searxng_url}): {err or 'empty response'}"

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return "[Parse Error] Invalid JSON from SearXNG"
        except Exception as e:
            return f"[Error] {type(e).__name__}: {e}"

        results = data.get("results", [])
        suggestions = data.get("suggestions", [])
        answers = data.get("answers", [])
        infoboxes = data.get("infoboxes", [])
        total = data.get("number_of_results", len(results))
        engines_responsive = data.get("engines_responsive", [])

        if not results and not suggestions and not answers:
            if allow_fallback:
                exa_output, exa_error = await _search_exa(query, max_results, category)
                if exa_output is not None:
                    return exa_output
                return f"No SearXNG results for '{query}'; Exa: {exa_error}."
            return f"No results for '{query}' (lang={language}, category={category}, page={page})."

        readable_results = []
        seen_urls = set()
        domain_counts = {}
        for res in results:
            title = res.get("title", "").strip()
            content = res.get("content", "").strip()
            result_url = res.get("url", "").strip()
            if not (title or content) or not result_url:
                continue
            try:
                parsed = urllib.parse.urlsplit(result_url)
                normalized_url = urllib.parse.urlunsplit((
                    parsed.scheme.lower(),
                    parsed.netloc.lower(),
                    parsed.path.rstrip("/") or "/",
                    parsed.query,
                    "",
                ))
                domain = (parsed.hostname or "").lower()
            except ValueError:
                continue
            if normalized_url in seen_urls or domain_counts.get(domain, 0) >= 2:
                continue
            seen_urls.add(normalized_url)
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            readable_results.append(res)
            if len(readable_results) >= max_results:
                break

        inline_texts: dict[str, str] = {}
        if include_content and content_results:
            urls = [res.get("url", "") for res in readable_results[:content_results]]
            inline_texts = await _inline_result_contents(urls, content_max_length)

        lines = []
        lines.append(f"Search: {data.get('query', query)}")
        reported_engines = list(engines_responsive)
        if not reported_engines:
            for res in readable_results:
                for name in res.get("engines", []) or [res.get("engine", "")]:
                    if name and name not in reported_engines:
                        reported_engines.append(name)
        if reported_engines:
            lines.append(
                f"Engines: {', '.join(reported_engines[:8])}"
                + (f" +{len(reported_engines) - 8} more" if len(reported_engines) > 8 else "")
            )
        if total:
            lines.append(f"Total: {total} | Page {page} | {language}")
        lines.append("")

        for ans in answers[:3]:
            lines.append(f"💡 {ans}")
            lines.append("")

        for ib in infoboxes[:2]:
            title = ib.get("infobox", ib.get("title", ""))
            content = ib.get("content", "")
            if title:
                lines.append(f"📌 {title}")
                if content:
                    lines.append(f"   {content[:300]}")
                for u in ib.get("urls", [])[:2]:
                    lines.append(f"   🔗 {u.get('title', 'link')}: {u.get('url', '')}")
                lines.append("")

        for count, res in enumerate(readable_results, start=1):
            title = res.get("title", "").strip()
            result_url = res.get("url", "")
            content = res.get("content", "").strip()
            engine_name = res.get("engine", "")
            published = res.get("publishedDate", "")

            lines.append(f"[{count}] {title or '(no title)'}")
            if result_url:
                lines.append(f"    URL: {result_url}")
            if content:
                snippet = content[:280] + ("..." if len(content) > 280 else "")
                lines.append(f"    Snippet: {snippet}")
            if result_url in inline_texts:
                lines.append(f"    Page content: {inline_texts[result_url]}")
                lines.append(f"    Fetched: {_fetched_label()}")
            meta = []
            if engine_name:
                meta.append(f"via {engine_name}")
            if published:
                meta.append(published[:10])
            if meta:
                lines.append(f"    ({', '.join(meta)})")
            lines.append("")

        if suggestions and len(readable_results) < max_results:
            lines.append("Related searches:")
            for sug in suggestions[:5]:
                lines.append(f"  • {sug}")
            lines.append("")

        output = "\n".join(lines).strip()
        _cache_set(search_cache, cache_key, output, search_cache_ttl)
        return output

    # ── web_extract provider waterfall ─────────────────────────────

    def _extract_result(
        *,
        requested_url: str,
        backend: str,
        title: str = "",
        content: str = "",
        final_url: str = "",
        error: str | None = None,
    ) -> dict:
        return {
            "url": final_url or requested_url,
            "title": title,
            "content": content,
            "backend": backend,
            "error": error,
        }

    def _align_api_results(
        urls: list[str],
        backend: str,
        items: object,
        *,
        content_keys: tuple[str, ...],
        failed_items: object = None,
    ) -> list[dict]:
        successful = [item for item in items or [] if isinstance(item, dict)] if isinstance(items, list) else []
        failed = [item for item in failed_items or [] if isinstance(item, dict)] if isinstance(failed_items, list) else []
        by_url = {
            str(item.get("url") or item.get("id") or ""): item
            for item in successful
            if item.get("url") or item.get("id")
        }
        failed_by_url = {
            str(item.get("url") or item.get("id") or ""): item
            for item in failed
            if item.get("url") or item.get("id")
        }
        unclaimed = [item for item in successful if item not in by_url.values() or str(item.get("url") or item.get("id") or "") not in urls]
        aligned = []
        for url in urls:
            item = by_url.get(url)
            if item is None and unclaimed:
                item = unclaimed.pop(0)
            if item is not None:
                content = next(
                    (str(item.get(key) or "") for key in content_keys if item.get(key)),
                    "",
                )
                if not content and isinstance(item.get("excerpts"), list):
                    content = "\n\n".join(str(value) for value in item["excerpts"] if value)
                if content:
                    aligned.append(_extract_result(
                        requested_url=url,
                        backend=backend,
                        title=str(item.get("title") or ""),
                        content=content,
                        final_url=str(item.get("url") or url),
                    ))
                    continue
            failure = failed_by_url.get(url) or {}
            error = (
                failure.get("error")
                or failure.get("content")
                or failure.get("message")
                or f"{backend} returned no content"
            )
            aligned.append(_extract_result(
                requested_url=url,
                backend=backend,
                error=str(error),
            ))
        return aligned

    async def _tavily_extract(urls: list[str], _max_chars: int) -> list[dict]:
        api_key = os.getenv("TAVILY_API_KEY", "").strip()
        if not api_key:
            return [_extract_result(requested_url=url, backend="tavily", error="TAVILY_API_KEY is not configured") for url in urls]
        data, error = await _http_post_json(
            f"{tavily_api_url}/extract",
            {
                "urls": urls,
                "extract_depth": os.getenv("TAVILY_EXTRACT_DEPTH", "basic").strip().lower() or "basic",
                "format": "markdown",
                "include_images": False,
            },
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            extract_api_timeout,
        )
        if error or not data:
            return [_extract_result(requested_url=url, backend="tavily", error=error or "empty Tavily response") for url in urls]
        return _align_api_results(
            urls,
            "tavily",
            data.get("results"),
            content_keys=("raw_content", "content"),
            failed_items=data.get("failed_results"),
        )

    async def _exa_extract(urls: list[str], _max_chars: int) -> list[dict]:
        api_key, _ = resolve_exa_api_key()
        if not api_key:
            return [_extract_result(requested_url=url, backend="exa", error="EXA_API_KEY is not configured") for url in urls]
        data, error = await _http_post_json(
            f"{exa_api_url}/contents",
            {"urls": urls, "text": True},
            {"x-api-key": api_key, "Content-Type": "application/json"},
            extract_api_timeout,
        )
        if error or not data:
            return [_extract_result(requested_url=url, backend="exa", error=error or "empty Exa response") for url in urls]
        return _align_api_results(
            urls,
            "exa",
            data.get("results"),
            content_keys=("text", "content"),
        )

    async def _parallel_extract(urls: list[str], max_chars: int) -> list[dict]:
        api_key = os.getenv("PARALLEL_API_KEY", "").strip()
        if not api_key:
            return [_extract_result(requested_url=url, backend="parallel", error="PARALLEL_API_KEY is not configured") for url in urls]
        data, error = await _http_post_json(
            f"{parallel_api_url}/v1/extract",
            {"urls": urls, "max_chars_total": max_chars * len(urls)},
            {"x-api-key": api_key, "Content-Type": "application/json"},
            extract_api_timeout,
        )
        if error or not data:
            return [_extract_result(requested_url=url, backend="parallel", error=error or "empty Parallel response") for url in urls]
        return _align_api_results(
            urls,
            "parallel",
            data.get("results"),
            content_keys=("full_content", "content"),
            failed_items=data.get("errors"),
        )

    async def _firecrawl_scrape(url: str, _max_chars: int) -> dict:
        endpoint = f"{firecrawl_url}/v1/scrape"
        payload = {
            "url": url,
            "formats": ["markdown"],
            "onlyMainContent": True,
        }
        headers = {"Content-Type": "application/json"}
        if firecrawl_api_key:
            headers["Authorization"] = f"Bearer {firecrawl_api_key}"
        data, request_error = await _http_post_json(
            endpoint,
            payload,
            headers,
            firecrawl_timeout,
        )
        if request_error or not data:
            return _extract_result(
                requested_url=url,
                backend="firecrawl",
                error=request_error or "empty Firecrawl response",
            )

        if not isinstance(data, dict) or not data.get("success"):
            error = (
                data.get("error", "Firecrawl returned an unsuccessful response")
                if isinstance(data, dict)
                else "Invalid Firecrawl response"
            )
            return _extract_result(requested_url=url, backend="firecrawl", error=str(error))

        page = data.get("data") or {}
        metadata = page.get("metadata") or {}
        content = page.get("markdown") or page.get("content") or ""
        return _extract_result(
            requested_url=url,
            backend="firecrawl",
            final_url=page.get("url") or metadata.get("sourceURL") or url,
            title=metadata.get("title") or page.get("title") or "",
            content=content,
            error=None if content else "Firecrawl returned no content",
        )

    async def _firecrawl_extract(urls: list[str], max_chars: int) -> list[dict]:
        return await asyncio.gather(*(_firecrawl_scrape(url, max_chars) for url in urls))

    async def _http_extract(urls: list[str], _max_chars: int) -> list[dict]:
        async def extract_one(url: str) -> dict:
            # Direct fallback must not follow an approved public URL into a
            # different origin where the original SSRF decision no longer applies.
            body, error = await _http_get(
                url,
                url_timeout,
                require_same_origin=True,
            )
            if error or not body:
                return _extract_result(
                    requested_url=url,
                    backend="http",
                    error=error or "empty HTTP response",
                )
            content = _html_to_markdown(body)
            if len(content) < 50 or _looks_like_spa_or_antibot(body, content):
                return _extract_result(
                    requested_url=url,
                    backend="http",
                    error="Static HTTP content is empty/minimal or requires JavaScript",
                )
            return _extract_result(
                requested_url=url,
                backend="http",
                content=content,
            )

        return await asyncio.gather(*(extract_one(url) for url in urls))

    extractors = {
        "tavily": _tavily_extract,
        "exa": _exa_extract,
        "parallel": _parallel_extract,
        "firecrawl": _firecrawl_extract,
        "http": _http_extract,
    }

    def _extract_provider_chain(requested: str) -> list[str]:
        order = ["tavily", "exa", "parallel", "firecrawl", "http"]
        requested = (requested or configured_extract_provider or "auto").strip().lower()
        if requested not in {"auto", *order}:
            requested = "auto"
        availability = {
            "tavily": bool(os.getenv("TAVILY_API_KEY", "").strip()),
            "exa": bool(resolve_exa_api_key()[0]),
            "parallel": bool(os.getenv("PARALLEL_API_KEY", "").strip()),
            "firecrawl": bool(firecrawl_url),
            "http": True,
        }
        start = 0 if requested == "auto" else order.index(requested)
        return [backend for backend in order[start:] if availability[backend]]

    def _replace_base64_images(content: str) -> str:
        content = re.sub(
            r"!\[([^\]]*)\]\(data:image/[^;,]+;base64,[^)]+\)",
            lambda match: f"[IMAGE: {match.group(1) or 'inline image'}]",
            content,
            flags=re.IGNORECASE,
        )
        return re.sub(
            r"data:image/[^;,]+;base64,[A-Za-z0-9+/=\s]+",
            "[IMAGE: inline image]",
            content,
            flags=re.IGNORECASE,
        )

    def _truncate_and_store(url: str, content: str, max_chars: int) -> tuple[str, str]:
        content = _replace_base64_images(content)
        if len(content) <= max_chars:
            return content, ""

        cache_path = extract_cache_dir.resolve() / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()[:20]}.md"
        stored_path = ""
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(f".{os.getpid()}.{time.time_ns()}.tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(cache_path)
            stored_path = str(cache_path)
        except OSError:
            stored_path = ""

        head_chars = max(1, int(max_chars * 0.75))
        tail_chars = max(1, max_chars - head_chars)
        head = content[:head_chars].rsplit("\n", 1)[0] or content[:head_chars]
        tail = content[-tail_chars:].split("\n", 1)[-1] or content[-tail_chars:]
        footer = "\n\n... (middle truncated)"
        if stored_path:
            footer += (
                f"\n\nFull content saved to: {stored_path}"
                f'\nUse read_file with {{"path": "{stored_path}", "offset": 0, "limit": 200}} to page through it.'
            )
        return f"{head}{footer}\n\n{tail}", stored_path

    async def _attempt_extract_candidates(
        candidates: dict[int, str],
        chain: list[str],
        max_chars: int,
    ) -> tuple[dict[int, dict], dict[int, str], dict[int, list[str]]]:
        """Run one bounded provider waterfall for one candidate per result slot."""

        completed: dict[int, dict] = {}
        pending = dict(candidates)
        errors = {index: [] for index in candidates}
        for backend in chain:
            if not pending:
                break
            indexes = list(pending)
            batch_results = await extractors[backend](
                [pending[index] for index in indexes],
                max_chars,
            )
            for index, result in zip(indexes, batch_results):
                content = str(result.get("content") or "")
                if result.get("error") or not content:
                    detail = str(result.get("error") or "empty content")
                    errors[index].append(
                        f"{backend}: {detail[:500]}"
                    )
                    continue

                final_url = str(result.get("url") or pending[index])
                if not await _is_safe_public_url(final_url):
                    errors[index].append(
                        f"{backend}: redirected to an unsafe/private URL"
                    )
                    continue

                final_content, stored_path = _truncate_and_store(
                    final_url,
                    content,
                    max_chars,
                )
                result["content"] = final_content
                result["fallback_from"] = [
                    item.split(":", 1)[0] for item in errors[index]
                ]
                if stored_path:
                    result["full_content_path"] = stored_path
                completed[index] = result
                pending.pop(index, None)
        return completed, pending, errors

    def _fallback_labels(errors: list[str]) -> list[str]:
        labels = []
        for error in errors:
            label = (
                "search"
                if error.startswith(("search candidate ", "search recovery:"))
                else error.split(":", 1)[0]
            )
            if label and label not in labels:
                labels.append(label)
        return labels

    def _recovery_search_query(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        path_parts = [
            urllib.parse.unquote(part)
            for part in parsed.path.split("/")
            if part
        ]
        topic = " ".join(path_parts[-2:]) or parsed.hostname or url
        topic = re.sub(r"[-_]+", " ", topic).strip()[:160]
        return f'"{topic}" {parsed.hostname or ""}'.strip()

    async def _recovery_candidates(url: str) -> list[str]:
        search_output = await _search_web(
            _recovery_search_query(url),
            max_results=5,
            include_content=False,
            provider="auto",
        )
        original = urllib.parse.urldefrag(url)[0]
        original_host = (urllib.parse.urlparse(url).hostname or "").lower()
        guarded_origin = (
            normalized_origin(url) if registry.approval_handler is not None else ""
        )
        candidates: list[str] = []
        for match in _SEARCH_RESULT_URL_RE.finditer(search_output):
            candidate = urllib.parse.urldefrag(
                match.group(1).rstrip(".,;)")
            )[0]
            if candidate == original or candidate in candidates:
                continue
            candidate_host = (urllib.parse.urlparse(candidate).hostname or "").lower()
            if candidate_host != original_host:
                continue
            if guarded_origin and normalized_origin(candidate) != guarded_origin:
                continue
            if not await _is_safe_public_url(candidate):
                continue
            candidates.append(candidate)
            if len(candidates) >= _WEB_EXTRACT_RECOVERY_CANDIDATES:
                break
        return candidates

    async def _web_extract(
        urls: list[str],
        max_chars: int = 15000,
        provider: str = "auto",
    ) -> str:
        if not isinstance(urls, list) or not urls:
            return json.dumps(
                {"success": False, "error": "urls must be a non-empty list"},
                ensure_ascii=False,
            )
        urls = [str(url).strip() for url in urls[:5] if str(url).strip()]
        max_chars = min(max(int(max_chars), 500), 50000)
        results: list[dict | None] = [None] * len(urls)
        initial_candidates: dict[int, str] = {}
        for index, url in enumerate(urls):
            if await _is_safe_public_url(url):
                initial_candidates[index] = url
                continue
            results[index] = {
                "url": url,
                "title": "",
                "content": "",
                "backend": "blocked",
                "error": "Blocked: URL targets localhost, credentials, or a private/internal address",
            }

        chain = _extract_provider_chain(provider)
        completed, pending, fallback_errors = await _attempt_extract_candidates(
            initial_candidates,
            chain,
            max_chars,
        )
        for index, result in completed.items():
            results[index] = result

        # An automatic extraction may fail because a search result points at a
        # moved or stale page. Recover only after the complete provider chain
        # fails, and keep both the retry count and candidate provenance visible.
        if pending and (provider or "auto").strip().lower() == "auto":
            candidate_lists = await asyncio.gather(
                *(_recovery_candidates(urls[index]) for index in pending)
            )
            recovery_by_index = dict(zip(pending, candidate_lists))
            for rank in range(_WEB_EXTRACT_RECOVERY_CANDIDATES):
                round_candidates = {
                    index: candidates[rank]
                    for index, candidates in recovery_by_index.items()
                    if index in pending and rank < len(candidates)
                }
                if not round_candidates:
                    continue
                recovered, _, recovery_errors = await _attempt_extract_candidates(
                    round_candidates,
                    chain,
                    max_chars,
                )
                for index, errors in recovery_errors.items():
                    candidate = round_candidates[index]
                    fallback_errors[index].append(
                        f"search candidate {candidate}: "
                        + ("; ".join(errors) or "no content")
                    )
                for index, result in recovered.items():
                    candidate = round_candidates[index]
                    result["requested_url"] = urls[index]
                    result["recovery"] = {
                        "method": "search",
                        "candidate_url": candidate,
                        "rank": rank + 1,
                    }
                    result["fallback_from"] = list(
                        dict.fromkeys([
                            *_fallback_labels(fallback_errors[index]),
                            "search",
                            *result.get("fallback_from", []),
                        ])
                    )
                    results[index] = result
                    pending.pop(index, None)

            for index in pending:
                if not recovery_by_index.get(index):
                    fallback_errors[index].append(
                        "search recovery: no safe alternative URLs found"
                    )

        for index, url in pending.items():
            results[index] = {
                "url": url,
                "title": "",
                "content": "",
                "backend": chain[-1] if chain else "none",
                "fallback_from": _fallback_labels(fallback_errors[index][:-1]),
                "error": "; ".join(fallback_errors[index]) or "No extract backend is available",
            }

        completed_results = [result for result in results if isinstance(result, dict)]
        success = any(
            not result.get("error") and result.get("content")
            for result in completed_results
        )
        return json.dumps(
            {
                "success": success,
                "provider_chain": chain,
                "results": completed_results,
            },
            ensure_ascii=False,
            indent=2,
        )

    # ── fetch_url ───────────────────────────────────────────────────

    async def _search_status() -> str:
        exa_ready, exa_source = exa_configuration_status()
        lines = [
            f"Default search provider: {search_provider_state.provider}",
            f"Extract provider: {configured_extract_provider}",
            f"Extract chain: {' -> '.join(_extract_provider_chain('auto'))}",
            f"Exa: {'configured' if exa_ready else 'not configured'} ({exa_source})",
            f"SearXNG URL: {searxng_url}",
            f"Firecrawl URL: {firecrawl_url}",
        ]

        config_body, config_err = await _http_get(f"{searxng_url}/config", min(search_timeout, 5))
        if config_err or not config_body:
            lines.append(f"SearXNG: error ({config_err or 'empty response'})")
        else:
            lines.append("SearXNG: ok")
            try:
                config = json.loads(config_body)
                formats = config.get("search", {}).get("formats", [])
                if formats:
                    lines.append(f"Formats: {', '.join(str(item) for item in formats)}")
            except json.JSONDecodeError:
                lines.append("Formats: unknown")

        params = urllib.parse.urlencode({"q": "test", "format": "json"})
        search_body, search_err = await _http_get(f"{searxng_url}/search?{params}", min(search_timeout, 5))
        if search_err or not search_body:
            lines.append(f"JSON search: error ({search_err or 'empty response'})")
        else:
            try:
                data = json.loads(search_body)
                results = data.get("results", [])
                lines.append(f"JSON search: ok ({len(results)} results)")
            except json.JSONDecodeError:
                lines.append("JSON search: error (invalid JSON)")

        firecrawl_body, firecrawl_err = await _http_get(f"{firecrawl_url}/", min(firecrawl_timeout, 5))
        if firecrawl_err or not firecrawl_body:
            lines.append(f"Firecrawl: error ({firecrawl_err or 'empty response'})")
        else:
            lines.append("Firecrawl: ok")

        browser_ok, browser_detail = await _browser_extract_status()
        if browser_ok:
            lines.append(f"Browser extractor: ok ({browser_detail})")
        else:
            lines.append(f"Browser extractor: error ({browser_detail})")
        return "\n".join(lines)

    async def _fetch_url(url: str, max_length: int = 8000) -> str:
        """Fetch and extract text from a URL (static HTML only)."""
        if not url.startswith(("http://", "https://")):
            return "[Error] URL must start with http:// or https://"

        max_length = min(max(max_length, 500), 50000)
        body, err = await _cached_url_text(url)
        if err or not body:
            return f"[Error] {err}" if err else "(Empty content)"

        text = _html_to_text(body)
        if not text or len(text) < 50:
            return (f"(Empty/minimal content — page may require JavaScript or is behind anti-bot protection. "
                    f"Try web_extract, or browser_open and browser_snapshot. URL: {url})")

        return _format_content(url, text, max_length)

    # ── extract_url ─────────────────────────────────────────────────

    async def _extract_url(
        url: str,
        max_length: int = 12000,
        browser_first: bool = False,
        min_static_chars: int = 500,
    ) -> str:
        """Extract content with browser fallback for JS/anti-bot pages."""
        if not url.startswith(("http://", "https://")):
            return "[Error] URL must start with http:// or https://"

        max_length = min(max(max_length, 500), 50000)
        min_static_chars = min(max(min_static_chars, 50), 5000)

        if not browser_first:
            body, err = await _cached_url_text(url)
            if body:
                text = _html_to_text(body)
                if len(text) >= min_static_chars and not _looks_like_spa_or_antibot(body, text):
                    return _format_content(url, text, max_length)
            elif err:
                pass

        return await _browser_extract(url, max_length)

    # ── Register all tools ──────────────────────────────────────────

    async def _image_pages(query: str, limit: int) -> tuple[dict | None, str | None]:
        key, _ = resolve_exa_api_key()
        if not key:
            return None, "EXA_API_KEY is not configured"
        data, error = await _http_post_json(
            f"{exa_api_url}/search",
            {"query": query, "numResults": max(10, limit), "type": exa_search_type,
             "contents": {"extras": {"imageLinks": 3}}},
            {"x-api-key": key, "Content-Type": "application/json"},
            exa_timeout,
        )
        return data, "Exa image search failed; check connectivity and Exa configuration." if error else None

    async def _image_preview_available(url: str) -> bool:
        # Check headers only. Never download image bodies into the tool context.
        if httpx is None:
            return False
        try:
            for _ in range(3):
                if not await _is_safe_public_url(url):
                    return False
                proxy_url = active_proxy_for_url(url)
                client_key = proxy_url or "direct"
                if client_key not in shared_clients:
                    shared_clients[client_key] = httpx.AsyncClient(**_client_kwargs(3, proxy_url))
                response = await shared_clients[client_key].head(url, timeout=3, follow_redirects=False)
                if response.is_redirect:
                    url = urllib.parse.urljoin(url, response.headers.get("location", ""))
                    continue
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                length = response.headers.get("content-length", "")
                return (response.status_code == 200 and content_type in {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}
                        and (not length.isdigit() or int(length) >= 2048))
        except Exception:
            return False
        return False

    def _image_client(url: str):
        if httpx is None:
            raise ValueError("Image loading requires httpx.")
        proxy_url = active_proxy_for_url(url)
        client_key = proxy_url or "direct"
        if client_key not in shared_clients:
            shared_clients[client_key] = httpx.AsyncClient(**_client_kwargs(6, proxy_url))
        return shared_clients[client_key]

    async def _load_search_image(url: str) -> Path:
        if httpx is None:
            raise ValueError("Image loading requires httpx.")
        return await load_search_image(
            url, registry.artifact_dir / "search-images", _is_safe_public_url, _image_client,
        )

    register_image_search(registry, _image_pages, _is_safe_public_url, search_cache_ttl,
                          _image_preview_available, _load_search_image)

    registry.register(ToolDef(
        name="search_web",
        description=(
            "通过 Exa 或 SearXNG 搜索网页并返回轻量候选链接（标题、URL、摘要）。"
            "provider=auto 会让新闻、论文、新模型和研究查询优先使用 Exa，并在 Exa/SearXNG 间故障回退；"
            "多个独立查询可同轮并行。摘要足以回答时直接引用；仅在摘要不足或需要核实原文时使用 web_extract。"
            "若仍缺关键证据，可主动使用浏览器访问原站或交互搜索补查，按信息需要自主选择，无需用户点名浏览器。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {"type": "integer", "description": "返回条数 1-50，默认 5", "default": 5},
                "provider": {
                    "type": "string",
                    "enum": ["auto", "exa", "searxng"],
                    "description": "搜索后端；auto 自动路由并允许故障回退",
                    "default": "auto",
                },
                "language": {
                    "type": "string",
                    "enum": ["auto", "zh", "en", "ja", "all"],
                    "description": "语言：auto(自动检测), zh, en, all",
                    "default": "auto",
                },
                "category": {
                    "type": "string",
                    "enum": ["general", "science", "news", "it", "images", "social media"],
                    "description": "类别：general, science(学术), news(新闻)",
                },
                "engine": {"type": "string", "description": "指定引擎：google, baidu, bing, arxiv, wikipedia 等"},
                "page": {"type": "integer", "description": "翻页 1-5，默认 1", "default": 1},
                "include_content": {
                    "type": "boolean",
                    "description": "兼容选项：是否内联少量正文，默认 false；推荐使用 web_extract",
                    "default": False,
                },
                "content_results": {
                    "type": "integer",
                    "description": "兼容模式下内联正文的结果数，默认 2",
                    "default": 2,
                },
                "content_max_length": {
                    "type": "integer",
                    "description": "每个结果内联正文最大字符数，默认 1200",
                    "default": 1200,
                },
            },
            "required": ["query"],
        },
        fn=_search_web, risk="network", approval="never", idempotent=True, parallel_safe=True,
        sandboxed=True, group="web",
    ))

    registry.register(ToolDef(
        name="web_extract",
        description=(
            "通过可配置后端瀑布流读取网页并返回干净 Markdown。auto 按 Tavily、Exa、Parallel、"
            "Firecrawl、直接 HTTP 的顺序选择并逐 URL 故障回退；整条链失败时会从搜索结果中"
            "有界重试最多 2 个公开候选，并在结果中标明恢复来源。一次最多 5 个公开 URL。"
            "提取成功不代表问题已回答；遇到折叠 FAQ、动态内容或需站内搜索等信息缺口，可主动用浏览器交互补查。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 5,
                    "description": "需要读取的完整公开网页 URL，最多 5 个",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "每个网页最多返回字符数 500-50000，默认 15000；超出时保存全文并返回头尾窗口",
                    "default": 15000,
                },
                "provider": {
                    "type": "string",
                    "enum": ["auto", "tavily", "exa", "parallel", "firecrawl", "http"],
                    "description": "首选提取后端；默认 auto，也可通过 WEB_EXTRACT_PROVIDER 配置",
                    "default": "auto",
                },
            },
            "required": ["urls"],
        },
        fn=_web_extract, risk="network", approval="never", idempotent=True, parallel_safe=True,
        sandboxed=True,
        timeout=None,
        group="web",
    ))

    registry.register(ToolDef(
        name="fetch_url",
        description=(
            "抓取静态 HTML 并返回清洗后的文本，不返回原始 HTML，也不执行 JS；script/style 等内容会被移除。"
            "结果未包含答案不代表源 HTML 没有答案。结果带 URL 缓存；需渲染时可用 web_extract，需展开或站内查询时可用浏览器交互。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整 URL（需 http/https 开头）"},
                "max_length": {"type": "integer", "description": "返回最大字符数 500-50000，默认 8000", "default": 8000},
            },
            "required": ["url"],
        },
        fn=_fetch_url, risk="network", approval="never", idempotent=True,
        sandboxed=True, group="web",
    ))

    registry.register(ToolDef(
        name="search_status",
        description="检查联网链路：SearXNG JSON 搜索、Firecrawl 网页提取及跨平台 browser extractor（浏览器提取器）。",
        parameters={"type": "object", "properties": {}},
        fn=_search_status, risk="network", approval="never", idempotent=True,
        sandboxed=True, group="web",
        # Diagnostics only; opt in with AGENT_TOOL_EXPOSE=search_status.
        expose_by_default=False, allow_hidden_execution=True,
    ))

    registry.register(ToolDef(
        name="extract_url",
        description=(
            "提取网页内容；默认先异步静态抓取，遇到 SPA/反爬/短正文自动降级到浏览器。"
            "可传 browser_first=true 直接走浏览器。浏览器提取器优先用 JSON BROWSER_EXTRACT_ARGV 配置；"
            "legacy BROWSER_EXTRACT_CMD 优先于 WSL_EXTRACT_CMD 回退。自定义状态命令由 "
            "BROWSER_EXTRACT_STATUS_CMD 配置；未配置时只检查 executable/wrapper readiness。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整 URL"},
                "max_length": {"type": "integer", "description": "最大字符数，默认 12000", "default": 12000},
                "browser_first": {"type": "boolean", "description": "是否跳过静态抓取直接走浏览器", "default": False},
                "min_static_chars": {
                    "type": "integer",
                    "description": "静态正文少于该字符数时走浏览器，默认 500",
                    "default": 500,
                },
            },
            "required": ["url"],
        },
        fn=_extract_url, risk="network", approval="never", idempotent=True,
        sandboxed=True, group="web",
        # fetch_url and web_extract read pages, the browser tools render them;
        # opt in with AGENT_TOOL_EXPOSE=extract_url.
        expose_by_default=False, allow_hidden_execution=True,
    ))

    return search_provider_state


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


async def _close_httpx_clients(clients: dict) -> None:
    pending = list(clients.values())
    clients.clear()
    for client in pending:
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()


def _close_httpx_clients_sync(clients: dict) -> None:
    if not clients:
        return
    try:
        asyncio.run(_close_httpx_clients(clients))
    except RuntimeError:
        # Interpreter shutdown or an already-running loop can make atexit cleanup
        # best-effort. Normal tool execution keeps using the shared clients.
        clients.clear()


def _http_get_urllib(
    url: str,
    timeout: int,
    http_proxy: str | None,
    require_same_origin: bool = False,
) -> tuple[str | None, str | None]:
    handlers = []
    if http_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": http_proxy, "https": http_proxy}))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            )
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            if require_same_origin:
                final_url = str(resp.geturl() or "")
                if normalized_origin(final_url) != normalized_origin(url):
                    return None, f"Cross-origin redirect blocked: {url} -> {final_url or '(unknown)'}"
            return resp.read().decode("utf-8", errors="replace"), None
    except Exception as e:
        return None, str(e)


def _http_post_json_urllib(
    url: str,
    payload: dict,
    timeout: int,
    headers: dict[str, str] | None = None,
    http_proxy: str | None = None,
) -> dict:
    request_headers = {
        "Content-Type": "application/json",
        "User-Agent": "Agent-System/0.2",
        **(headers or {}),
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    handlers = []
    if http_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": http_proxy, "https": http_proxy}))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))
