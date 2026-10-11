import assert from "node:assert/strict";
import test from "node:test";
import { describeTool, span } from "../src/tool-activity.js";

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
