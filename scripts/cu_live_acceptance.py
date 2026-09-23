"""Live CU acceptance in Edge against a local fixture, through Astra's real tool contract.

Covers text entry into a web field and typing a URL into the omnibox, each under the ABC
layout and under Pinyin; keyboard delivery into the omnibox opens its suggestion window. It
only acts on the fixture window it opened, never replays an uncertain action, and restores
the original input source.

Run from the project root, then leave the mouse and keyboard alone for about two minutes:

    .venv/bin/python -m scripts.cu_live_acceptance
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.util
import json
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
# Test this checkout's tool layer against the helper; an installed editable package may point at
# another checkout whose protocol does not match.
sys.path.insert(0, str(ROOT))

from agent.runtime.computer_backend import ComputerSessionManager  # noqa: E402
from agent.runtime.macos_computer import HelperTransport, MacComputerBackend  # noqa: E402
from agent.runtime.tools.computer import register_computer_tools  # noqa: E402
from agent.runtime.tools.registry import ToolRegistry  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/cu-text-input.html"
TITLE = "Astra CU Text Fixture"
EDGE = "com.microsoft.edgemac"
OMNIBOX_LABELS = ("Address", "地址", "搜索")
SCENARIOS = ("abc", "pinyin", "omnibox", "omnibox-pinyin", "omnibox-shortcut", "omnibox-hover", "omnibox-click")
HOVER_LABEL = "Fixture hover link"
ABC = "com.apple.keylayout.ABC"
PINYIN = "com.apple.inputmethod.SCIM.ITABC"
DIAGNOSTICS = Path("/tmp/astra-sipp-diagnostics.log")
DIAGNOSTIC_MARKERS = ("AX-TEXT-INEFFECTIVE", "AX-TEXT-UNAVAILABLE", "EXECUTE-TEXT-SAFETY",
                      "EXECUTE-OTHER", "EXECUTE-PERFORM", "TEXT-CONTINUITY-REJECT", "KEY-VALIDATE", "FOCUSED-ROOT",
                      "TYPE-ROUTE", "PLAN-RESULT", "RESP-ERR", "TAKEOVER-FAIL", "OVERLAY-DETAIL",
                      "observation_validation", "FOCUS-ACQUIRE")

# Selects an input source by ID (when given) and prints the current one. No text is typed.
TIS_SCRIPT = """
import Carbon
func current() -> String {
    guard let source = TISCopyCurrentKeyboardInputSource()?.takeRetainedValue(),
          let raw = TISGetInputSourceProperty(source, kTISPropertyInputSourceID) else { return "" }
    return Unmanaged<CFString>.fromOpaque(raw).takeUnretainedValue() as String
}
if CommandLine.arguments.count > 1 {
    let filter = [kTISPropertyInputSourceID as String: CommandLine.arguments[1]] as CFDictionary
    guard let list = TISCreateInputSourceList(filter, false)?.takeRetainedValue() as? [TISInputSource],
          let source = list.first, TISSelectInputSource(source) == noErr else { print("unavailable"); exit(2) }
}
print(current())
"""


# Pages that answer like a real site, not instantly: Edge keeps drawing the address field's
# suggestion list while such a page starts loading.
SLOW_STEPS = {"omnibox-click": 1.5}


class Fixture:
    """Serves the page, keeps its last report, and records every requested URL."""

    def __init__(self, port: int):
        self.state: dict = {}
        self.requests: list[str] = []
        self.lock = threading.Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with fixture.lock:
                    fixture.requests.append(self.path)
                path = urlsplit(self.path).path
                if path not in {"/text", "/state"}:
                    self.send_error(404)
                    return
                if path == "/text":
                    step = parse_qs(urlsplit(self.path).query).get("step", [""])[0]
                    time.sleep(SLOW_STEPS.get(step, 0))
                    data, kind = FIXTURE.read_bytes(), "text/html; charset=utf-8"
                else:
                    data, kind = json.dumps(fixture.snapshot()).encode(), "application/json"
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                size = int(self.headers.get("Content-Length", 0))
                if self.path != "/report" or not 0 < size < 8192:
                    self.send_error(400)
                    return
                value = json.loads(self.rfile.read(size))
                with fixture.lock:
                    fixture.state = value
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def saw(self, step: str, nonce: str) -> bool:
        with self.lock:
            paths = list(self.requests)
        for path in paths:
            parsed = urlsplit(path)
            query = parse_qs(parsed.query)
            if parsed.path == "/text" and query.get("step") == [step] and query.get("nonce") == [nonce]:
                return True
        return False


def nodes(tree):
    if isinstance(tree, dict):
        yield tree
        for child in tree.get("children", []):
            yield from nodes(child)


def hover_point(tree, window_bounds: dict) -> tuple[float, float] | None:
    """Screen center of the fixture link; snapshot bounds are window-local logical points."""
    for node in nodes(tree):
        text = " ".join(str(node.get(key) or "") for key in ("label", "title", "description"))
        bounds = node.get("bounds")
        if node.get("role") == "AXLink" and HOVER_LABEL in text and isinstance(bounds, dict):
            return (window_bounds["x"] + bounds["x"] + bounds["width"] / 2,
                    window_bounds["y"] + bounds["y"] + bounds["height"] / 2)
    return None


def pick_suggestion_window(main: dict, windows: list[dict]) -> dict | None:
    """The omnibox suggestion list: untitled, bindable, inside the fixture window, and not the thin
    bottom-edge strip Edge shows for a hovered link."""
    outer = main.get("bounds") or {}
    for window in windows:
        inner = window.get("bounds") or {}
        if (window is not main and window.get("bindable") and not (window.get("title") or "")
                and outer and inner and inner["x"] >= outer["x"] and inner["y"] >= outer["y"]
                and inner["x"] + inner["width"] <= outer["x"] + outer["width"]
                and inner["y"] + inner["height"] <= outer["y"] + outer["height"]
                and inner["height"] > 32 and inner["y"] < outer["y"] + outer["height"] / 2):
            return window
    return None


def status_strip_seen(lines) -> bool:
    """The helper waived a hovered link's bottom-edge status strip during this run."""
    return any("OVERLAY-PASSIVE status_strip" in line for line in lines)


def mouse_location() -> tuple[float, float] | None:
    """Where the pointer is now, so the hover scenario can put it back afterwards."""
    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    graphics = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
    foundation = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    graphics.CGEventCreate.restype = ctypes.c_void_p
    graphics.CGEventCreate.argtypes = [ctypes.c_void_p]
    graphics.CGEventGetLocation.restype = CGPoint
    graphics.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    foundation.CFRelease.argtypes = [ctypes.c_void_p]
    event = graphics.CGEventCreate(None)
    if not event:
        return None
    try:
        point = graphics.CGEventGetLocation(event)
        return point.x, point.y
    finally:
        foundation.CFRelease(event)


def post_mouse_move(x: float, y: float) -> None:
    """A real mouse-moved event: a warp alone does not make the browser show a hover strip."""
    class CGPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    graphics = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
    foundation = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    graphics.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    graphics.CGEventCreateMouseEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
    graphics.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    foundation.CFRelease.argtypes = [ctypes.c_void_p]
    event = graphics.CGEventCreateMouseEvent(None, 5, CGPoint(x, y), 0)  # kCGEventMouseMoved
    if not event:
        raise RuntimeError("could not create a mouse-moved event")
    try:
        graphics.CGEventPost(0, event)  # kCGHIDEventTap
    finally:
        foundation.CFRelease(event)


def find_ref(tree, role: str, *labels: str) -> str | None:
    for node in nodes(tree):
        text = " ".join(str(node.get(key) or "") for key in ("label", "title", "description"))
        if node.get("role") == role and node.get("element_ref") and any(label in text for label in labels):
            return node["element_ref"]
    return None


def omnibox_field(tree) -> dict | None:
    """Edge's address field in an observation of the page window."""
    return next((node for node in nodes(tree) if node.get("role") == "AXTextField" and node.get("element_ref")
                 and any(label in " ".join(str(node.get(key) or "") for key in ("label", "title", "description"))
                         for label in OMNIBOX_LABELS)), None)


def exact_fixture_address(value: object, url: str) -> bool:
    """Edge may show the entered HTTP address with or without its scheme."""
    return value in (url, f"http://{url}")


def observed_fixture_navigation(tree: object, step: str, nonce: str, port: int) -> bool:
    """Require this run's loaded page marker and address in the active Edge window."""
    path = f"/text?step={step}&nonce={nonce}"
    url = f"127.0.0.1:{port}{path}"
    has_page_marker = any(node.get("role") == "AXStaticText" and node.get("value") == path
                          for node in nodes(tree))
    has_address = any(node.get("role") == "AXTextField" and exact_fixture_address(node.get("value"), url)
                      and any(label in " ".join(str(node.get(k) or "") for k in
                          ("label", "title", "description")) for label in OMNIBOX_LABELS)
                      for node in nodes(tree))
    return has_page_marker and has_address


class Runner:
    # Real pointer control, replaceable in unit tests.
    locate_pointer = staticmethod(mouse_location)
    move_pointer = staticmethod(post_mouse_move)

    def __init__(self, args, work: Path):
        self.args = args
        self.report: dict = {"scenarios": [], "calls": [], "passed": False}
        self.transport = HelperTransport(args.helper) if args.helper else HelperTransport()
        self.manager = ComputerSessionManager(MacComputerBackend(self.transport), cache_root=work / "cache")
        self.registry = ToolRegistry()
        # Approval is pre-granted only because every act below targets the fixture window.
        self.registry.yolo = True
        register_computer_tools(self.registry, self.manager)
        self.tis = work / "tis.swift"
        self.tis.write_text(TIS_SCRIPT)

    def input_source(self, wanted: str | None = None) -> str:
        command = ["swift", str(self.tis)] + ([wanted] if wanted else [])
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        current = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        if wanted and current != wanted:
            raise RuntimeError(f"input source {wanted} could not be selected (now {current or 'unknown'})")
        return current

    async def call(self, name: str, parameters: dict) -> tuple[dict, dict | None]:
        started = time.monotonic()
        result = await self.registry.execute(name, parameters)
        entry = {"tool": name, "seconds": round(time.monotonic() - started, 3), "code": result.get("code") or ""}
        if "computer_receipt" in result:
            entry["receipt"] = result["computer_receipt"]
        self.report["calls"].append(entry)
        payload = None
        if not result.get("error"):
            try:
                payload = json.loads(result.get("fresh_output") or result.get("output") or "")
            except ValueError:
                payload = None
        return result, payload

    async def edge_windows(self) -> tuple[str, list[dict]]:
        # A fresh catalog every time: refs rotate whenever the tool layer refreshes it.
        _, catalog = await self.call("computer_apps", {})
        for app in (catalog or {}).get("apps", []):
            if app.get("bundle_id") == EDGE:
                return app["app_ref"], app.get("windows", [])
        return "", []

    async def fixture_window(self) -> tuple[str, str]:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            app_ref, windows = await self.edge_windows()
            for window in windows:
                if window.get("bindable") and TITLE in (window.get("title") or ""):
                    return app_ref, window["window_ref"]
            await asyncio.sleep(1)
        raise RuntimeError(f"No bindable Edge window titled '{TITLE}' appeared")

    async def suggestion_popup(self) -> tuple[str, str] | None:
        """The omnibox suggestion window: untitled, bindable and inside the fixture window."""
        app_ref, windows = await self.edge_windows()
        main = next((w for w in windows if TITLE in (w.get("title") or "")), None)
        if main is None:
            return None
        popup = pick_suggestion_window(main, windows)
        return (app_ref, popup["window_ref"]) if popup else None

    async def observe(self, app_ref: str, window_ref: str) -> dict:
        waited = 0.0
        while True:
            result, payload = await self.call("computer_get_app_state", {"app_ref": app_ref, "window_ref": window_ref})
            if payload is not None:
                if waited:
                    self.report.setdefault("overlay_waits", []).append(round(waited, 1))
                return payload
            if result.get("code") != "overlay_blocked" or waited >= 4:
                raise RuntimeError(f"fixture observation failed after {waited:.1f}s: "
                                   f"{result.get('code')}: {result.get('error')}")
            # Record how long an overlay lasts; OVERLAY-DETAIL in the diagnostics says what it was.
            # A refused observation leaves the catalog, and so these refs, unchanged.
            await asyncio.sleep(0.5)
            waited += 0.5

    async def act(self, observation: dict, actions: list[dict]) -> tuple[dict, dict | None]:
        return await self.call("computer_act", {"snapshot_id": observation["snapshot_id"], "actions": actions})

    async def type_into(self, role: str, labels: tuple[str, ...], text: str) -> dict:
        """Type once; retry once only when the helper proved that nothing was sent."""
        attempts = []
        for _ in range(2):
            observation = await self.observe(*await self.fixture_window())
            ref = find_ref(observation.get("ax_tree"), role, *labels)
            if ref is None:
                raise RuntimeError(f"{role} {labels} is not in the fixture observation")
            result, _ = await self.act(observation, [{"type": "type", "element_ref": ref, "text": text}])
            receipt = result.get("computer_receipt", {})
            code = result.get("code") or ""
            attempts.append({"code": code, "dispatch": receipt.get("dispatch_state")})
            # An AX write with unchanged readback can also return input_focus_required;
            # that does not prove the write had no delayed effect, so do not replay it.
            if (not result.get("error") or receipt.get("dispatch_state") != "not_dispatched"
                    or code not in {"background_action_unsupported", "stale_snapshot"}):
                break
        return {"attempts": attempts, "last_code": attempts[-1]["code"],
                "unknown": attempts[-1]["dispatch"] in {"unknown", "partial"}}

    async def web_field(self, fixture: Fixture, source: str, label: str) -> dict:
        self.input_source(source)
        observation = await self.observe(*await self.fixture_window())
        reset = find_ref(observation.get("ax_tree"), "AXButton", "Reset fixture")
        if reset is None:
            raise RuntimeError("Reset fixture button is not in the fixture observation")
        await self.act(observation, [{"type": "click", "element_ref": reset}])
        await asyncio.sleep(0.3)
        marker = f"Lyra {label} 验收 abc 123"
        typed = await self.type_into("AXTextField", ("Fixture line field",), marker)
        await asyncio.sleep(0.5)
        page = fixture.snapshot()
        return {"name": f"web field ({label})", "input_source": source, "expected": marker,
                "page_value": page.get("value"), "keydowns": page.get("keydowns"), "inputs": page.get("inputs"),
                **typed, "passed": page.get("value") == marker}

    async def navigated(self, fixture: Fixture, step: str, nonce: str) -> bool:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if fixture.saw(step, nonce):
                try:
                    observation = await self.observe(*await self.fixture_window())
                    if observed_fixture_navigation(observation.get("ax_tree"), step, nonce, self.args.port):
                        return True
                except RuntimeError:
                    pass  # Navigation may still be settling behind the suggestion window.
            await asyncio.sleep(0.25)
        return False

    async def open_fixture_tab(self, fixture: Fixture, nonce: str, label: str) -> str:
        """Give each omnibox case a fresh local page and a verifiable starting address."""
        step = f"setup-{label}"
        url = f"127.0.0.1:{self.args.port}/text?step={step}&nonce={nonce}"
        subprocess.run(["open", "-a", "Microsoft Edge", f"http://{url}"], check=True, timeout=30)
        if not await self.navigated(fixture, step, nonce):
            raise RuntimeError(f"Edge did not load the local setup page for {label}")
        return url

    async def type_omnibox(self, current_url: str, url: str, *, select: str = "bound") -> dict:
        """Select the bound address field, then type once into the same field."""
        observation = await self.observe(*await self.fixture_window())
        field = next((node for node in nodes(observation.get("ax_tree"))
                      if node.get("role") == "AXTextField" and node.get("element_ref")
                      and any(label in " ".join(str(node.get(k) or "") for k in
                          ("label", "title", "description")) for label in OMNIBOX_LABELS)), None)
        if field is None or not exact_fixture_address(field.get("value"), current_url):
            raise RuntimeError("Edge is not showing this case's fresh local fixture address")
        ref = field["element_ref"]
        # Both actions run in one batch: a separate Cmd+L opens an Edge suggestion
        # window and prevents a fresh observation of the field. The shortcut variant
        # carries no element_ref, so the bound select-all rule cannot route the typed
        # text; only the helper's application-level keyboard routing can.
        selection = ({"type": "keypress", "key": "l", "modifiers": ["command"]} if select == "shortcut"
                     else {"type": "keypress", "key": "a", "modifiers": ["command"], "element_ref": ref})
        result, _ = await self.act(observation, [
            selection,
            {"type": "type", "element_ref": ref, "text": url},
        ])
        receipt = result.get("computer_receipt") or {}
        code = result.get("code") or ""
        return {"attempts": [{"code": code, "dispatch": receipt.get("dispatch_state"),
                              "acknowledged": receipt.get("acknowledged")}],
                "last_code": code,
                "delivered": receipt.get("dispatch_state") == "acknowledged"
                and receipt.get("acknowledged") == 2
                and code in {"", "post_action_observation_pending"},
                "unknown": receipt.get("dispatch_state") in {"unknown", "partial"}}

    def diagnostics_offset(self) -> int:
        return DIAGNOSTICS.stat().st_size if DIAGNOSTICS.exists() else 0

    def diagnostics_since(self, offset: int) -> list[str]:
        if not DIAGNOSTICS.exists():
            return []
        with DIAGNOSTICS.open("rb") as log:
            log.seek(offset)
            return log.read().decode("utf-8", "replace").splitlines()

    async def hover_fixture_link(self) -> tuple[float, float]:
        """Rest the real pointer on the fixture link so Edge shows its bottom-edge status strip."""
        app_ref, windows = await self.edge_windows()
        window = next((w for w in windows if w.get("bindable") and TITLE in (w.get("title") or "")), None)
        if window is None:
            raise RuntimeError("the fixture window is not in the catalog")
        observation = await self.observe(app_ref, window["window_ref"])
        point = hover_point(observation.get("ax_tree"), window.get("bounds") or {})
        if point is None:
            raise RuntimeError("the fixture hover link is not in the observation")
        self.move_pointer(*point)
        await asyncio.sleep(1.0)
        return point

    async def omnibox(self, fixture: Fixture, nonce: str, source: str, label: str, *,
                      select: str = "bound") -> dict:
        """Pass only when the local server receives this run's unique URL."""
        current_url = await self.open_fixture_tab(fixture, nonce, label)
        self.input_source(source)
        step = f"omnibox-{label}"
        url = f"127.0.0.1:{self.args.port}/text?step={step}&nonce={nonce}"
        hover = select == "hover"
        strip_offset = self.diagnostics_offset() if hover else 0
        resting = self.locate_pointer() if hover else None
        try:
            return await self.omnibox_flow(fixture, nonce, source, label, select, current_url, step, url,
                                           hover, strip_offset)
        finally:
            if resting is not None:
                # Later scenarios and the user's desktop must not inherit the hover strip.
                self.move_pointer(*resting)

    async def omnibox_flow(self, fixture: Fixture, nonce: str, source: str, label: str, select: str,
                           current_url: str, step: str, url: str, hover: bool, strip_offset: int) -> dict:
        hovered = await self.hover_fixture_link() if hover else None
        # While hovering, type by the unbound Cmd+L route: the strip must not block observation,
        # the pre-action check, or the typing itself.
        typed = await self.type_omnibox(current_url, url, select="shortcut" if hover else select)
        outcome = {"name": f"omnibox ({label})", "input_source": source, **typed,
                   "typed_through_popup": False, "navigated": False}
        if not typed["delivered"]:
            outcome["stop"] = typed["unknown"] or typed["attempts"][-1]["dispatch"] == "acknowledged"
            return {**outcome, "passed": False}
        try:
            # Keyboard delivery opens suggestions over the page; read that window as its own target.
            popup = await self.suggestion_popup()
            if popup is not None:
                observation = await self.observe(*popup)
                pressable = [node for node in nodes(observation.get("ax_tree"))
                             if node.get("role") == "AXStaticText" and node.get("element_ref")
                             and exact_fixture_address(node.get("value"), url)
                             and "AXPress" in (node.get("actions") or [])]
                outcome["typed_through_popup"] = len(pressable) == 1
                if len(pressable) == 1:
                    # AXPress needs no activation, so the popup stays exactly as observed.
                    result, _ = await self.act(observation, [{"type": "click", "element_ref": pressable[0]["element_ref"]}])
                    outcome["press_code"] = result.get("code") or ""
                    outcome["navigated"] = await self.navigated(fixture, step, nonce)
                    if not outcome["navigated"]:
                        receipt = result.get("computer_receipt", {})
                        outcome["unknown"] = receipt.get("dispatch_state") in {"unknown", "partial"}
                        return {**outcome, "passed": False, "stop": True}
                else:
                    return {**outcome, "passed": False, "stop": True,
                            "error": "no unique exact local URL in the suggestion window"}
            if not outcome["navigated"]:
                # With no popup, submit only a field whose whole value is this run's URL.
                observation = await self.observe(*await self.fixture_window())
                field = next((node for node in nodes(observation.get("ax_tree")) if node.get("role") == "AXTextField"
                              and node.get("element_ref") and any(label in " ".join(
                                  str(node.get(k) or "") for k in ("label", "title", "description"))
                                  for label in OMNIBOX_LABELS)), None)
                if field is None:
                    return {**outcome, "passed": False, "stop": True,
                            "error": "omnibox is not in the fixture observation"}
                outcome["omnibox_has_url"] = exact_fixture_address(field.get("value"), url)
                if not outcome["omnibox_has_url"]:
                    return {**outcome, "passed": False, "stop": True}
                result, _ = await self.act(observation, [{"type": "keypress", "key": "return",
                                                          "element_ref": field["element_ref"]}])
                outcome["return_code"] = result.get("code") or ""
                outcome["navigated"] = await self.navigated(fixture, step, nonce)
        except Exception as error:  # noqa: BLE001 - sent text must not be retried after observation fails
            return {**outcome, "passed": False, "stop": True, "error": str(error)}
        outcome["passed"] = outcome["navigated"]
        if hover:
            outcome["hover_point"] = hovered
            outcome["status_strip_seen"] = status_strip_seen(self.diagnostics_since(strip_offset))
            # Without the strip this run proves nothing about the waiver.
            outcome["passed"] = outcome["navigated"] and outcome["status_strip_seen"]
        outcome["stop"] = not outcome["passed"]
        return outcome

    async def omnibox_click(self, fixture: Fixture, nonce: str, source: str, label: str) -> dict:
        """Click the address field, then type and submit through the page window while the field's
        suggestion list is open: a model's route, with no targeting of the suggestion window."""
        current_url = await self.open_fixture_tab(fixture, nonce, label)
        self.input_source(source)
        step = f"omnibox-{label}"
        url = f"127.0.0.1:{self.args.port}/text?step={step}&nonce={nonce}"
        outcome = {"name": f"omnibox ({label})", "input_source": source, "navigated": False}
        observation = await self.observe(*await self.fixture_window())
        field = omnibox_field(observation.get("ax_tree"))
        if field is None or not exact_fixture_address(field.get("value"), current_url):
            raise RuntimeError("Edge is not showing this case's fresh local fixture address")
        clicked, _ = await self.act(observation, [{"type": "click", "element_ref": field["element_ref"]}])
        outcome["click_code"] = clicked.get("code") or ""
        waits = len(self.report.setdefault("overlay_waits", []))
        try:
            observation = await self.observe(*await self.fixture_window())
        except RuntimeError as error:
            return {**outcome, "passed": False, "stop": True, "error": str(error)}
        # Observed at once, not after the suggestion list happened to close.
        outcome["page_observable"] = len(self.report["overlay_waits"]) == waits
        outcome["popup_reported"] = bool(observation.get("suggestion_popups"))
        field = omnibox_field(observation.get("ax_tree"))
        if field is None:
            return {**outcome, "passed": False, "stop": True, "error": "omnibox is not in the page observation"}
        ref = field["element_ref"]
        typed, _ = await self.act(observation, [
            {"type": "keypress", "key": "a", "modifiers": ["command"], "element_ref": ref},
            {"type": "type", "element_ref": ref, "text": url},
        ])
        receipt = typed.get("computer_receipt") or {}
        outcome["type_code"] = typed.get("code") or ""
        outcome["unknown"] = receipt.get("dispatch_state") in {"unknown", "partial"}
        # The helper must observe the page after typing, with the suggestion list still open.
        if receipt.get("dispatch_state") != "acknowledged" or outcome["type_code"]:
            return {**outcome, "passed": False, "stop": True}
        observation = await self.observe(*await self.fixture_window())
        field = omnibox_field(observation.get("ax_tree"))
        outcome["omnibox_has_url"] = field is not None and exact_fixture_address(field.get("value"), url)
        if not outcome["omnibox_has_url"]:
            return {**outcome, "passed": False, "stop": True}
        submitted, _ = await self.act(observation, [{"type": "keypress", "key": "return",
                                                     "element_ref": field["element_ref"]}])
        outcome["return_code"] = submitted.get("code") or ""
        outcome["navigated"] = await self.navigated(fixture, step, nonce)
        # The list still drawn while the page loads must not block the observation after Return.
        outcome["passed"] = outcome["navigated"] and outcome["page_observable"] and not outcome["return_code"]
        outcome["stop"] = not outcome["passed"]
        return outcome

    async def run(self) -> bool:
        fixture = Fixture(self.args.port)
        nonce = secrets.token_hex(4)
        log_offset = DIAGNOSTICS.stat().st_size if DIAGNOSTICS.exists() else 0
        original = self.input_source()
        self.report["original_input_source"] = original
        try:
            # The helper inherits TCC permission from the launching terminal, not its own bundle.
            _, status = await self.call("computer_status", {})
            permissions = (status or {}).get("permissions") or {}
            self.report["helper"] = (status or {}).get("build_identity")
            if not (permissions.get("accessibility") and permissions.get("screen_recording")):
                raise RuntimeError("This terminal lacks Accessibility or Screen Recording permission. "
                                   "Run the script in the terminal where Astra runs.")
            subprocess.run(["open", "-a", "Microsoft Edge", f"http://127.0.0.1:{self.args.port}/text?nonce={nonce}"],
                           check=True, timeout=30)
            await self.fixture_window()
            scenarios = [("abc", lambda: self.web_field(fixture, ABC, "ABC")),
                         ("pinyin", lambda: self.web_field(fixture, PINYIN, "拼音")),
                         ("omnibox", lambda: self.omnibox(fixture, secrets.token_hex(4), ABC, "abc")),
                         ("omnibox-pinyin", lambda: self.omnibox(fixture, secrets.token_hex(4), PINYIN, "pinyin")),
                         ("omnibox-shortcut", lambda: self.omnibox(fixture, secrets.token_hex(4), ABC, "shortcut",
                                                                   select="shortcut")),
                         ("omnibox-hover", lambda: self.omnibox(fixture, secrets.token_hex(4), ABC, "hover",
                                                                select="hover")),
                         ("omnibox-click", lambda: self.omnibox_click(fixture, secrets.token_hex(4), ABC, "click"))]
            for key, scenario in scenarios:
                if self.args.only and key not in self.args.only:
                    continue
                try:
                    outcome = await scenario()
                except Exception as error:  # noqa: BLE001 - one broken scenario must not hide the others
                    outcome = {"name": key, "passed": False, "error": str(error)}
                self.report["scenarios"].append(outcome)
                print(("PASS " if outcome["passed"] else "FAIL ") + json.dumps(outcome, ensure_ascii=False), flush=True)
                if outcome.get("unknown") or outcome.get("stop"):
                    print("Stopping: the current omnibox action needs inspection and will not be replayed.", flush=True)
                    break
            self.report["passed"] = bool(self.report["scenarios"]) and all(
                s["passed"] for s in self.report["scenarios"])
        except Exception as error:  # noqa: BLE001 - always restore state and write the report
            self.report["error"] = str(error)
            print(f"ERROR {error}", flush=True)
        finally:
            try:
                self.report["restored_input_source"] = self.input_source(original) if original else ""
            except Exception as error:  # noqa: BLE001
                self.report["restore_error"] = str(error)
            await self.manager.close()
            fixture.server.shutdown()
            if DIAGNOSTICS.exists():
                with DIAGNOSTICS.open("rb") as log:
                    log.seek(log_offset)
                    lines = log.read().decode("utf-8", errors="replace").splitlines()
                self.report["diagnostics"] = [line for line in lines if any(m in line for m in DIAGNOSTIC_MARKERS)][-120:]
        return self.report["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8771)
    parser.add_argument("--helper", type=Path, default=None, help="helper executable; default is the installed one")
    parser.add_argument("--only", nargs="*", choices=list(SCENARIOS), default=None)
    parser.add_argument("--output", type=Path,
                        default=ROOT / ".scratch" / time.strftime("cu-live-acceptance-%Y%m%d-%H%M%S.json"))
    args = parser.parse_args()
    print("Live CU acceptance: Edge will open a local fixture tab. Do not touch the mouse or keyboard.", flush=True)
    for remaining in (3, 2, 1):
        print(f"  starting in {remaining}…", flush=True)
        time.sleep(1)
    with tempfile.TemporaryDirectory(prefix="astra-cu-live-") as temporary:
        runner = Runner(args, Path(temporary))
        passed = asyncio.run(runner.run())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(runner.report, ensure_ascii=False, indent=2) + "\n")
    print(("ALL PASSED" if passed else "NOT ALL PASSED") + f" — report: {args.output}", flush=True)
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
