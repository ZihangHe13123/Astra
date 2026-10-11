import assert from "node:assert/strict";
import test from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { toolInput, withActivity } from "../src/renderer/activity-state.js";
import { StepDetail } from "../src/renderer/activity.js";

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

test("a call's arguments are laid out to be read, whatever shape they came in", () => {
  // One argument to a line; the backend sends them as JSON text.
  assert.equal(toolInput({ arguments: JSON.stringify({ path: "notes/a.md", limit: 40, follow: true, encoding: null }) }),
    "path: notes/a.md\nlimit: 40\nfollow: true\nencoding: null");
  // Text of several lines is a block with its line breaks as they are, not one line of "\\n".
  assert.equal(toolInput({ arguments: { command: "python3 - <<'PY'\r\nimport json\n\nprint(1)\nPY", timeout: 60 } }),
    "command:\n  python3 - <<'PY'\n  import json\n  \n  print(1)\n  PY\ntimeout: 60");
  // A small structure stays on its line; a large one is opened out under its name.
  assert.equal(toolInput({ arguments: { range: [1, 20], options: { strict: true } } }), 'range: [1,20]\noptions: {"strict":true}');
  const large = { edits: Array.from({ length: 6 }, (_, index) => ({ old: `before ${index}`, new: `after ${index}` })) };
  const shown = toolInput({ arguments: large });
  assert.match(shown, /^edits:\n  \[\n    \{\n      "old": "before 0",/);
  assert.equal(shown.split("\n").every((line, index) => index === 0 || line.startsWith("  ")), true);
  // Arguments that are not an object, or cannot be parsed, are shown as they came.
  assert.equal(toolInput({ arguments: '{"path": "cut off' }), '{"path": "cut off');
  assert.equal(toolInput({ arguments: '"just text"' }), "just text");
  assert.equal(toolInput({ arguments: "[1, 2]" }), "[\n  1,\n  2\n]");
  assert.equal(toolInput({ args: { query: "older field name" } }), "query: older field name");
  // No arguments is nothing to show.
  for (const tool of [{}, { arguments: "" }, { arguments: "  " }, { arguments: "{}" }, { arguments: {} }, { arguments: null }]) assert.equal(toolInput(tool), "");
});

test("an opened row shows what the call was asked and what came back", () => {
  const html = (tool: object, openDetails?: () => void) => renderToStaticMarkup(React.createElement(StepDetail, { tool, openDetails }));
  const done = html({ name: "execute_shell", arguments: JSON.stringify({ command: "pytest -q\ncat <report.txt>" }), output: "3 passed", status: "completed" }, () => {});
  // The arguments stand above the output, each under its own label, and markup in them is text.
  assert.match(done, /参数<\/h4><pre class="step-input">command:\n  pytest -q\n  cat &lt;report\.txt&gt;<\/pre>.*输出<\/h4>.*<pre class="step-output">3 passed<\/pre>/s);
  assert.match(done, /在执行详情中查看/);
  // Without a panel to go to there is no link; without arguments there is no empty block.
  const bare = html({ name: "context_open", output: "one", status: "completed" });
  assert.doesNotMatch(bare, /参数|在执行详情中查看/);
  assert.match(bare, /输出<\/h4><pre class="step-output">one<\/pre>/);
  // A failed call says why under the same label, and a call still running shows what it was asked.
  assert.match(html({ name: "edit_file", arguments: { path: "a.py" }, error: "old text not found", status: "failed" }), /参数.*path: a\.py.*输出<\/h4><p class="error-text">old text not found<\/p>/s);
  const running = html({ name: "execute_shell", arguments: { command: "sleep 60" }, status: "running", type: "tool_calls" });
  assert.match(running, /step-input">command: sleep 60<\/pre>.*输出<\/h4><pre class="step-output">等待结果…<\/pre>/s);
  // Very long arguments are cut here, and say where the rest is.
  const long = html({ name: "write_file", arguments: { path: "a.txt", content: "x".repeat(9000) }, output: "ok", status: "completed" });
  assert.match(long, /参数较长，这里只显示开头；完整内容见执行详情。/);
  assert.ok(long.length < 6000, `the row holds ${long.length} characters`);
});
