"""Opt-in browser acceptance through separate real Astra sandbox tool calls.

ASTRA_PLAYWRIGHT_E2E=1 enables this test. The pinned CLI must already be cached
unless ASTRA_PLAYWRIGHT_E2E_OFFLINE=0 is explicitly set. This test never installs
a browser. ASTRA_PLAYWRIGHT_TEST_CONFIG may name an absolute CLI config file.
"""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid

import pytest

from agent.runtime.tools.image import register_image_tools
from agent.runtime.tools.registry import ToolRegistry
from agent.sandbox.local import LocalSandbox


SCRIPT = (Path(__file__).resolve().parents[1] / ".astra" / "skills" / "development"
          / "web-product-debug" / "scripts" / "playwright_cli.py")
ENABLED = os.getenv("ASTRA_PLAYWRIGHT_E2E") == "1"
PAGE = b"""<!doctype html><meta charset="utf-8"><title>Product debug acceptance</title>
<style>body{font:18px sans-serif;margin:30px}button,input{font:inherit;padding:8px}
.target{position:relative;width:300px;height:70px}.cover{position:absolute;inset:0;z-index:2;background:#dde}</style>
<h1>Product debug acceptance</h1>
<div class="target"><button id="covered">Covered action</button><div class="cover">Blocking overlay</div></div>
<button id="pointer">Pointer action</button>
<label>Project name <input id="name"></label><button id="save">Save draft</button>
<button id="fail">Load failed API</button><pre id="output"></pre>
<script>
const counts={covered:0,pointerdown:0,click:0,saved:'none'};
function render(){document.querySelector('#output').textContent=Object.entries(counts).map(([k,v])=>k+'='+v).join(';');}
document.querySelector('#covered').onclick=()=>{counts.covered++;render();};
document.querySelector('#pointer').addEventListener('pointerdown',()=>{counts.pointerdown++;render();});
document.querySelector('#pointer').onclick=()=>{counts.click++;render();};
document.querySelector('#save').onclick=()=>{counts.saved=document.querySelector('#name').value;render();};
document.querySelector('#fail').onclick=async()=>{const r=await fetch('/api/fail');if(!r.ok)console.error('Fixture API failed: '+r.status);};
render();
</script>"""


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/fail":
            status, content_type, body = 503, "application/json", b'{"error":"intentional fixture error"}'
        elif self.path == "/":
            status, content_type, body = 200, "text/html; charset=utf-8", PAGE
        else:
            status, content_type, body = 204, "text/plain", b""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def element_ref(snapshot: str, role: str, label: str) -> str:
    matches = re.findall(rf"- {role} {re.escape(json.dumps(label))}[^\n]*\[ref=([^\]]+)\]", snapshot)
    assert len(matches) == 1, (label, matches, snapshot)
    return matches[0]


@pytest.mark.skipif(not ENABLED, reason="Set ASTRA_PLAYWRIGHT_E2E=1 for real browser acceptance")
@pytest.mark.allow_application_environment
def test_product_debugging_across_real_sandbox_calls(tmp_path, monkeypatch):
    # Keep explicit execution policy (including Windows Job containment) while
    # isolating artifacts; this test never weakens the caller's process policy.
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "astra-home"))
    monkeypatch.setenv("AGENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.chdir(tmp_path)
    config = os.getenv("ASTRA_PLAYWRIGHT_TEST_CONFIG", "")
    if config:
        assert Path(config).is_absolute() and Path(config).is_file(), config
    offline = os.getenv("ASTRA_PLAYWRIGHT_E2E_OFFLINE", "1") != "0"
    session = "acceptance-" + uuid.uuid4().hex[:12]
    directory = tmp_path / "output" / "playwright" / session
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    transcript = []

    async def scenario():
        sandbox = LocalSandbox(timeout=45, workdir=str(tmp_path))

        async def command(*arguments, success=True):
            argv = [sys.executable, str(SCRIPT), "--workspace", str(tmp_path), "--session", session]
            if offline:
                argv.append("--offline")
            argv.extend(["--", *arguments])
            # Each invocation creates a fresh Python tool process. The browser
            # daemon must retain its tab/state after that process has exited.
            code = "import subprocess\nraise SystemExit(subprocess.run(" + repr(argv) + ", check=False).returncode)\n"
            result = await sandbox.execute_python(code)
            transcript.append({"command": list(arguments), **result})
            if success:
                assert result["exit_code"] == 0, result
            return result

        async def snapshot():
            return (await command("snapshot"))["output"]

        try:
            version = await command("--version")
            assert "0.1.24" in version["output"], version
            open_arguments = ["open", url]
            if config:
                open_arguments.append(f"--config={config}")
            await command(*open_arguments)
            before = await snapshot()
            assert "Product debug acceptance" in before, before
            await command("tracing-start")
            covered = element_ref(before, "button", "Covered action")
            blocked = await command("click", covered, success=False)
            assert blocked["exit_code"] != 0, blocked
            assert "intercepts pointer events" in blocked["output"] + blocked["error"], blocked
            current = await snapshot()
            assert "covered=0;pointerdown=0;click=0;saved=none" in current, current
            await command("click", element_ref(current, "button", "Pointer action"))
            current = await snapshot()
            assert "covered=0;pointerdown=1;click=1;saved=none" in current, current
            await command("fill", element_ref(current, "textbox", "Project name"), "Astra product smoke")
            current = await snapshot()
            await command("click", element_ref(current, "button", "Save draft"))
            current = await snapshot()
            assert "covered=0;pointerdown=1;click=1;saved=Astra product smoke" in current, current
            await command("click", element_ref(current, "button", "Load failed API"))
            console = (await command("console", "error"))["output"]
            assert "Fixture API failed: 503" in console, console
            requests = (await command("requests"))["output"]
            assert "/api/fail" in requests and "503" in requests, requests
            screenshot = (await command("screenshot"))["output"]
            paths = re.findall(r"\]\(([^\n)]+\.png)\)", screenshot)
            assert len(paths) == 1, screenshot
            image = (directory / paths[0]).resolve()
            assert image.is_relative_to(tmp_path) and image.is_file(), image
            registry = ToolRegistry()
            register_image_tools(registry, workdir=str(tmp_path))
            attached = await registry.execute("read_image", {"path": str(image), "detail": "high"})
            transcript.append({"read_image": attached})
            assert not attached.get("error"), attached
            payload = json.loads(attached["output"])
            assert payload["type"] == "image_attachment" and payload["image_paths"] == [str(image)], payload
            await command("tracing-stop")
            traces = list((directory / ".playwright-cli" / "traces").glob("*.trace"))
            network = list((directory / ".playwright-cli" / "traces").glob("*.network"))
            assert traces and all(path.stat().st_size for path in traces), traces
            assert network and all(path.stat().st_size for path in network), network
            responses = [json.loads(line).get("snapshot", {}) for path in network
                         for line in path.read_text(encoding="utf-8").splitlines() if line]
            assert any(item.get("request", {}).get("url") == url + "api/fail"
                       and item.get("response", {}).get("status") == 503 for item in responses), responses
        finally:
            try:
                await command("close", success=False)
            finally:
                await sandbox.close()

    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        (tmp_path / "acceptance-results.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
