"""Astra-owned loopback transport: all local MLX clients share one model worker.

Only load/maintenance may start a worker. encode never starts a process or loads
a model. The endpoint is authenticated by a private, per-user rendezvous file.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

PROTOCOL = 1
MAX_BODY = 128 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
DEFAULT_IDLE_SECONDS = 900


def idle_seconds() -> float:
    """How long the model stays loaded without an encode request (ASTRA_EMBEDDING_IDLE_SECONDS)."""
    try:
        return max(60.0, float(os.getenv("ASTRA_EMBEDDING_IDLE_SECONDS", DEFAULT_IDLE_SECONDS)))
    except ValueError:
        return float(DEFAULT_IDLE_SECONDS)


def identity() -> str:
    from .embedder import MODEL_ID
    return "mlx:" + MODEL_ID


def runtime_directory() -> Path:
    configured = os.getenv("ASTRA_EMBEDDING_RUNTIME_DIR", "").strip()
    directory = Path(configured).expanduser() if configured else Path.home() / ".astra" / "embedding-runtime"
    return directory.resolve()


def state_path(directory: Path) -> Path:
    profile = hashlib.sha256(f"{PROTOCOL}:{identity()}".encode()).hexdigest()[:20]
    return directory / f"{profile}.json"


def read_state(directory: Path) -> dict:
    path = state_path(directory)
    if path.is_symlink():
        raise ValueError("Unsafe embedding rendezvous file")
    info = path.stat()
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError("Embedding rendezvous file must be private")
    if info.st_size > 4096:
        raise ValueError("Invalid embedding rendezvous file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("protocol") != PROTOCOL or value.get("identity") != identity()
            or not isinstance(value.get("port"), int) or not 0 < value["port"] < 65536
            or not isinstance(value.get("pid"), int) or value["pid"] <= 0
            or not isinstance(value.get("token"), str) or len(value["token"]) != 64):
        raise ValueError("Invalid embedding rendezvous metadata")
    return value


def request(state: dict, path: str, payload: dict | None = None, *, timeout: float = 2.0) -> dict:
    from .embedder import EmbeddingUnavailable
    connection = http.client.HTTPConnection("127.0.0.1", state["port"], timeout=timeout)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    if body is not None and len(body) > MAX_BODY:
        raise ValueError("Embedding request too large")
    try:
        connection.request("POST" if payload is not None else "GET", path, body=body,
                           headers={"Authorization": "Bearer " + state["token"], "Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE + 1)
        if response.status != 200 or len(raw) > MAX_RESPONSE:
            raise EmbeddingUnavailable("Embedding worker unavailable")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("protocol") != PROTOCOL or value.get("identity") != identity():
            raise ValueError("Embedding worker identity mismatch")
        return value
    except (OSError, http.client.HTTPException) as exc:
        raise EmbeddingUnavailable("Embedding worker unavailable") from exc
    finally:
        connection.close()


def ensure_runtime(directory: Path) -> dict:
    """Called off the reply path; kernel ownership prevents duplicate models."""
    try:
        current = read_state(directory)
        request(current, "/health", timeout=0.3)
        return current
    except (OSError, ValueError, RuntimeError, http.client.HTTPException):
        pass
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    process = subprocess.Popen(
        [sys.executable, "-m", "agent.runtime.context_index.embedding_worker", "--directory", str(directory)],
        cwd=Path(__file__).resolve().parents[3],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
    )
    # Reap the short losing process too when simultaneous clients race startup.
    threading.Thread(target=process.wait, daemon=True, name="embedding-worker-reaper").start()
    until = time.monotonic() + 15
    while time.monotonic() < until:
        try:
            current = read_state(directory)
            request(current, "/health", timeout=0.3)
            return current
        except (OSError, ValueError, RuntimeError, http.client.HTTPException):
            time.sleep(0.05)
    raise RuntimeError("Embedding worker startup unavailable")


class SharedMlxBackend:
    """A small per-process client; model and Metal allocations live in the worker."""

    def __init__(self) -> None:
        self.directory = runtime_directory()
        self._state: dict | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._active = True
        self._maintenance: threading.Thread | None = None
        # The last time something wanted a vector. Health pings do not count: without
        # demand the worker unloads its model after idle_seconds() and is not restarted.
        self._last_demand = time.monotonic()

    def load(self) -> None:
        self._state = ensure_runtime(self.directory)
        # Initial model preparation can download weights. This method runs only
        # during explicit preparation or background warmup, never on a query.
        until = time.monotonic() + 30 * 60
        while time.monotonic() < until:
            health = request(self._state, "/health")
            if health["state"] == "ready":
                break
            if health["state"] == "failed":
                raise RuntimeError("Embedding model unavailable")
            time.sleep(0.1)
        else:
            raise RuntimeError("Embedding model warmup timed out")
        atexit.register(self.close)

        def maintain() -> None:
            from .embedder import _enabled
            while not self._stop.is_set():
                self._wake.wait(30)
                self._wake.clear()
                if self._stop.is_set():
                    return
                if not self._active or not _enabled():
                    continue
                if time.monotonic() - self._last_demand > idle_seconds():
                    # Idle: let the worker exit; the next encode request revives it.
                    self._state = None
                    continue
                try:
                    current = ensure_runtime(self.directory)
                    if not self._stop.is_set() and self._active:
                        self._state = current
                except Exception:
                    self._state = None

        self._maintenance = threading.Thread(target=maintain, name="astra-embedding-client", daemon=True)
        self._maintenance.start()

    def encode(self, texts):
        import math
        from .embedder import EmbeddingUnavailable
        state = self._state
        self._last_demand = time.monotonic()
        if state is None:
            # Restart off the request path; this request keeps its lexical results.
            self._wake.set()
            raise EmbeddingUnavailable("Embedding worker is cold")
        if not texts:
            return []
        try:
            value = request(state, "/encode", {"texts": list(texts)}, timeout=10)
        except EmbeddingUnavailable:
            # The worker may have unloaded after idling; bring it back in the background.
            self._wake.set()
            raise
        vectors = value.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ValueError("Embedding response count mismatch")
        size = len(vectors[0]) if isinstance(vectors[0], list) else 0
        if not 0 < size <= 16384 or any(
            not isinstance(row, list) or len(row) != size
            or not all(isinstance(value, (float, int)) and math.isfinite(value) for value in row)
            for row in vectors
        ):
            raise ValueError("Invalid embedding response")
        return vectors

    def memory_stats(self) -> dict:
        return request(self._state, "/health").get("memory", {}) if self._state else {}

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        self._state = None
        atexit.unregister(self.close)

    def pause(self) -> None:
        self._active = False
        self._state = None

    def resume(self) -> None:
        self._active = True
        self._wake.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or stop Astra's shared local embedding worker")
    parser.add_argument("action", choices=("status", "stop"), default="status", nargs="?")
    parser.add_argument("--directory", type=Path, default=runtime_directory())
    args = parser.parse_args(argv)
    try:
        state = read_state(args.directory)
        value = request(state, "/stop", {}) if args.action == "stop" else request(state, "/health")
        print(json.dumps(value, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, http.client.HTTPException):
        print(json.dumps({"state": "absent"}))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
