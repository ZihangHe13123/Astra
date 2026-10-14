"""Real backend protocol: retained members must not turn new input into steering."""

import json
import sqlite3
import threading
import time

from agent.runtime.agent_team import AgentTeamStore
from agent.runtime.task_store import TaskStore
from test_backend_task_protocol import (
    ThreadingHTTPServer,
    _SlowOpenAIHandler,
    _start_question_protocol_backend,
    _stop_protocol_backend,
)


class RetainedMemberProvider(_SlowOpenAIHandler):
    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        server = self.server
        message = {"role": "assistant", "content": ""}
        if not payload.get("stream"):
            server.member_requests.append(payload["messages"])
            if len(server.member_requests) == 2:
                # The second assignment outlasts the parent turn that gave it.
                time.sleep(1.5)
                server.second_report_sent = True
            message["content"] = f"retained report {len(server.member_requests)}"
        else:
            server.parent_step += 1
            turn, step = server.parent_turn, server.parent_step
            tool, args = "", {}
            if turn == 1 and step == 1:
                tool, args = "team", {"action": "create", "name": "retained", "goal": "two parent turns"}
            elif step <= 2:
                with sqlite3.connect(server.task_db) as db:
                    team_id = db.execute("SELECT id FROM agent_teams LIMIT 1").fetchone()[0]
                if turn == 1:
                    tool, args = "team_spawn", {
                        "team_id": team_id, "name": "member", "goal": "retained member fixture",
                        "keep_alive": True, "timeout": 30,
                    }
                elif step == 1:
                    tool, args = "team", {"action": "status", "team_id": team_id}
                else:
                    tool, args = "team_send", {
                        "team_id": team_id, "to": "member", "message": "second retained assignment",
                        "kind": "task_assignment",
                    }
            if tool:
                message["tool_calls"] = [{"id": f"fixture-{turn}-{step}", "type": "function",
                                          "function": {"name": tool, "arguments": json.dumps(args)}}]
            else:
                message["content"] = f"parent turn {turn} finished"
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        response = {"id": "retained-fixture", "object": "chat.completion", "created": int(time.time()),
                    "model": "Qwen3.6-35B-A3B", "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
        if not payload.get("stream"):
            self._json(response)
            return
        if message.get("tool_calls"):
            message["tool_calls"][0]["index"] = 0
        response["object"] = "chat.completion.chunk"
        response["choices"] = [{"index": 0, "delta": message, "finish_reason": finish}]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(("data: " + json.dumps(response) + "\n\ndata: [DONE]\n\n").encode())
        self.wfile.flush()


def test_backend_completes_turn_and_reuses_retained_member_on_next_input(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), RetainedMemberProvider)
    server.task_db = str(tmp_path / "tasks.db")
    server.member_requests = []
    server.parent_turn, server.parent_step = 1, 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    proc, _, seen, wait = _start_question_protocol_backend(
        tmp_path, server.server_port, "retained-backend", workdir=workspace,
        # The fixture is on this computer, where parent and member share its request
        # slots; with one, the parent's last request would wait out the member's.
        env_overrides={"LLM_MAX_CONCURRENT_REQUESTS": "2"},
    )
    try:
        wait(lambda event: event.get("type") == "model_info")
        proc.stdin.write(json.dumps({"type": "message", "text": "first parent turn"}) + "\n")
        proc.stdin.flush()
        wait(lambda event: event.get("type") == "done", timeout=8)
        assert not [e for e in seen if e.get("type") == "error"], seen[-8:]
        assert "parent turn 1 finished" in "".join(e.get("content", "") for e in seen if e.get("type") == "chunk")
        first_spawn = next(e for e in seen if e.get("type") == "tool_result" and e.get("name") == "team_spawn")
        assert not first_spawn.get("error")
        with sqlite3.connect(server.task_db) as db:
            team_id, owner_id = db.execute("SELECT id, owner_task_id FROM agent_teams").fetchone()
            member_id, process_id = db.execute("SELECT id, process_id FROM team_agents WHERE name='member'").fetchone()
        teams = AgentTeamStore(server.task_db)
        assert teams.get_agent(member_id)["status"] == "idle"
        server.parent_turn, server.parent_step = 2, 0
        boundary = len(seen)
        proc.stdin.write(json.dumps({"type": "message", "text": "next parent turn"}) + "\n")
        proc.stdin.flush()
        wait(lambda event: event.get("type") == "done", timeout=8)
        assert not [e for e in seen[boundary:] if e.get("type") == "error"], seen[-8:]
        assert "parent turn 2 finished" in "".join(e.get("content", "") for e in seen[boundary:] if e.get("type") == "chunk")
        assert not any(e.get("type") == "steering" for e in seen[boundary:])
        assert teams.get_team(team_id)["owner_task_id"] != owner_id
        assert teams.get_agent(member_id)["process_id"] == process_id
        # The parent answered without waiting; the member's report then starts a turn.
        assert not getattr(server, "second_report_sent", False)
        wake = wait(lambda event: event.get("type") == "background_wakeup", timeout=8)
        assert wake["state"] == "started"
        wait(lambda event: event.get("type") == "done", timeout=8)
        assert teams.get_agent(member_id)["status"] == "idle"
        assert len(server.member_requests) == 2
        assert "retained report 1" in str(server.member_requests[1])
        assert "second retained assignment" in str(server.member_requests[1])
        assert TaskStore(server.task_db).get_task(owner_id)["status"] == "completed"
    finally:
        _stop_protocol_backend(proc)
        server.shutdown()
        server.server_close()
