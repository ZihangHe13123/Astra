"""Stable read-only change-card navigation through the real snapshot ledger."""
from types import SimpleNamespace

import pytest

from agent.runtime.turn_change_store import TurnChangeStore
from agent.ui.queries import query


def test_change_source_survives_new_turns_and_never_falls_back_to_another_record(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = TurnChangeStore(workspace, "conversation:branch-A", root=tmp_path / "snapshots")
    agent = SimpleNamespace(turn_change_store=lambda: store)

    def write_turn(name, request_id, content):
        path = workspace / name
        store.begin_turn(request_id)
        store.note_absent(path)
        path.write_text(content)
        manifest = store.seal()
        assert manifest is not None
        return {"session_id": manifest.session_id, "request_id": manifest.request_id, "turn_seq": manifest.turn_seq}

    def read(source=None, **params):
        return query({"method": "changes", "params": {**params, **({"source": source} if source is not None else {})}}, agent=agent)

    try:
        old = write_turn("old.txt", "request-old", "old turn\n")
        latest = write_turn("new.txt", "request-new", "new turn\n")
        assert read()["source"] == latest
        old_view = read(old, index=0)
        assert old_view["source"] == old and old_view["turn"] == 2
        assert old_view["sides"]["after"] == "old turn\n"
        write_turn("newest.txt", "request-newest", "newest turn\n")
        still_old = read(old, index=0)
        assert still_old["source"] == old and still_old["turn"] == 3
        assert still_old["sides"]["after"] == "old turn\n"
        for mismatch in [{**old, "session_id": "conversation:branch-B"}, {**old, "request_id": "wrong"}, {**old, "turn_seq": 999}]:
            with pytest.raises(ValueError):
                read(mismatch)
        for invalid in [{}, {**old, "turn_seq": True}, {**old, "turn_seq": -1}, {**old, "turn_seq": "1"}]:
            with pytest.raises(ValueError, match="Invalid change source"):
                read(invalid)
        assert read()["source"]["request_id"] == "request-newest"
    finally:
        store.close()


def test_missing_store_cannot_redirect_an_old_card_to_latest():
    with pytest.raises(ValueError, match="指定回合"):
        query({"method": "changes", "params": {"source": {"session_id": "s", "request_id": "r", "turn_seq": 1}}},
              agent=SimpleNamespace(turn_change_store=lambda: None))
