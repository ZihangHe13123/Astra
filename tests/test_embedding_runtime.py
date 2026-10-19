"""Native embedding IPC and memory ownership contracts, without model loading."""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent.runtime.context_index import embedder, embedding_runtime as runtime
from agent.runtime.context_index.embedding_worker import serve


class Backend:
    def __init__(self):
        self.loads = 0
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()

    def load(self):
        self.loads += 1

    def encode(self, texts):
        self.calls.append(texts)
        if texts == ["slow"]:
            self.started.set()
            self.release.wait(3)
        return [[float(len(text)), 1.0] for text in texts]


@pytest.fixture
def worker(tmp_path):
    backend = Backend()
    thread = threading.Thread(target=serve, args=(tmp_path, backend), daemon=True)
    thread.start()
    until = time.monotonic() + 3
    state = None
    while time.monotonic() < until:
        try:
            state = runtime.read_state(tmp_path)
            if runtime.request(state, "/health")["state"] == "ready":
                break
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(0.01)
    assert state and runtime.request(state, "/health")["state"] == "ready"
    yield SimpleNamespace(directory=tmp_path, backend=backend, state=state)
    backend.release.set()
    runtime.request(state, "/stop", {})
    thread.join(3)
    assert not thread.is_alive()


def test_clients_share_one_model_and_a_second_owner_never_loads(worker, monkeypatch):
    monkeypatch.setenv("ASTRA_EMBEDDING_RUNTIME_DIR", str(worker.directory))
    second_owner = Backend()
    assert serve(worker.directory, second_owner) == 0
    assert second_owner.loads == 0
    clients = [runtime.SharedMlxBackend(), runtime.SharedMlxBackend()]
    try:
        for client in clients:
            client.load()
            assert client.encode(["abc", "x"]) == [[3.0, 1.0], [1.0, 1.0]]
        assert clients[0]._state == clients[1]._state == worker.state
        assert worker.backend.loads == 1
    finally:
        for client in clients:
            client.close()


def test_loading_worker_keeps_health_responsive_and_owns_model_exclusively(tmp_path):
    class LoadingBackend(Backend):
        def load(self):
            self.loads += 1
            self.started.set()
            assert self.release.wait(3)

    backend = LoadingBackend()
    thread = threading.Thread(target=serve, args=(tmp_path, backend), daemon=True)
    thread.start()
    assert backend.started.wait(2)
    state = runtime.read_state(tmp_path)
    try:
        assert runtime.request(state, "/health")["state"] == "loading"
        with pytest.raises(embedder.EmbeddingUnavailable):
            runtime.request(state, "/encode", {"texts": ["query"]})
        other = Backend()
        assert serve(tmp_path, other) == 0
        assert other.loads == 0
    finally:
        backend.release.set()
        runtime.request(state, "/stop", {})
        thread.join(3)
    assert not thread.is_alive()


def test_busy_worker_declines_without_queueing_another_model_job(worker):
    outcomes = []
    pending = threading.Thread(target=lambda: outcomes.append(runtime.request(worker.state, "/encode", {"texts": ["slow"]})))
    pending.start()
    try:
        assert worker.backend.started.wait(1)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="unavailable"):
            runtime.request(worker.state, "/encode", {"texts": ["another"]})
        assert time.monotonic() - started < 0.5
        assert ["another"] not in worker.backend.calls
        with pytest.raises(embedder.EmbeddingUnavailable):
            runtime.request(worker.state, "/stop", {})
        assert runtime.request(worker.state, "/health")["busy"]
    finally:
        worker.backend.release.set()
        pending.join(3)
    assert outcomes[0]["vectors"]


def test_unauthorized_and_oversized_requests_never_reach_model(worker):
    with pytest.raises(RuntimeError):
        runtime.request({**worker.state, "token": "x" * 64}, "/encode", {"texts": ["private"]})
    with pytest.raises(RuntimeError):
        runtime.request(worker.state, "/encode", {"texts": ["x"] * 17})
    with pytest.raises(ValueError, match="too large"):
        runtime.request(worker.state, "/encode", {"texts": ["x" * runtime.MAX_BODY]})
    assert worker.backend.calls == [["context index warm up"]]


def test_query_encoding_never_bootstraps_a_worker(monkeypatch):
    monkeypatch.setattr(runtime, "ensure_runtime", lambda *_: pytest.fail("query must not start worker"))
    with pytest.raises(RuntimeError, match="cold"):
        runtime.SharedMlxBackend().encode(["query"])


def test_disconnected_worker_is_a_quiet_lexical_fallback(monkeypatch, caplog):
    client = runtime.SharedMlxBackend()
    client._state = {"port": 1, "token": "x" * 64}

    def disconnected(*args, **kwargs):
        raise ConnectionResetError("connection closed")

    monkeypatch.setattr(runtime.http.client.HTTPConnection, "request", disconnected)
    assert embedder.Embedder(client).encode(["query"]) is None
    assert not caplog.records


def test_resume_recovers_off_request_path_and_close_cannot_revive_client(worker, monkeypatch):
    monkeypatch.setenv("ASTRA_EMBEDDING_RUNTIME_DIR", str(worker.directory))
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "on")
    client = runtime.SharedMlxBackend()
    client.load()
    client.pause()
    assert embedder.Embedder(client).encode(["query"]) is None
    restarted, allow_restart, completed = threading.Event(), threading.Event(), threading.Event()
    recovered = {**worker.state, "pid": worker.state["pid"] + 1}

    def restart(_):
        restarted.set()
        allow_restart.wait(2)
        completed.set()
        return recovered

    monkeypatch.setattr(runtime, "ensure_runtime", restart)
    try:
        client.resume()
        assert restarted.wait(1)
        assert client._state is None  # No work or wait has entered the query path.
        allow_restart.set()
        until = time.monotonic() + 2
        while client._state != recovered and time.monotonic() < until:
            time.sleep(.01)
        assert client._state == recovered

        restarted.clear()
        allow_restart.clear()
        completed.clear()
        client._wake.set()
        assert restarted.wait(1)
        client.close()
        allow_restart.set()
        assert completed.wait(1)
        assert client._maintenance is not None
        client._maintenance.join(2)
        assert not client._maintenance.is_alive()
        assert client._state is None
    finally:
        client.close()
        allow_restart.set()


@pytest.mark.parametrize("vectors", [None, [], [[float("nan")]], [[1.0], [1.0]], [[]], [["1"]]])
def test_invalid_worker_vectors_are_rejected(monkeypatch, vectors):
    client = runtime.SharedMlxBackend()
    client._state = {"port": 1}
    monkeypatch.setattr(runtime, "request", lambda *args, **kwargs: {"vectors": vectors})
    with pytest.raises(ValueError):
        client.encode(["query"])


@pytest.mark.parametrize("metadata", [[], {}, {"protocol": 999}])
def test_invalid_rendezvous_metadata_is_rejected(tmp_path, metadata):
    path = runtime.state_path(tmp_path)
    path.write_text(json.dumps(metadata))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        runtime.read_state(tmp_path)


def test_idle_worker_releases_ownership_and_removes_endpoint(tmp_path):
    backend = Backend()
    thread = threading.Thread(target=serve, args=(tmp_path, backend), kwargs={"idle_seconds": 0.1})
    thread.start()
    thread.join(3)
    assert not thread.is_alive()
    assert not runtime.state_path(tmp_path).exists()


def _start_worker(tmp_path, idle_seconds):
    backend = Backend()
    thread = threading.Thread(target=serve, args=(tmp_path, backend), kwargs={"idle_seconds": idle_seconds},
                              daemon=True)
    thread.start()
    until = time.monotonic() + 3
    while time.monotonic() < until:
        try:
            state = runtime.read_state(tmp_path)
            if runtime.request(state, "/health")["state"] == "ready":
                return thread, state
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(0.01)
    raise AssertionError("worker did not start")


def test_health_checks_do_not_keep_an_idle_model_loaded(tmp_path):
    thread, state = _start_worker(tmp_path, idle_seconds=0.5)
    until = time.monotonic() + 3
    with pytest.raises(embedder.EmbeddingUnavailable):
        # Clients check health every 30 s while they run; that is not use.
        while time.monotonic() < until:
            runtime.request(state, "/health", timeout=0.3)
            time.sleep(0.05)
    thread.join(3)
    assert not thread.is_alive()
    assert not runtime.state_path(tmp_path).exists()


def test_encode_requests_keep_the_model_loaded(tmp_path):
    thread, state = _start_worker(tmp_path, idle_seconds=0.5)
    try:
        until = time.monotonic() + 1.5
        while time.monotonic() < until:
            assert runtime.request(state, "/encode", {"texts": ["query"]})["vectors"]
            time.sleep(0.1)
        assert thread.is_alive()
    finally:
        runtime.request(state, "/stop", {})
        thread.join(3)


def test_an_idle_client_lets_the_worker_go_until_a_vector_is_needed(worker, monkeypatch):
    monkeypatch.setenv("ASTRA_EMBEDDING_RUNTIME_DIR", str(worker.directory))
    monkeypatch.setenv("ASTRA_CONTEXT_INDEX_EMBEDDING", "on")
    client = runtime.SharedMlxBackend()
    client.load()
    restarts = []
    monkeypatch.setattr(runtime, "ensure_runtime", lambda directory: restarts.append(directory) or worker.state)
    monkeypatch.setattr(runtime, "idle_seconds", lambda: 0.2)
    try:
        client._last_demand = time.monotonic() - 1
        client._wake.set()
        until = time.monotonic() + 2
        while client._state is not None and time.monotonic() < until:
            time.sleep(0.01)
        assert client._state is None and not restarts  # idle: no restart, model may unload
        # A query finds it cold, keeps its lexical results, and wakes a background restart.
        assert embedder.Embedder(client).encode(["query"]) is None
        until = time.monotonic() + 2
        while client._state is None and time.monotonic() < until:
            time.sleep(0.01)
        assert restarts and client._state == worker.state
    finally:
        client.close()


def test_mlx_materializes_plain_numbers_with_single_text_batches_and_clears_scratch(monkeypatch):
    calls = []

    class Vectors:
        def __iter__(self):
            pytest.fail("iterating MLX rows retains scalar graphs")

        def tolist(self):
            return [[1.0, 2.0]]

    mx = SimpleNamespace(set_cache_limit=lambda value: calls.append(("cache", value)),
                         set_memory_limit=lambda value: calls.append(("limit", value)),
                         eval=lambda *_: None, synchronize=lambda: None,
                         clear_cache=lambda: calls.append("clear"))
    model = SimpleNamespace(parameters=lambda: {})

    def generate(*args, **kwargs):
        assert len(kwargs["texts"]) == 1 and kwargs["max_length"] == 512
        calls.append(kwargs["texts"])
        return SimpleNamespace(text_embeds=Vectors())

    libraries = {"mlx.core": mx, "mlx_embeddings": SimpleNamespace(load=lambda _: (model, None), generate=generate)}
    monkeypatch.setattr(embedder.importlib, "import_module", libraries.__getitem__)
    backend = embedder._MlxBackend()
    backend.load()
    assert backend.encode(["one", "two"]) == [[1.0, 2.0], [1.0, 2.0]]
    assert ("cache", 0) in calls
    assert calls.count("clear") == 3


def test_mlx_failure_also_releases_scratch(monkeypatch):
    cleared = []
    mx = SimpleNamespace(synchronize=lambda: None, clear_cache=lambda: cleared.append(True))

    def fail(*args, **kwargs):
        raise RuntimeError("allocation failed")

    libraries = {"mlx.core": mx, "mlx_embeddings": SimpleNamespace(generate=fail)}
    monkeypatch.setattr(embedder.importlib, "import_module", libraries.__getitem__)
    with pytest.raises(RuntimeError, match="allocation failed"):
        embedder._MlxBackend().encode(["one"])
    assert cleared == [True]


def test_mlx_sync_failure_still_clears_scratch(monkeypatch):
    cleared = []

    def fail():
        raise RuntimeError("device failure")

    mx = SimpleNamespace(eval=lambda *_: None, synchronize=fail, clear_cache=lambda: cleared.append(True))
    vectors = SimpleNamespace(tolist=lambda: [[1.0]])
    libraries = {"mlx.core": mx, "mlx_embeddings": SimpleNamespace(generate=lambda *args, **kwargs: SimpleNamespace(text_embeds=vectors))}
    monkeypatch.setattr(embedder.importlib, "import_module", libraries.__getitem__)
    with pytest.raises(RuntimeError, match="device failure"):
        embedder._MlxBackend().encode(["one"])
    assert cleared == [True]
