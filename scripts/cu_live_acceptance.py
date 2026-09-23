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
import json
import secrets
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agent.runtime.computer_backend import ComputerSessionManager
from agent.runtime.macos_computer import HelperTransport, MacComputerBackend
from agent.runtime.tools.computer import register_computer_tools
from agent.runtime.tools.registry import ToolRegistry

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/cu-text-input.html"
TITLE = "Astra CU Text Fixture"
EDGE = "com.microsoft.edgemac"
OMNIBOX_LABELS = ("Address", "地址", "搜索")
ABC = "com.apple.keylayout.ABC"
PINYIN = "com.apple.inputmethod.SCIM.ITABC"
DIAGNOSTICS = Path("/tmp/astra-sipp-diagnostics.log")
DIAGNOSTIC_MARKERS = ("AX-TEXT-INEFFECTIVE", "TEXT-CONTINUITY-REJECT", "KEY-VALIDATE", "FOCUSED-ROOT",
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
            query = parse_qs(urlsplit(path).query)
            if query.get("step") == [step] and query.get("nonce") == [nonce]:
                return True
        return False


def nodes(tree):
    if isinstance(tree, dict):
        yield tree
        for child in tree.get("children", []):
            yield from nodes(child)


def find_ref(tree, role: str, *labels: str) -> str | None:
    for node in nodes(tree):
        text = " ".join(str(node.get(key) or "") for key in ("label", "title", "description"))
        if node.get("role") == role and node.get("element_ref") and any(label in text for label in labels):
            return node["element_ref"]
    return None


class Runner:
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
        outer = main.get("bounds") or {}
        for window in windows:
            inner = window.get("bounds") or {}
            if (window is not main and window.get("bindable") and not (window.get("title") or "")
                    and outer and inner and inner["x"] >= outer["x"] and inner["y"] >= outer["y"]
                    and inner["x"] + inner["width"] <= outer["x"] + outer["width"]
                    and inner["y"] + inner["height"] <= outer["y"] + outer["height"]):
                return app_ref, window["window_ref"]
        return None

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
            attempts.append({"code": result.get("code") or "", "dispatch": receipt.get("dispatch_state")})
            if not result.get("error") or receipt.get("dispatch_state") != "not_dispatched":
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
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not fixture.saw(step, nonce):
            await asyncio.sleep(0.25)
        return fixture.saw(step, nonce)

    async def omnibox(self, fixture: Fixture, nonce: str, source: str, label: str) -> dict:
        """Pass only when the local server receives this run's unique URL."""
        self.input_source(source)
        step = f"omnibox-{label}"
        url = f"127.0.0.1:{self.args.port}/text?step={step}&nonce={nonce}"
        typed = await self.type_into("AXTextField", OMNIBOX_LABELS, url)
        outcome = {"name": f"omnibox ({label})", "input_source": source, **typed,
                   "typed_through_popup": False, "navigated": False}
        if typed["unknown"]:
            return {**outcome, "passed": False}
        # Keyboard delivery opens suggestions over the page; read that window as its own target.
        popup = await self.suggestion_popup()
        if popup is not None:
            observation = await self.observe(*popup)
            rows = [node for node in nodes(observation.get("ax_tree"))
                    if nonce in " ".join(str(node.get(k) or "") for k in ("label", "title", "value", "description"))]
            outcome["typed_through_popup"] = bool(rows)
            pressable = next((node for node in rows if node.get("element_ref")
                              and "AXPress" in (node.get("actions") or [])), None)
            if pressable is not None:
                # AXPress needs no activation, so the popup stays exactly as observed.
                result, _ = await self.act(observation, [{"type": "click", "element_ref": pressable["element_ref"]}])
                outcome["press_code"] = result.get("code") or ""
                outcome["navigated"] = await self.navigated(fixture, step, nonce)
        if not outcome["navigated"]:
            # Without an open, pressable suggestion, read the omnibox itself and submit it.
            observation = await self.observe(*await self.fixture_window())
            field = next((node for node in nodes(observation.get("ax_tree")) if node.get("role") == "AXTextField"
                          and node.get("element_ref") and any(label in " ".join(
                              str(node.get(k) or "") for k in ("label", "title", "description")) for label in OMNIBOX_LABELS)),
                         None)
            if field is None:
                return {**outcome, "passed": False, "error": "omnibox is not in the fixture observation"}
            outcome["omnibox_has_url"] = nonce in str(field.get("value") or "")
            result, _ = await self.act(observation, [{"type": "keypress", "key": "return",
                                                      "element_ref": field["element_ref"]}])
            outcome["return_code"] = result.get("code") or ""
            outcome["navigated"] = await self.navigated(fixture, step, nonce)
        outcome["passed"] = outcome["navigated"]
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
                         ("omnibox", lambda: self.omnibox(fixture, nonce, ABC, "abc")),
                         ("omnibox-pinyin", lambda: self.omnibox(fixture, nonce, PINYIN, "pinyin"))]
            for key, scenario in scenarios:
                if self.args.only and key not in self.args.only:
                    continue
                try:
                    outcome = await scenario()
                except Exception as error:  # noqa: BLE001 - one broken scenario must not hide the others
                    outcome = {"name": key, "passed": False, "error": str(error)}
                self.report["scenarios"].append(outcome)
                print(("PASS " if outcome["passed"] else "FAIL ") + json.dumps(outcome, ensure_ascii=False), flush=True)
                if outcome.get("unknown"):
                    print("Stopping: an action outcome is uncertain and will not be replayed.", flush=True)
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
    parser.add_argument("--only", nargs="*", choices=["abc", "pinyin", "omnibox", "omnibox-pinyin"], default=None)
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
