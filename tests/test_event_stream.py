from agent.runtime.event_stream import RuntimeEventStream, safe_replay_event


def test_reconnect_scope_cannot_replay_another_tui_task(tmp_path, monkeypatch):
    path = tmp_path / "events.db"
    monkeypatch.setenv("ASTRA_EVENT_SCOPE", "tui-a")
    first = RuntimeEventStream(path)
    first.publish({"type": "task_started", "task": {"id": "a", "status": "running"}})
    monkeypatch.setenv("ASTRA_EVENT_SCOPE", "tui-b")
    other = RuntimeEventStream(path)
    other.publish({"type": "done"})
    monkeypatch.setenv("ASTRA_EVENT_SCOPE", "tui-a")
    reconnected = RuntimeEventStream(path)
    assert [event["type"] for event in reconnected.replay(0)] == ["task_started"]
    assert first.generation != reconnected.generation


def test_every_live_event_gets_an_envelope_but_only_safe_types_persist(tmp_path):
    stream = RuntimeEventStream(tmp_path / "events.db", retention=100)

    chunk = stream.publish({"type": "chunk", "content": "private model text"})
    progress = stream.publish(
        {
            "type": "tool_progress",
            "call_id": "call-1",
            "name": "write_file",
            "stage": "running",
            "status": "running",
            "message": "secret path",
        }
    )

    assert chunk["event_id"]
    assert chunk["sequence"] == 1
    assert chunk["replayable"] is False
    assert progress["sequence"] == 2
    assert progress["cursor"] == 1
    assert progress["replayable"] is True
    replayed = stream.replay(0)
    assert replayed == [
        {
            "type": "tool_progress",
            "call_id": "call-1",
            "name": "write_file",
            "stage": "running",
            "status": "running",
            "event_id": progress["event_id"],
            "sequence": 1,
            "cursor": 1,
            "replayable": True,
            "replayed": True,
        }
    ]


def test_replay_cursor_is_stable_across_process_instances(tmp_path):
    path = tmp_path / "events.db"
    first = RuntimeEventStream(path, retention=100)
    one = first.publish({"type": "task_started", "task": {"id": "t1", "status": "running"}})
    second = RuntimeEventStream(path, retention=100)
    two = second.publish({"type": "done"})

    assert one["cursor"] == 1
    assert two["cursor"] == 2
    assert [event["cursor"] for event in second.replay(1)] == [2]


def test_terminal_replay_across_process_instances_preserves_cancelled_state(tmp_path):
    path = tmp_path / "events.db"
    stream = RuntimeEventStream(path, retention=100)
    stream.publish({"type": "task_started", "task": {"id": "t1", "status": "running"}})
    stream.publish({"type": "task_status", "task": {"id": "t1", "status": "cancelled"}})
    stream.publish({"type": "done"})

    replayed = RuntimeEventStream(path, retention=100).replay(0)

    cursors = [event["cursor"] for event in replayed]
    assert cursors == sorted(set(cursors))
    assert [event["type"] for event in replayed][-2:] == ["task_status", "done"]
    assert replayed[-2]["task"]["status"] == "cancelled"


def test_replay_approval_drops_unknown_and_raw_fields():
    safe = safe_replay_event(
        {
            "type": "tool_approval_request",
            "request_id": "a1",
            "tool_name": "write_file",
            "arguments": {"content": "<9000 chars preserved>"},
            "raw_arguments": {"content": "secret"},
            "choices": ["once", "session", "deny", "always"],
        }
    )

    assert safe is not None
    assert "raw_arguments" not in safe
    assert safe["arguments"] == {"content": "<9000 chars preserved>"}
    assert safe["choices"] == ["once", "session", "deny"]


def test_reconnect_replays_presentation_intent_without_private_takeover_fields(tmp_path):
    path = tmp_path / "events.db"
    RuntimeEventStream(path).publish({
        "type": "tool_approval_request", "request_id": "show-result", "kind": "computer_foreground_takeover",
        "choices": ["once", "deny"], "scope": "private scope", "target": "private target",
        "arguments": {"window": "Fixture", "present_result": True, "takeover_ref": "private ref"},
    })

    event, = RuntimeEventStream(path).replay(0)
    assert event["request_id"] == "show-result" and event["choices"] == ["once", "deny"]
    assert event["arguments"] == {"window": "Fixture", "present_result": True}
    assert event["arguments"]["present_result"] is True
    assert "scope" not in event and "target" not in event


def test_replay_keeps_the_crew_and_its_members_group(tmp_path):
    stream = RuntimeEventStream(tmp_path / "events.db")
    stream.publish({"type": "agent_team", "event": "team_created", "team_kind": "crew", "team_id": "one"})
    stream.publish({"type": "agent_team", "event": "team_agent_created", "group": "editors",
                    "agent_id": "worker", "team_id": "one", "message": "not public metadata"})
    created, member = stream.replay(0)
    assert created["team_kind"] == "crew"
    assert member["group"] == "editors"
    assert "message" not in member


def test_replay_agent_team_keeps_only_frontend_safe_coordination_fields():
    safe = safe_replay_event(
        {
            "type": "agent_team",
            "event": "team_message",
            "kind": "agent_team",
            "team_id": "team-1",
            "sender_agent_id": "agent-1",
            "recipient_agent_ids": ["agent-2"],
            "message_kind": "text",
            "message_count": 1,
            "message": "private teammate content",
            "raw_payload": {"secret": True},
        }
    )

    assert safe == {
        "type": "agent_team",
        "event": "team_message",
        "kind": "agent_team",
        "team_id": "team-1",
        "sender_agent_id": "agent-1",
        "recipient_agent_ids": ["agent-2"],
        "message_kind": "text",
        "message_count": 1,
    }


def test_retention_prunes_old_rows_without_reusing_cursor(tmp_path):
    stream = RuntimeEventStream(tmp_path / "events.db", retention=100)
    for index in range(105):
        stream.publish({"type": "task_status", "task": {"id": str(index), "status": "running"}})

    replayed = stream.replay(0, limit=200)

    assert replayed[0]["cursor"] == 6
    assert replayed[-1]["cursor"] == 105


def test_replay_pages_continue_from_last_returned_cursor(tmp_path):
    stream = RuntimeEventStream(tmp_path / "events.db", retention=1_000)
    for index in range(12):
        stream.publish({"type": "task_status", "task": {"id": str(index), "status": "running"}})

    first = stream.replay(0, limit=5)
    second = stream.replay(first[-1]["cursor"], limit=5)
    third = stream.replay(second[-1]["cursor"], limit=5)

    assert [event["cursor"] for event in first + second + third] == list(range(1, 13))


def test_replay_vision_preprocess_keeps_only_frontend_safe_fields(tmp_path):
    stream = RuntimeEventStream(tmp_path / "events.db", retention=100)

    event = stream.publish(
        {
            "type": "vision_preprocess",
            "message": (
                "Prepared 1 protected local image(s) with annotated overview and "
                "original-pixel detail tiles."
            ),
            "protected": True,
            "protected_local_images": 1,
            "unprotected_external_images": 0,
            "image_paths": ["/Users/example/dashboard.png"],
            "tile_set_id": "opaque-request-token",
        }
    )

    assert event["replayable"] is True
    replayed = stream.replay(0)
    assert len(replayed) == 1
    payload = replayed[0]
    assert payload["type"] == "vision_preprocess"
    assert payload["message"].startswith("Prepared 1 protected local image(s)")
    assert payload["protected"] is True
    assert payload["protected_local_images"] == 1
    assert payload["unprotected_external_images"] == 0
    assert "image_paths" not in payload
    assert "tile_set_id" not in payload


def test_replay_vision_preprocess_handles_missing_fields():
    safe = safe_replay_event({"type": "vision_preprocess"})
    assert safe == {
        "type": "vision_preprocess",
        "message": "",
        "protected": False,
        "protected_local_images": 0,
        "unprotected_external_images": 0,
    }
