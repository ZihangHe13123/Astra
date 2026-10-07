"""Speech through the standard endpoint: order, interruption, failure, and the local server's lifetime."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from agent.cli.provider_connections import save_connection
from agent.cli.voice_preferences import VoicePreferences, execute_voice_command
from agent.speech.provider import SpeechError
from agent.speech.service import SpeechService
from agent.speech.settings import SpeechSettings

ROOT = Path(__file__).resolve().parents[1]
# The fake endpoint answers with the text itself as UTF-16, so the bytes that
# reach the player read back as exactly what was spoken, in order.
SERVER = textwrap.dedent('''
    import json, sys, threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Speech(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.requests.append((self.path, self.headers.get("Authorization", ""), body))
            if self.server.status != 200:
                self.send_response(self.server.status); self.send_header("Content-Length", "6"); self.end_headers()
                self.wfile.write(b"broken"); return
            gate = self.server.gates.get(body["input"])
            if gate: gate.wait(10)
            audio = body["input"].encode("utf-16-le")
            self.send_response(200); self.send_header("Content-Type", "audio/pcm")
            self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
            for start in range(0, len(audio), 3):  # odd-sized pieces: samples must be re-aligned
                piece = audio[start:start + 3]
                self.wfile.write(f"{len(piece):x}\\r\\n".encode() + piece + b"\\r\\n"); self.wfile.flush()
            self.wfile.write(b"0\\r\\n\\r\\n")

    def serve(port=0):
        server = ThreadingHTTPServer(("127.0.0.1", port), Speech)
        server.requests, server.gates, server.status = [], {}, 200
        return server

    if __name__ == "__main__":
        serve(int(sys.argv[1])).serve_forever()
''')
_namespace: dict = {}
exec(SERVER, _namespace)
serve_speech = _namespace["serve"]


@pytest.fixture
def endpoint():
    server = serve_speech()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    yield server
    server.shutdown()
    server.server_close()


class Player:
    def __init__(self) -> None:
        self.audio, self.units, self.stops, self.closed = bytearray(), 0, 0, False

    def lead(self, seconds: float) -> None:
        assert seconds >= 0

    def write(self, pcm: bytes) -> None:
        assert len(pcm) % 2 == 0, "audio must arrive in whole 16-bit samples"
        self.audio += pcm

    def unit_done(self) -> None:
        self.units += 1

    def stop(self) -> None:
        self.stops += 1

    async def drain(self) -> None: ...

    def close(self) -> None:
        self.closed = True

    @property
    def heard(self) -> str:
        return self.audio.decode("utf-16-le")


class Store:
    def __init__(self, tmp_path: Path, **voice) -> None:
        self.voice, self.tmp_path = voice, tmp_path

    def load(self) -> SpeechSettings:
        return SpeechSettings.parse(self.voice)

    def endpoint(self, settings: SpeechSettings) -> tuple[str, str]:
        return settings.base_url, "voice-test-key"

    def runtime_dir(self) -> Path:
        return self.tmp_path / "voice"


def make(tmp_path: Path, **voice):
    players: list[Player] = []
    statuses: list[dict] = []
    service = SpeechService(Store(tmp_path, **voice), statuses.append,
                            open_player=lambda rate, lead: players.append(Player()) or players[-1])
    return service, players, statuses


async def settled(service: SpeechService) -> None:
    deadline = time.monotonic() + 10
    while service.speaking:
        assert time.monotonic() < deadline, "speech did not finish"
        await asyncio.sleep(0.01)


def test_reply_is_spoken_in_order_with_the_selected_voice(tmp_path, endpoint):
    async def scenario():
        service, players, statuses = make(
            tmp_path, enabled=True, base_url=endpoint.url, model="speech-model", selected="warm",
            request={"stream": True, "streaming_interval": 1.0}, first_request={"streaming_interval": 0.3},
            voices={"cool": {"voice": "c"}, "warm": {"voice": "w", "request": {"ref_text": "参考"}}})
        first = threading.Event()
        endpoint.gates["你回来啦，"] = first
        service.begin_turn()
        for piece in ("你回来啦，今天的问题我已经", "看过一遍了。\n```sh\nrm -rf x\n```\n我们一步一步来，好吗？"):
            service.feed(piece)
        service.finish()
        while not endpoint.requests:
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        in_flight = len(endpoint.requests)
        first.set()
        await settled(service)
        await service.close()
        return players, statuses, in_flight

    players, statuses, in_flight = asyncio.run(scenario())
    assert in_flight == 1, "one unit is synthesized at a time, so a local engine is never split between two"
    units = ["你回来啦，", "今天的问题我已经看过一遍了。", "我们一步一步来，好吗？"]
    assert [body["input"] for _, _, body in endpoint.requests] == units
    assert players[0].heard == "".join(units) and players[0].units == 3 and players[0].closed
    path, authorization, body = endpoint.requests[0]
    assert (path, authorization) == ("/v1/audio/speech", "Bearer voice-test-key")
    assert body == {"input": units[0], "response_format": "pcm", "stream": True, "streaming_interval": 0.3,
                    "model": "speech-model", "voice": "w", "ref_text": "参考"}
    assert [later["streaming_interval"] for _, _, later in endpoint.requests[1:]] == [1.0, 1.0]
    assert [status["state"] for status in statuses] == ["speaking", "idle"]
    assert statuses[0]["voice"] == "warm" and statuses[0]["voices"] == ["cool", "warm"]


def test_stopping_drops_everything_not_yet_heard(tmp_path, endpoint):
    async def scenario():
        service, players, _ = make(tmp_path, enabled=True, base_url=endpoint.url)
        held = threading.Event()
        endpoint.gates["第一句还没出声。"] = held
        service.begin_turn()
        service.feed("第一句还没出声。第二句也一样。")
        service.finish()
        while not endpoint.requests:
            await asyncio.sleep(0.01)
        service.begin_turn()  # the user sent something new
        held.set()
        service.feed("这是新的回复。")
        service.finish()
        await settled(service)
        await service.close()
        return players

    players = asyncio.run(scenario())
    assert [player.heard for player in players] == ["这是新的回复。"]


def test_voice_that_is_off_or_not_set_up_contacts_nothing(tmp_path, endpoint):
    async def scenario(**voice):
        service, players, _ = make(tmp_path, **voice)
        service.begin_turn()
        service.feed("这句话不会被读出来。")
        service.finish()
        await asyncio.sleep(0.05)
        status = service.status()
        await service.close()
        return players, status

    players, status = asyncio.run(scenario(enabled=False, base_url=endpoint.url))
    assert (players, endpoint.requests, status["state"]) == ([], [], "off")
    players, status = asyncio.run(scenario(enabled=True))
    assert (players, endpoint.requests) == ([], []) and "not set up" in status["error"]


def test_endpoint_failure_is_reported_and_the_next_reply_tries_again(tmp_path, endpoint):
    async def scenario():
        service, players, statuses = make(tmp_path, enabled=True, base_url=endpoint.url)
        endpoint.status = 503
        service.begin_turn()
        service.feed("第一次会失败。")
        service.finish()
        await settled(service)
        failed = service.status()
        endpoint.status = 200
        service.begin_turn()
        service.feed("第二次正常。")
        service.finish()
        await settled(service)
        await service.close()
        return players, failed, service.status(), statuses

    players, failed, recovered, statuses = asyncio.run(scenario())
    assert "503" in failed["error"] and failed["state"] == "idle"
    assert recovered["error"] == "" and players[-1].heard == "第二次正常。"
    assert any("503" in status["error"] for status in statuses)


def test_long_reply_is_read_up_to_the_limit(tmp_path, endpoint):
    async def scenario():
        service, players, _ = make(tmp_path, enabled=True, base_url=endpoint.url, max_chars=20)
        service.begin_turn()
        service.feed("第一句有十个字左右吧。第二句也有十个字左右。第三句不应该被读出来。第四句也不应该。")
        service.finish()
        await settled(service)
        await service.close()
        return players

    assert asyncio.run(scenario())[0].heard == "第一句有十个字左右吧。第二句也有十个字左右。"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _until(condition, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def test_local_server_starts_on_demand_and_stops_with_the_session(tmp_path):
    script, port = tmp_path / "speech_server.py", _free_port()
    script.write_text(SERVER, encoding="utf-8")

    async def scenario():
        service, players, statuses = make(
            tmp_path, enabled=True, base_url=f"http://127.0.0.1:{port}/v1",
            server={"command": [sys.executable, str(script), str(port)], "startup_seconds": 20})
        service.begin_turn()
        service.feed("本地服务按需启动。")
        service.finish()
        await settled(service)
        running = _listening(port)
        await service.close()
        return players, statuses, running

    assert not _listening(port)
    players, statuses, running = asyncio.run(scenario())
    assert running and players[0].heard == "本地服务按需启动。"
    assert [status["state"] for status in statuses] == ["starting", "speaking", "idle"]
    assert _until(lambda: not _listening(port)), "the server outlived the session that started it"
    assert (tmp_path / "voice" / "server.heartbeat").exists()


def test_switching_voice_off_stops_the_local_server_at_once(tmp_path, monkeypatch):
    script, port, settings = tmp_path / "speech_server.py", _free_port(), tmp_path / "settings.json"
    script.write_text(SERVER, encoding="utf-8")
    settings.write_text(json.dumps({"voice": {
        "enabled": True, "base_url": f"http://127.0.0.1:{port}/v1",
        "server": {"command": [sys.executable, str(script), str(port)], "startup_seconds": 20, "idle_seconds": 3600}}}),
        encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "home"))

    async def scenario():
        store = VoicePreferences()
        service = SpeechService(store, lambda status: None, open_player=lambda rate, lead: Player())
        service.begin_turn()
        service.feed("先说一句。")
        service.finish()
        await settled(service)
        running = _listening(port)
        message, error = execute_voice_command(service, store, ["off"])
        stopped = await asyncio.to_thread(_until, lambda: not _listening(port))
        await service.close()
        return running, message, error, stopped

    running, message, error, stopped = asyncio.run(scenario())
    assert running and (message.startswith("Voice is off"), error) == (True, "")
    assert stopped, "a server that nobody will use kept its memory"


def _launch(tmp_path: Path, port: int, idle: float) -> subprocess.Popen:
    script, heartbeat = tmp_path / "speech_server.py", tmp_path / "heartbeat"
    script.write_text(SERVER, encoding="utf-8")
    heartbeat.touch()
    process = subprocess.Popen(
        [sys.executable, "-m", "agent.speech.launcher", "--heartbeat", str(heartbeat), "--idle", str(idle), "--",
         sys.executable, str(script), str(port)], stdin=subprocess.PIPE, cwd=ROOT)
    assert _until(lambda: _listening(port)), "the server never started"
    return process


def test_launcher_stops_the_server_when_its_owner_goes(tmp_path):
    port = _free_port()
    process = _launch(tmp_path, port, idle=600)
    assert process.stdin is not None
    process.stdin.close()  # what an exiting or killed session leaves behind
    assert process.wait(timeout=10) == 0 and not _listening(port)


def test_launcher_stops_an_unused_server_and_spares_a_used_one(tmp_path):
    port = _free_port()
    process = _launch(tmp_path, port, idle=1.5)
    try:
        for _ in range(6):  # another session keeps speaking
            (tmp_path / "heartbeat").touch()
            time.sleep(0.5)
        assert process.poll() is None and _listening(port)
        assert process.wait(timeout=10) == 0 and not _listening(port)
    finally:
        if process.poll() is None:
            process.kill()


def test_voice_command_persists_the_switch_and_the_selected_voice(tmp_path, monkeypatch, endpoint):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_model": "kept", "voice": {
        "base_url": endpoint.url, "voices": {"温暖搭档": {}, "深夜温柔": {}, "Cool": {}}}}), encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))

    async def scenario():
        store = VoicePreferences()
        statuses: list[dict] = []
        service = SpeechService(store, statuses.append, open_player=lambda rate, lead: Player())
        results = [execute_voice_command(service, store, args) for args in (
            [], ["on"], ["use", "深夜"], ["use", "nobody"], ["list"], ["use", "cool"], ["off"], ["louder"])]
        await service.close()
        return results

    status, on, use, unknown, listed, prefix, off, usage = asyncio.run(scenario())
    assert status == ("Voice is off. /voice on reads replies aloud.", "")
    assert on == ("Voice is on · 温暖搭档.", "")
    assert use == ("Voice set to 深夜温柔.", "")
    assert unknown == ("", "Unknown voice: nobody. Voices: 温暖搭档, 深夜温柔, Cool")
    assert listed == ("Voices: 温暖搭档, 深夜温柔 (selected), Cool", "")
    assert prefix == ("Voice set to Cool.", "")
    assert off[0].startswith("Voice is off") and usage[1].startswith("Usage: /voice")
    saved = json.loads(settings.read_text(encoding="utf-8"))
    assert saved["selected_model"] == "kept"
    assert saved["voice"] == {"base_url": endpoint.url, "voices": {"温暖搭档": {}, "深夜温柔": {}, "Cool": {}},
                              "enabled": False, "selected": "Cool"}


def test_endpoint_and_key_come_from_the_voice_settings_or_a_saved_connection(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("VOICE_TEST_KEY", "from-environment")
    save_connection("speechhost", {"label": "Speech host", "base_url": "https://speech.example/v1",
                                   "api_key": "from-connection", "api_key_env": "", "route_id": "custom"})
    store = VoicePreferences()
    own = SpeechSettings.parse({"base_url": "http://127.0.0.1:9/v1/", "api_key_env": "VOICE_TEST_KEY"})
    assert store.endpoint(own) == ("http://127.0.0.1:9/v1", "from-environment")
    assert store.endpoint(SpeechSettings.parse({"connection": "speechhost"})) == (
        "https://speech.example/v1", "from-connection")
    with pytest.raises(SpeechError, match="no saved connection"):
        store.endpoint(SpeechSettings.parse({"connection": "missing"}))
    assert os.environ["VOICE_TEST_KEY"] == "from-environment"
