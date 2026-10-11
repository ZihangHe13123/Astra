import assert from "node:assert/strict";
import { test } from "node:test";
import stringWidth from "string-width";
import { processRowCells, rowCells, toolRowCells, type ToolRowInput } from "./tool-row.js";

const text = (cells: { text: string }[]) => cells.map((cell) => cell.text).join("");
const read: ToolRowInput = { id: 12, name: "read_file", arguments: JSON.stringify({ path: "agent/runtime/turn_change_store.py" }), output: "x\n".repeat(61).trimEnd(), error: "", durationMs: 96 };
const search: ToolRowInput = { id: 13, name: "search_text", arguments: JSON.stringify({ pattern: "os.open(", path: "agent/runtime" }), output: "hit\n".repeat(14).trimEnd(), error: "", durationMs: 312 };
const edit: ToolRowInput = { id: 14, name: "edit_file", arguments: JSON.stringify({ path: "agent/runtime/turn_change_store.py" }), output: "Edited", error: "" };
const run: ToolRowInput = { id: 15, name: "execute_shell", arguments: JSON.stringify({ command: "pytest tests/test_turn_change_store.py tests/test_turn_changes_integration.py -q" }), output: "collected 18 items\n\n18 passed in 4.21s", error: "", durationMs: 4210 };
const failed: ToolRowInput = { id: 16, name: "execute_shell", arguments: JSON.stringify({ command: "pytest tests -q -x" }), output: "…\n1 failed, 212 passed", error: "Command exited with code 1\nsecond line", durationMs: 98_000 };

test("a call is one row: what was done, to what, what came of it, how long, how it ended, its number", () => {
  // Between the target and what stands on the right runs a faint line of dots.
  assert.match(text(toolRowCells(read, 82)[0]), /^◆ READ     agent\/runtime\/turn_change_store\.py ·{6,} 61 lines  0\.1s ✓ #12$/);
  assert.match(text(toolRowCells(search, 82)[0]), /^◆ SEARCH   os\.open\(  in agent\/runtime ·{6,} 14 lines  0\.3s ✓ #13$/);
  assert.equal(toolRowCells(read, 82)[0].find((cell) => cell.text.includes("···"))?.tone, "subtle");
  // A change says what it changed; the turn's own report says by how much.
  assert.match(text(toolRowCells(edit, 82)[0]), /^◆ EDIT     agent\/runtime\/turn_change_store\.py ·{6,}  ✓ #14$/);
  // A run ends on its own summary line, and a long command gives way to it: with no room
  // for a line of dots there is none.
  assert.match(text(toolRowCells(run, 82)[0]), /^◆ RUN      pytest tests\/test_turn_change_store\.py…  18 passed in 4\.21s  4\.2s ✓ #15$/);
  assert.equal(toolRowCells(read, 82).length, 1);
});

test("every row of one width is that wide, with its columns in the same place", () => {
  for (const width of [140, 104, 82, 60, 44, 36, 30, 20]) {
    const rows = [read, search, edit, run, failed].map((tool) => toolRowCells(tool, width)[0]);
    for (const row of rows) assert.equal(stringWidth(text(row)), width, `${width}: ${text(row)}`);
    for (const mark of [/[✓✕]/u, /#\d+$/u]) {
      assert.equal(new Set(rows.map((row) => stringWidth(text(row).slice(0, text(row).search(mark))))).size, 1, `${width}: ${mark}`);
    }
  }
  // Wide characters are measured as the terminal draws them.
  const wide = toolRowCells({ ...read, arguments: JSON.stringify({ path: "文档/很长很长很长很长很长很长很长很长很长很长的中文文件名.md" }) }, 44);
  assert.equal(stringWidth(text(wide[0])), 44);
  assert.match(text(wide[0]), /…/);
});

test("a call's time is told in the unit that says something", () => {
  const timed = (durationMs: number | undefined) => text(toolRowCells({ ...edit, durationMs }, 82)[0]).match(/· (\S*) ✓ #14$/)![1];
  // Without a result the time stands right after the dots.
  assert.deepEqual([timed(0), timed(41), timed(94), timed(96), timed(4210), timed(98_000)], ["0ms", "41ms", "94ms", "0.1s", "4.2s", "1:38"]);
  assert.match(text(toolRowCells({ ...edit, durationMs: undefined }, 82)[0]), /store\.py ·+  ✓ #14$/);
});

test("a narrow terminal drops the result before the time, and keeps how the call ended", () => {
  assert.match(text(toolRowCells(run, 60)[0]), /18 passed in 4\.21s/);
  assert.doesNotMatch(text(toolRowCells(run, 44)[0]), /passed/);
  assert.match(text(toolRowCells(run, 44)[0]), /4\.2s ✓ #15$/);
  assert.doesNotMatch(text(toolRowCells(run, 30)[0]), /4\.2s/);
  assert.match(text(toolRowCells(run, 30)[0]), /^◆ RUN .* ✓ #15$/);
});

test("a failed call is marked, and says why on the row below", () => {
  const [row, reason, ...rest] = toolRowCells(failed, 82);
  assert.equal(rest.length, 0);
  assert.match(text(row), /^◆ RUN      pytest tests -q -x ·{6,} 1 failed, 212 passed  1:38 ✕ #16$/);
  assert.equal(row.find((cell) => cell.text.includes("✕"))?.tone, "danger");
  assert.equal(row[0].tone, "danger");
  assert.equal(text(reason), "  Command exited with code 1");
  assert.equal(reason[1].tone, "danger");
  // A long reason is cut to the row, never wrapped.
  const long = toolRowCells({ ...failed, error: "x".repeat(300) }, 40);
  assert.equal(stringWidth(text(long[1])), 40);
  // With nothing said about the failure, the row still says it failed.
  assert.equal(text(toolRowCells({ ...failed, error: " ", output: "" }, 82).at(-1)!), "  failed");
});

test("a note stands under its row, before any reason for failing", () => {
  const noted = toolRowCells({ ...read, note: "文档 notes.md · 写入「导语」\nsecond line" }, 82);
  assert.equal(noted.length, 2);
  assert.equal(text(noted[1]), "  文档 notes.md · 写入「导语」");
  assert.equal(noted[1][1].tone, "muted");
  const both = toolRowCells({ ...failed, note: "找到 3 张图片 · /gallery 16 打开图库" }, 82);
  assert.deepEqual(both.slice(1).map((cells) => cells[1].tone), ["muted", "danger"]);
});

test("a call whose arguments cannot be read is still named", () => {
  const row = text(toolRowCells({ id: 3, name: "mystery_tool", arguments: "{not json", output: "", error: "" }, 60)[0]);
  assert.match(row, /^◆ TOOL     mystery_tool ·{6,}  ✓  #3$/);
  assert.match(text(toolRowCells({ id: 1234, name: "read_file", output: "", error: "" }, 60)[0]), /✓ #1234$/);
});

test("the dots bridge a gap worth bridging, and nothing shorter", () => {
  const row = (target: string) => text(rowCells({ verb: "RUN", target, time: "1.0s", id: 1 }, 60));
  assert.match(row("short"), /^◆ RUN      short ·{20,} 1\.0s ✓  #1$/);
  // Five cells or fewer between the target and the time stay empty.
  const tight = row("x".repeat(60 - 11 - 6 - 2 - 4 - 3));
  assert.doesNotMatch(tight, /·/);
  assert.equal(stringWidth(tight), 60);
  for (const width of [140, 82, 60, 44, 30, 20]) {
    for (const input of [{ verb: "RUN", target: "a" }, { verb: "READ", target: "b", result: "9 lines", time: "12ms", id: 7 }, { verb: "EDIT", target: "c".repeat(200), failed: true, id: 12345 }]) {
      assert.equal(stringWidth(text(rowCells(input, width))), width, `${width}: ${text(rowCells(input, width))}`);
    }
  }
});

test("a background process that ended is a row like a call's, without a number to open", () => {
  const done = text(processRowCells({ kind: "shell", label: "npm run build\nsecond line", status: "completed", durationMs: 34_533, outputChars: 336_743, processId: "2540283a99ff" }, 100));
  assert.match(done, /^◆ RUN      background shell · npm run build ·{6,} 336,743 chars   35s ✓ {4}$/);
  assert.equal(stringWidth(done), 100);
  // One that did not complete says how it ended where the size would be, and is marked.
  const stopped = processRowCells({ kind: "subagent", status: "timed_out", durationMs: 600_000, outputChars: 12, processId: "2540283a99ff" }, 100);
  assert.match(text(stopped), /^◆ RUN      background subagent · 2540283a ·{6,} timed out +10:00 ✕ {4}$/);
  assert.equal(stopped[0].tone, "danger");
});
