import json
from types import SimpleNamespace

import httpx
import pytest

from agent.runtime.context_index import embedder, vector_index
from agent.runtime.context_index.factory import _budget


@pytest.mark.parametrize('platform,expected', [('darwin', 'mlx'), ('win32', 'llamacpp'), ('linux', 'llamacpp')])
def test_platform_backend_defaults_and_explicit_override(monkeypatch, platform, expected):
    monkeypatch.setattr(embedder.sys, 'platform', platform)
    monkeypatch.delenv('ASTRA_EMBEDDING_BACKEND', raising=False)
    assert embedder.backend_name() == expected
    monkeypatch.setenv('ASTRA_EMBEDDING_BACKEND', 'mlx')
    from agent.runtime.context_index.embedding_runtime import SharedMlxBackend
    assert isinstance(embedder._default_backend(), SharedMlxBackend)
    monkeypatch.setenv('ASTRA_EMBEDDING_BACKEND', 'llamacpp')
    assert isinstance(embedder._default_backend(), embedder._HttpBackend)


def http_backend(monkeypatch, handler):
    original = httpx.Client
    clients = []
    def factory(**kwargs):
        assert kwargs['trust_env'] is False
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(httpx, 'Client', factory)
    backend = embedder._HttpBackend()
    backend.load()
    return backend, clients


def test_http_embeddings_order_auth_and_client_reuse(monkeypatch):
    monkeypatch.setenv('ASTRA_EMBEDDING_API_KEY', 'test-only')
    def handler(request):
        assert request.url.path == '/v1/embeddings'
        assert request.headers['Authorization'] == 'Bearer test-only'
        assert json.loads(request.content)['input'] == ['a', 'b']
        return httpx.Response(200, json={'data': [
            {'index': 1, 'embedding': [0, 1]}, {'index': 0, 'embedding': [1, 0]},
        ]})
    backend, clients = http_backend(monkeypatch, handler)
    try:
        for _ in range(2):
            assert backend.encode(['a', 'b']) == [[1, 0], [0, 1]]
        assert len(clients) == 1
    finally:
        backend.client.close()


@pytest.mark.parametrize('data', [[], [{'index': 1, 'embedding': [1]}],
    [{'index': 0, 'embedding': []}], [{'index': 0, 'embedding': ['nan']}],
    [{'index': 0, 'embedding': [1]}, {'index': 0, 'embedding': [1]}]])
def test_invalid_http_vectors_degrade(monkeypatch, data):
    backend, _ = http_backend(monkeypatch, lambda request: httpx.Response(200, json={'data': data}))
    try:
        assert embedder.Embedder(backend).encode(['one']) is None
    finally:
        backend.client.close()


def test_http_timeout_degrades(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout('test timeout', request=request)
    backend, _ = http_backend(monkeypatch, handler)
    try:
        assert embedder.Embedder(backend).encode(['one']) is None
    finally:
        backend.client.close()


def test_vector_models_are_isolated_even_at_same_dimension(monkeypatch, tmp_path):
    monkeypatch.delenv(vector_index.ENV_DB_PATH, raising=False)
    monkeypatch.setenv('ASTRA_EMBEDDING_BACKEND', 'mlx')
    mac_path = vector_index.default_vectors_db_path()
    assert mac_path.name == 'context-vectors.db'
    path = tmp_path / 'vectors.db'
    store = vector_index.VectorStore(path)
    store.upsert('one', 'hash', [1, 0])
    store.close()
    monkeypatch.setenv('ASTRA_EMBEDDING_BACKEND', 'llamacpp')
    assert vector_index.default_vectors_db_path() != mac_path
    with pytest.raises(ValueError, match='different embedding model'):
        vector_index.read_vector_snapshot(path)
    with pytest.raises(ValueError, match='Embedding model changed'):
        vector_index.VectorStore(path)


def test_configured_budgets_are_bounded(monkeypatch):
    for value, expected in [('invalid', 75), ('-1', 25), ('99999', 2000)]:
        monkeypatch.setenv('TEST_CONTEXT_BUDGET', value)
        assert _budget('TEST_CONTEXT_BUDGET', 75) == expected


def test_relevance_timeout_does_not_discard_recency(monkeypatch, tmp_path):
    from test_context_index_session_source import _create_recall_database, _add_session, WORKSPACE, NOW
    from agent.runtime.context_index import session_source
    from agent.runtime.context_index.session_source import SessionRecommendationSource
    path = tmp_path / 'sessions.db'
    connection = _create_recall_database(path)
    _add_session(connection, 'old', 'history', WORKSPACE.key, [('user', 'useful older memory', NOW - 1)])
    connection.commit()
    connection.close()
    clock = [0.0]
    monkeypatch.setattr(session_source, 'time', SimpleNamespace(perf_counter=lambda: clock[0]))
    def slow_relevance(self, connection, *args, **kwargs):
        # Advance past the total source deadline, independent of channel order
        # or host speed, then exercise SQLite's actual progress interruption.
        clock[0] = 0.201
        connection.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n').fetchone()
        raise AssertionError('relevance must be interrupted after the source deadline')
    monkeypatch.setattr(SessionRecommendationSource, '_relevance_rows', slow_relevance)
    result = SessionRecommendationSource(path, deadline_ms=200).recommend('needle', WORKSPACE, 'current', frozenset(), NOW)
    assert result.availability == 'available'
    assert result.recency
    assert not result.relevance
    assert result.error_category == 'deadline'
    assert result.diagnostics == ('relevance_omitted',)
