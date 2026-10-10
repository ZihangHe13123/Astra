"""Browser fallback regression suite with real and deterministic local URLs.

Requires Chrome and network access. Run with:
    pytest tests/test_browser_regression.py -v

Tests the full fallback ladder (static → CDP → screenshot), plus multi-tab,
interaction, and connection stability. Critical assertions use data URLs so
an external demo site's availability cannot break the local quality gate.
"""

import asyncio
import os
import sys
from urllib.parse import quote

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.runtime.cdp_backend import CdpBrowserBackend, find_chrome

# Skip the whole browser launch suite unless this is native Windows Chrome.
chrome_path = find_chrome()
pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not chrome_path,
    reason="Requires native Windows Chrome",
)

# ---------------------------------------------------------------------------
# Shared event loop + backend for the whole module.
# asyncio.run() creates a new loop each call, but subprocess transports
# are bound to the loop that spawned them.  Using one loop avoids
# "Future attached to a different loop" errors.
# ---------------------------------------------------------------------------

_loop = asyncio.new_event_loop()
_backend: CdpBrowserBackend | None = None


def _get_backend() -> CdpBrowserBackend:
    global _backend
    if _backend is None:
        _backend = CdpBrowserBackend(chrome_path, timeout=25)
    return _backend


def run(coro):
    return _loop.run_until_complete(coro)


def teardown_module():
    global _backend
    if _backend is not None:
        try:
            _loop.run_until_complete(_backend.close_connection())
        except Exception:
            pass
        _backend = None
    _loop.close()


# ---------------------------------------------------------------------------
# 1. Static extraction regression
# ---------------------------------------------------------------------------

LOCAL_MELVILLE_URL = "data:text/html," + quote(
    "<html><body><h1>Herman Melville</h1><p>"
    + "Moby Dick regression fixture with deterministic local text. " * 8
    + "</p></body></html>"
)
LOCAL_OTHER_URL = "data:text/html," + quote(
    "<html><body><h1>Independent navigation target</h1></body></html>"
)
LOCAL_DYNAMIC_FORM_URL = "data:text/html," + quote(
    "<html><body><main id='app'>Loading</main><script>"
    "document.getElementById('app').textContent="
    "'Dynamic form content rendered by JavaScript with enough text for CDP extraction.';"
    "</script></body></html>"
)

# A live page's wording is not ours: the heading "Example Domain" left example.com's
# text, so these checks ask only for the word the page is about.
STATIC_URLS = [
    ("https://example.com", "example", 50),
    ("https://news.ycombinator.com", "Hacker News", 200),
    (LOCAL_MELVILLE_URL, "Herman Melville", 200),
]


class TestStaticExtraction:
    @pytest.mark.parametrize("url,expected_fragment,min_chars", STATIC_URLS)
    def test_static_extract(self, url, expected_fragment, min_chars):
        async def _test():
            text = await _get_backend().extract(url, max_length=12000)
            assert len(text.strip()) >= min_chars, (
                f"Static extract too short: {len(text.strip())} chars for {url}"
            )
            assert expected_fragment.lower() in text.lower(), (
                f"Expected '{expected_fragment}' in extract of {url}"
            )
        run(_test())


# ---------------------------------------------------------------------------
# 2. CDP interactive regression
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "win32", reason="CDP interactive tests require native Windows Chrome")
class TestCdpInteractive:
    def test_navigate_and_get_text(self):
        async def _test():
            b = _get_backend()
            await b.interactive_navigate("https://example.com", tab_id="reg1")
            text = await b.interactive_get_text(tab_id="reg1")
            assert "example" in text.lower()
            assert len(text.strip()) >= 50
        run(_test())

    def test_evaluate_js(self):
        async def _test():
            b = _get_backend()
            await b.interactive_navigate("https://example.com", tab_id="reg2")
            title = await b.interactive_evaluate("document.title", tab_id="reg2")
            assert title == "Example Domain"
        run(_test())

    def test_screenshot(self, tmp_path):
        async def _test():
            b = _get_backend()
            await b.interactive_navigate("https://example.com", tab_id="reg3")
            path = await b.interactive_screenshot(
                tab_id="reg3", output_path=str(tmp_path / "regression_ss.png")
            )
            assert os.path.exists(path), f"Screenshot not saved: {path}"
            size = os.path.getsize(path)
            assert size > 1000, f"Screenshot too small: {size} bytes"
        run(_test())

    def test_post_screenshot_stability(self):
        """Connection must survive a large screenshot response."""
        async def _test():
            b = _get_backend()
            await b.interactive_navigate("https://example.com", tab_id="reg4")
            await b.interactive_screenshot(tab_id="reg4")
            result = await b.interactive_evaluate("1+1", tab_id="reg4")
            assert str(result) == "2"
        run(_test())


# ---------------------------------------------------------------------------
# 3. Multi-tab regression
# ---------------------------------------------------------------------------

class TestMultiTab:
    def test_two_tabs_independent(self):
        async def _test():
            b = _get_backend()
            await b.interactive_navigate("https://example.com", tab_id="mt1")
            await b.interactive_navigate(LOCAL_MELVILLE_URL, tab_id="mt2")
            t1 = await b.interactive_get_text(tab_id="mt1")
            t2 = await b.interactive_get_text(tab_id="mt2")
            assert "example" in t1.lower()
            assert "Herman Melville" in t2 or "Moby" in t2
        run(_test())

    def test_tab_isolation_after_navigation(self):
        async def _test():
            b = _get_backend()
            await b.interactive_navigate("https://example.com", tab_id="iso1")
            await b.interactive_navigate(LOCAL_MELVILLE_URL, tab_id="iso2")
            await b.interactive_navigate(LOCAL_OTHER_URL, tab_id="iso1")
            t2 = await b.interactive_get_text(tab_id="iso2")
            assert "Herman Melville" in t2 or "Moby" in t2
        run(_test())


# ---------------------------------------------------------------------------
# 4. Interaction regression
# ---------------------------------------------------------------------------

class TestInteraction:
    def test_click(self):
        async def _test():
            b = _get_backend()
            html = 'data:text/html,<button id="b" onclick="document.title=\'clicked\'">Go</button>'
            await b.interactive_navigate(html, tab_id="click1")
            await b.interactive_evaluate(
                'document.querySelector("#b").click()', tab_id="click1"
            )
            await asyncio.sleep(0.3)
            title = await b.interactive_evaluate("document.title", tab_id="click1")
            assert title == "clicked"
        run(_test())

    def test_type(self):
        async def _test():
            b = _get_backend()
            html = (
                'data:text/html,<input id="inp" /><span id="out"></span>'
                '<script>document.getElementById("inp").addEventListener("input",'
                'e=>{document.getElementById("out").textContent=e.target.value})</script>'
            )
            await b.interactive_navigate(html, tab_id="type1")
            await b.interactive_type("#inp", "regression test", tab_id="type1")
            await asyncio.sleep(0.3)
            value = await b.interactive_evaluate(
                'document.getElementById("out").textContent', tab_id="type1"
            )
            assert value == "regression test"
        run(_test())

    def test_keyboard_event(self):
        async def _test():
            b = _get_backend()
            html = (
                'data:text/html,<input id="inp" />'
                '<script>document.getElementById("inp").addEventListener("keydown",'
                'e=>{if(e.key==="Enter")document.title="entered"})</script>'
            )
            await b.interactive_navigate(html, tab_id="key1")
            conn = b._connections.get("key1")
            assert conn is not None
            await conn.evaluate('document.getElementById("inp").focus()')
            await conn.send("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13,
            })
            await conn.send("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "Enter", "code": "Enter",
                "windowsVirtualKeyCode": 13,
            })
            await asyncio.sleep(0.3)
            title = await conn.evaluate("document.title")
            assert title == "entered"
        run(_test())


# ---------------------------------------------------------------------------
# 5. SPA / JS-heavy page fallback
# ---------------------------------------------------------------------------

class TestSpaFallback:
    def test_spa_get_text_via_cdp(self):
        async def _test():
            b = _get_backend()
            await b.interactive_navigate(
                LOCAL_DYNAMIC_FORM_URL, tab_id="spa1"
            )
            text = await b.interactive_get_text(tab_id="spa1")
            assert len(text.strip()) >= 50, f"SPA text too short: {len(text.strip())}"
        run(_test())

    def test_data_url_page(self):
        async def _test():
            b = _get_backend()
            html = "data:text/html,<h1>Regression</h1><p>This is a data URL page.</p>"
            await b.interactive_navigate(html, tab_id="data1")
            text = await b.interactive_get_text(tab_id="data1")
            assert "Regression" in text
        run(_test())


# ---------------------------------------------------------------------------
# 6. Connection stability
# ---------------------------------------------------------------------------

class TestConnectionStability:
    def test_ten_consecutive_operations(self):
        async def _test():
            b = _get_backend()
            await b.interactive_navigate("https://example.com", tab_id="stab1")
            for i in range(10):
                result = await b.interactive_evaluate(f"'op_{i}'", tab_id="stab1")
                assert result == f"op_{i}", f"Operation {i} failed: {result}"
        run(_test())

    def test_screenshot_then_operations(self, tmp_path):
        async def _test():
            b = _get_backend()
            await b.interactive_navigate("https://example.com", tab_id="stab2")
            for i in range(3):
                path = await b.interactive_screenshot(
                    tab_id="stab2", output_path=str(tmp_path / f"regression_ss_{i}.png")
                )
                assert os.path.exists(path)
            result = await b.interactive_evaluate("'alive'", tab_id="stab2")
            assert result == "alive"
        run(_test())
