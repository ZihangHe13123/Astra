"""Windows cache identities use metadata change time and close every handle."""
import ctypes
import os
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.runtime import file_change_time
from agent.runtime.session_store import SessionStore
from agent.ui import history_index


@pytest.fixture
def windows_api(monkeypatch):
    state = {"change": 123456789012345, "error": 5, "query_ok": True, "close_ok": True}

    def query(handle, kind, pointer, size):
        assert handle == 0x1_0000_0001  # Never truncate a 64-bit HANDLE.
        assert kind == 0 and size == 40
        pointer._obj.CreationTime = 10
        pointer._obj.LastWriteTime = 20
        pointer._obj.ChangeTime = state["change"]
        return int(state["query_ok"])

    def close(_handle):
        if not state["close_ok"]:
            state["error"] = 6
        return int(state["close_ok"])

    api = SimpleNamespace(CreateFileW=Mock(return_value=0x1_0000_0001),
                          GetFileInformationByHandleEx=Mock(side_effect=query), CloseHandle=Mock(side_effect=close))
    loader = Mock(return_value=api)
    monkeypatch.setattr(file_change_time, "sys", SimpleNamespace(platform="win32", exc_info=sys.exc_info))
    monkeypatch.setattr(ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: state["error"], raising=False)
    monkeypatch.setattr(ctypes, "WinError", lambda code: OSError(code, "simulated Win32 error"), raising=False)
    file_change_time._kernel32.cache_clear()
    yield api, state, loader
    file_change_time._kernel32.cache_clear()


def test_posix_uses_stat_metadata_without_loading_windows_api(tmp_path, monkeypatch):
    path = tmp_path / "source"
    path.write_text("same")
    info = path.stat()
    monkeypatch.setattr(file_change_time, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(file_change_time, "_kernel32", Mock(side_effect=AssertionError("not Windows")))
    assert file_change_time.change_time_token(path, info) == info.st_ctime_ns


def test_windows_abi_sharing_and_change_time_are_independent_of_mtime(tmp_path, windows_api):
    api, state, loader = windows_api
    path = tmp_path / "数据.json"
    info = SimpleNamespace(st_ctime_ns=11, st_mtime_ns=22)
    assert ctypes.sizeof(file_change_time._FileBasicInfo) == 40
    assert file_change_time._FileBasicInfo.ChangeTime.offset == 24
    assert file_change_time._FileBasicInfo.FileAttributes.offset == 32
    assert file_change_time.change_time_token(path, info) == state["change"]
    state["change"] += 1
    assert file_change_time.change_time_token(path, info) == state["change"]
    loader.assert_called_once_with("kernel32", use_last_error=True)
    assert api.CreateFileW.call_args.args == (str(path), 0x80, 7, None, 3, 0, None)
    assert api.CreateFileW.restype is ctypes.c_void_p
    assert api.CloseHandle.call_args_list == [((0x1_0000_0001,),), ((0x1_0000_0001,),)]


@pytest.mark.parametrize("handle", [None, ctypes.c_void_p(-1).value])
def test_open_failure_is_not_a_weak_token_or_a_handle_to_close(tmp_path, windows_api, handle):
    api, _, _ = windows_api
    api.CreateFileW.return_value = handle
    with pytest.raises(OSError) as caught:
        file_change_time.change_time_token(tmp_path / "source", SimpleNamespace(st_ctime_ns=1))
    assert caught.value.errno == 5
    api.GetFileInformationByHandleEx.assert_not_called()
    api.CloseHandle.assert_not_called()


@pytest.mark.parametrize("close_ok", [True, False])
def test_query_failure_closes_handle_and_preserves_original_error(tmp_path, windows_api, close_ok):
    api, state, _ = windows_api
    state.update(query_ok=False, close_ok=close_ok)
    with pytest.raises(OSError) as caught:
        file_change_time.change_time_token(tmp_path / "source", SimpleNamespace(st_ctime_ns=1))
    assert caught.value.errno == 5
    api.CloseHandle.assert_called_once_with(0x1_0000_0001)


def test_close_failure_does_not_publish_a_successful_token(tmp_path, windows_api):
    _, state, _ = windows_api
    state["close_ok"] = False
    with pytest.raises(OSError) as caught:
        file_change_time.change_time_token(tmp_path / "source", SimpleNamespace(st_ctime_ns=1))
    assert caught.value.errno == 6


def test_unsupported_change_time_fails_closed_and_closes_handle(tmp_path, windows_api):
    api, state, _ = windows_api
    state["change"] = 0
    with pytest.raises(OSError, match="usable file change time"):
        file_change_time.change_time_token(tmp_path / "source", SimpleNamespace(st_ctime_ns=1))
    api.CloseHandle.assert_called_once_with(0x1_0000_0001)


def test_changed_windows_token_rebuilds_same_size_same_mtime_cache(tmp_path, monkeypatch, windows_api):
    _, state, _ = windows_api
    monkeypatch.setenv("ASTRA_GUI_HISTORY_CACHE", str(tmp_path / "cache"))
    store = SessionStore(tmp_path / "session.json")
    store.legacy_path.write_text('{"messages":[{"role":"user","content":"added"}]}')
    frozen = store.legacy_path.stat()
    original_stat = type(store.legacy_path).stat

    def stat(path, *args, **kwargs):
        if path == store.legacy_path:
            return frozen  # Reproduce creation-time st_ctime with restored mtime.
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(type(store.legacy_path), "stat", stat)
    assert history_index.history_page(store)["messages"][0]["content"] == "added"
    first = history_index._signature(store)
    store.legacy_path.write_text('{"messages":[{"role":"user","content":"other"}]}')
    os.utime(store.legacy_path, ns=(frozen.st_atime_ns, frozen.st_mtime_ns))
    state["change"] += 1
    assert history_index._signature(store) != first
    assert history_index.history_page(store)["messages"][0]["content"] == "other"


def test_signature_read_failure_never_serves_cached_history(tmp_path, monkeypatch, windows_api):
    _, state, _ = windows_api
    monkeypatch.setenv("ASTRA_GUI_HISTORY_CACHE", str(tmp_path / "cache"))
    store = SessionStore(tmp_path / "session.json")
    store.legacy_path.write_text('{"messages":[{"role":"user","content":"private"}]}')
    assert history_index.history_page(store)["messages"]
    state["query_ok"] = False
    with pytest.raises(OSError):
        history_index.history_page(store)
