"""One Astra-owned MLX model per user/model, with authenticated loopback IPC."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time

from ..instance_lock import InstanceAlreadyRunning, InstanceLock
from .embedding_runtime import MAX_BODY, PROTOCOL, identity, runtime_directory, state_path
from .embedding_runtime import idle_seconds as configured_idle_seconds


class Worker:
    def __init__(self, backend, *, idle_seconds: float = 90) -> None:
        self.backend = backend
        self.idle_seconds = idle_seconds
        self.token = secrets.token_hex(32)
        self.state = "loading"
        self.last_use = time.monotonic()
        self.stop = threading.Event()
        self.encoding = threading.Lock()

    def load(self) -> None:
        try:
            self.backend.load()
            self.backend.encode(["context index warm up"])
            self.state = "ready"
        except Exception:
            self.state = "failed"
        finally:
            # Idle time counts from the end of loading (a first load may download weights).
            self.last_use = time.monotonic()

    def idle(self) -> bool:
        return (self.state != "loading" and not self.encoding.locked()
                and time.monotonic() - self.last_use > self.idle_seconds)

    def health(self) -> dict:
        stats = getattr(self.backend, "memory_stats", lambda: {})() if self.state == "ready" else {}
        return {"state": self.state, "pid": os.getpid(), "memory": stats,
                "busy": self.encoding.locked()}

    def handler(self):
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args):
                pass  # Never log memory text, credentials, or caller paths.

            def reply(self, code: int, value: dict) -> None:
                raw = json.dumps({"protocol": PROTOCOL, "identity": identity(), **value}).encode()
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (OSError, ConnectionError):
                    pass

            def authorized(self) -> bool:
                if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + worker.token):
                    self.reply(403, {"state": "unauthorized"})
                    return False
                # Only an encode is use: clients check health every 30 s while they
                # run, which would otherwise keep the model loaded indefinitely.
                return True

            def do_GET(self) -> None:
                if self.authorized():
                    self.reply(200, worker.health()) if self.path == "/health" else self.reply(404, {"state": "absent"})

            def do_POST(self) -> None:
                if not self.authorized():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        raise ValueError
                    self.connection.settimeout(2)
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError
                except (ValueError, OSError):
                    self.reply(400, {"state": "invalid"})
                    return
                if self.path == "/stop":
                    # Shutdown and encode admission share one boundary: a
                    # maintenance stop never interrupts an admitted encoding.
                    if not worker.encoding.acquire(blocking=False):
                        self.reply(409, {"state": "busy"})
                        return
                    try:
                        worker.stop.set()
                        self.reply(200, {"state": "stopping"})
                    finally:
                        worker.encoding.release()
                    return
                if self.path != "/encode":
                    self.reply(404, {"state": "absent"})
                    return
                texts = payload.get("texts")
                if (not isinstance(texts, list) or not 1 <= len(texts) <= 16
                        or any(not isinstance(text, str) or len(text) > 16000 for text in texts)
                        or sum(len(text) for text in texts) > 32000):
                    self.reply(400, {"state": "invalid"})
                    return
                if worker.state != "ready" or not worker.encoding.acquire(blocking=False):
                    self.reply(503, {"state": "busy"})
                    return
                try:
                    if worker.stop.is_set():
                        self.reply(503, {"state": "stopping"})
                        return
                    vectors = worker.backend.encode(texts)
                    self.reply(200, {"vectors": vectors})
                except Exception:
                    self.reply(503, {"state": "unavailable"})
                finally:
                    worker.last_use = time.monotonic()
                    worker.encoding.release()

        return Handler


def serve(directory: Path, backend, *, idle_seconds: float | None = None) -> int:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_path(directory)
    owner = InstanceLock(path.with_suffix(".lock"))
    try:
        owner.acquire()
    except InstanceAlreadyRunning:
        return 0
    server = None
    try:
        worker = Worker(backend, idle_seconds=configured_idle_seconds() if idle_seconds is None else idle_seconds)
        server = ThreadingHTTPServer(("127.0.0.1", 0), worker.handler())
        server.daemon_threads = True
        server.timeout = 1
        metadata = {"protocol": PROTOCOL, "identity": identity(), "pid": os.getpid(),
                    "port": server.server_port, "token": worker.token}
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream)
        os.replace(temporary, path)
        threading.Thread(target=worker.load, name="astra-embedding-model", daemon=True).start()
        while not worker.stop.is_set():
            server.handle_request()
            if worker.idle():
                break
        return 0
    finally:
        if server is not None:
            server.server_close()
        path.unlink(missing_ok=True)
        owner.release()


def main(argv: list[str] | None = None) -> int:
    from .embedder import _MlxBackend
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=runtime_directory())
    args = parser.parse_args(argv)
    return serve(args.directory, _MlxBackend())


if __name__ == "__main__":
    raise SystemExit(main())
