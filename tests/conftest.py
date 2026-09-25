"""pytest configuration — robust tmp_path on Windows.

On Windows, ``os.scandir`` on the basetemp (``pytest-of-<user>``) can
intermittently fail with ``PermissionError: [WinError 5]`` when a filter
driver (anti-virus, search indexer, etc.) temporarily holds the directory.
pytest's own retry loop does **not** include a delay, so all 10 attempts
can fail immediately.

This conftest monkey-patches ``_pytest.pathlib.find_prefixed`` to retry
with an increasing delay, giving transient locks time to clear.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolate_application_environment(request, monkeypatch):
    # A test launched by Astra inherits its installation, workspace and provider
    # overrides. Each test must be able to choose those independently. Keep
    # explicit acceptance flags and credentials available to opt-in live tests.
    if request.node.get_closest_marker("allow_application_environment") is not None:
        return
    keep = set()
    if request.node.get_closest_marker("allow_real_session_recall_db") is not None:
        keep.update({"ASTRA_SESSION_RECALL_DB", "ASTRA_CONTEXT_INDEX_SESSIONS_DB"})
    for name in tuple(os.environ):
        if name.startswith(("ASTRA_", "AGENT_", "LLM_", "SANDBOX_")) and not (
            name.endswith(("_API_KEY", "_LIVE_TEST")) or "_E2E" in name
            or name.startswith("ASTRA_RUN_")
            or name in keep | {"ASTRA_LIVE_TESTS", "ASTRA_PROVIDER_SMOKE"}
        ):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _isolate_provider_connections(tmp_path, monkeypatch, _isolate_application_environment):
    monkeypatch.setenv("AGENT_CONNECTIONS_DIR", str(tmp_path / "connections"))
    monkeypatch.setenv("AGENT_MODEL_CACHE_DIR", str(tmp_path / "model-cache"))


@pytest.fixture(autouse=True)
def _isolate_local_personas(tmp_path, monkeypatch, _isolate_application_environment):
    monkeypatch.setenv("ASTRA_PERSONA_FILE", str(tmp_path / "persona.local.json"))
    monkeypatch.setenv("ASTRA_LOCAL_MODE_FILE", str(tmp_path / "missing-local-mode.py"))


@pytest.fixture(autouse=True)
def _isolate_learning_archive(tmp_path, monkeypatch, _isolate_application_environment):
    # Local history is now exposed by session_search too. Tests and their child
    # processes must never read or create the user's real learning archive.
    monkeypatch.setenv("AGENT_LEARNING_PATH", str(tmp_path / "learning.db"))


@pytest.fixture(autouse=True)
def _isolate_peer_mailbox(tmp_path, monkeypatch, _isolate_application_environment):
    # A test backend would otherwise list itself in the user's real directory of
    # open Astra sessions, where the user's own sessions could message it.
    monkeypatch.setenv("ASTRA_PEER_DB", str(tmp_path / "peers.db"))


@pytest.fixture(autouse=True)
def _isolate_session_recall_db(request, tmp_path, monkeypatch, _isolate_application_environment):
    if request.node.get_closest_marker("allow_real_session_recall_db") is not None:
        return
    monkeypatch.setenv(
        "ASTRA_SESSION_RECALL_DB",
        str(tmp_path / "session-recall.db"),
    )
    monkeypatch.setenv(
        "ASTRA_CONTEXT_INDEX_SESSIONS_DB",
        str(tmp_path / "session-recall.db"),
    )


def _patch_win32_scandir_retry() -> None:
    if sys.platform != "win32":
        return

    from _pytest import pathlib as _pl  # type: ignore[attr-defined]

    _original = _pl.find_prefixed

    def _robust_find_prefixed(
        root: Path, prefix: str
    ) -> Iterator[os.DirEntry[str]]:
        max_retries = 6
        for attempt in range(max_retries):
            try:
                yield from _original(root, prefix)
                return
            except PermissionError:
                if attempt == max_retries - 1:
                    raise
                time.sleep(0.15 * (attempt + 1))

    _pl.find_prefixed = _robust_find_prefixed


def pytest_configure(config: pytest.Config) -> None:
    # Keep test-only registration out of the launcher's dependency fingerprint.
    config.addinivalue_line(
        "markers",
        "allow_application_environment: opt-in live acceptance using inherited application settings",
    )
    # Isolate import-time prompt construction too, before test modules import
    # AgentContext. Per-test fixtures below can then supply synthetic overrides.
    persona_state = tempfile.TemporaryDirectory(prefix="astra-test-persona-")
    config.add_cleanup(persona_state.cleanup)
    patch = pytest.MonkeyPatch()
    patch.setenv("ASTRA_PERSONA_FILE", os.path.join(persona_state.name, "persona.local.json"))
    patch.setenv("ASTRA_LOCAL_MODE_FILE", os.path.join(persona_state.name, "missing-local-mode.py"))
    config.add_cleanup(patch.undo)
    _patch_win32_scandir_retry()
