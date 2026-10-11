import assert from "node:assert/strict";
import test from "node:test";
import { describeTool, span, withActivity } from "../src/renderer/activity-state.js";

test("a tool call reads as what was done, to what, and what came of it", () => {
  assert.deepEqual(describeTool({ type: "tool_result", name: "read_file", status: "completed", arguments: JSON.stringify({ path: "agent/runtime/turn_change_store.py" }), output: "a\nb\nc" }),
    { verb: "READ", target: "agent/runtime/turn_change_store.py", result: "3 lines", tone: "ok" });
  assert.deepEqual(describeTool({ type: "tool_result", name: "execute_shell", status: "completed", arguments: { command: "pytest tests/test_x.py" }, output: "....\n158 passed in 4.2s\n" }),
    { verb: "RUN", target: "pytest tests/test_x.py", result: "158 passed in 4.2s", tone: "ok" });
  assert.deepEqual(describeTool({ type: "tool_calls", name: "execute_python", status: "running", arguments: { code: "\nimport time\ntime.sleep(5)" }, progress: { stage: "running", percent: 40 } }),
    { verb: "RUN", target: "import time", result: "running · 40%", tone: "live" });
  assert.deepEqual(describeTool({ type: "tool_result", name: "edit_file", arguments: { path: "a.py" }, output: "", error: "old text not found\nRecovery: read the file" }),
    { verb: "EDIT", target: "a.py", result: "old text not found", tone: "bad" });
  assert.equal(describeTool({ name: "search_text", status: "completed", arguments: { path: "agent/runtime", pattern: "os.open(" }, output: "a\nb" }).target, "os.open(  in agent/runtime");
  assert.equal(describeTool({ name: "apply_patch", status: "completed", arguments: { patch: "*** Begin Patch\n*** Update File: a.py\n@@\n*** Add File: b.py\n+x\n*** End Patch" } }).target, "a.py +1");
  // A tool this list does not know keeps its own name; an MCP tool names its server and tool.
  assert.deepEqual(describeTool({ name: "current_time", status: "completed", output: "12:00" }), { verb: "TOOL", target: "current_time", result: "", tone: "ok" });
  assert.equal(describeTool({ name: "mcp__demo__lookup", status: "completed", arguments: { query: "x" } }).target, "demo · lookup  x");
  assert.equal(describeTool({ name: "search_web", status: "completed", arguments: "not json" }).target, "search_web");
});

test("durations read as a person would say them", () => {
  assert.deepEqual([0.1, 4.23, 12.4, 65, 3723, -1].map(span), ["0.1s", "4.2s", "12s", "1:05", "1:02:03", ""]);
});

test("work appears after the message it followed, and nowhere when it has no place", () => {
  const rows = [{ id: "u1", role: "user", content: "改" }, { id: "a1", role: "assistant", content: "好" }, { id: "u2", role: "user", content: "再改" }];
  const tools = [
    { call_id: "c1", name: "read_file", after_message: "u1" }, { call_id: "c2", name: "edit_file", after_message: "u1" },
    { call_id: "c3", name: "execute_shell", after_message: "a1" },
    { call_id: "old", name: "read_file", historical: true }, { call_id: "gone", name: "read_file", after_message: "not-here" },
  ];
  const changes = [{ session_id: "s", request_id: "r1", turn_seq: 1, files: [], unknown_count: 0, totals: {}, after_message: "a1" }];
  const { rows: placed, unplaced } = withActivity(rows, tools, changes);
  assert.deepEqual(placed.map(row => row.id), ["u1", "activity:u1", "a1", "activity:a1", "changes:r1:1", "u2"]);
  assert.deepEqual(placed[1].tools?.map(tool => tool.call_id), ["c1", "c2"]);
  assert.deepEqual(unplaced.map(tool => tool.call_id), ["old", "gone"]);
  // Nothing to place: the same rows come back, so a list without tools is not rebuilt.
  assert.equal(withActivity(rows).rows, rows);
  // Work done before any message was shown goes first.
  assert.deepEqual(withActivity(rows, [{ call_id: "first", name: "read_file", after_message: "" }]).rows.map(row => row.id).slice(0, 2), ["activity:", "u1"]);
});
