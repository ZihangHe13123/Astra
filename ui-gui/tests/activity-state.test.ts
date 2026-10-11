import assert from "node:assert/strict";
import test from "node:test";
import { withActivity } from "../src/renderer/activity-state.js";

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
