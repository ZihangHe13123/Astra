import assert from "node:assert/strict";
import test from "node:test";
import { initialSession, projectEvent } from "../src/session-state.js";
import { preparationLabel, reduceToolPreparation } from "../src/tool-preparation.js";

const preparing = (id = "attempt") => ({ type: "tool_preparing", attempt_id: id, request_id: "request", state: "preparing",
  calls: [{ index: 0, call_id: "write", name: "write_file", summary: "report.md", argument_chars: 4096 }] });

test("preparation stays outside execution history and stale terminal attempts cannot erase a retry", () => {
  let state = projectEvent(initialSession("a"), preparing());
  assert.equal(state.tools.length, 0);
  assert.match(preparationLabel(state.preparation)!, /准备 write_file.*4,096.*尚未执行/);
  state = projectEvent(state, preparing("retry"));
  state = projectEvent(state, { ...preparing(), state: "discarded", calls: [] });
  assert.equal(state.preparation?.attempt_id, "retry");
  state = projectEvent(state, { ...preparing("retry"), state: "finished", calls: [] });
  assert.equal(state.preparation, undefined);
  assert.equal(state.tools.length, 0);
  state = projectEvent(state, { type: "tool_calls", calls: [{ id: "write", name: "write_file" }] });
  assert.equal(state.tools.length, 1);
  assert.equal(state.tools[0].result_index, undefined);
});

test("disconnect, history and task completion clear progress without persisting replayed previews", () => {
  for (const event of [{ type: "gui_disconnected" }, { type: "history" }, { type: "done", request_id: "request" }]) {
    assert.equal(projectEvent(projectEvent(initialSession("a"), preparing()), event).preparation, undefined);
  }
  assert.equal(reduceToolPreparation(undefined, { ...preparing(), replayed: true }), undefined);
  let state = projectEvent(initialSession("a"), { type: "task_started", task: { id: "request", status: "running" } });
  state = projectEvent(state, preparing());
  assert.ok(projectEvent(state, { type: "done" }).preparation, "unscoped side command does not finish active preparation");
  assert.ok(projectEvent(state, { type: "done", request_id: "other" }).preparation);
});

test("completed step references bind only exact stream or submission IDs, including duplicate text", () => {
  let state = projectEvent(initialSession("a"), { type: "gui_user", submission_id: "user", text: "same" });
  state = projectEvent(state, { type: "chunk", stream_id: "one", content: "same" });
  state = projectEvent(state, { type: "chunk", stream_id: "two", content: "same" });
  assert.equal(state.messages.length, 3);
  const source_ref = { index: 2, digest: "a".repeat(64) };
  state = projectEvent(state, { type: "message_source", stream_id: "two", source_ref });
  assert.deepEqual(state.messages.map(m => m.source_ref), [undefined, undefined, source_ref]);
  state = projectEvent(state, { type: "message_source", submission_id: "user", source_ref });
  assert.deepEqual(state.messages[0].source_ref, source_ref);
  assert.equal(state.messages[1].source_ref, undefined);
});
