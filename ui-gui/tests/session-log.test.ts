import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { SessionLogCard, logTargetNotice, recordChatSource, sameSource, supportsSessionLog, type SessionLogRecord } from "../src/renderer/session-log.js";
import { ToolPreparation } from "../src/renderer/tool-preparation.js";

const source = { index: 7, digest: "a".repeat(64) };
const record: SessionLogRecord = { source_ref: source, role: "tool", raw: '<script>alert("x")</script>', raw_truncated: true, raw_bytes: 90000, tool_call_ids: ["call-1"], chat_position: null };
test("log cards render raw records as escaped text, label truncation and refuse unproven chat positions", () => {
  const html = renderToStaticMarkup(React.createElement(SessionLogCard, { record, selected: true, select() {}, locate() {} }));
  assert.match(html, /&lt;script&gt;/); assert.doesNotMatch(html, /<script>/);
  assert.match(html, /disabled=""/); assert.match(html, /16 KiB/); assert.match(html, /没有对应的可见聊天消息/);
  assert.equal(recordChatSource(record), undefined);
});
test("tool-result navigation uses the proven call source rather than the result index", () => {
  const call = { index: 6, digest: "b".repeat(64) };
  assert.deepEqual(recordChatSource({ ...record, chat_position: 3, chat_source_ref: call }), call);
  assert.deepEqual(recordChatSource({ ...record, chat_position: 0 }), source);
  assert.equal(sameSource(source, { ...source, digest: "b".repeat(64) }), false);
  assert.equal(sameSource(source, { ...source, index: 8 }), false);
  assert.equal(sameSource(source, { ...source }), true);
});
test("stale, missing and ambiguous record navigation have distinct explanations", () => {
  assert.match(logTargetNotice("stale"), /变化或被压缩/);
  assert.match(logTargetNotice("missing"), /尚未保存/);
  assert.match(logTargetNotice("ambiguous"), /无法唯一定位/);
  assert.equal(logTargetNotice("found"), "");
});
test("preparing tool arguments are visibly unexecuted and never acquire a result index", () => {
  const html = renderToStaticMarkup(React.createElement(ToolPreparation, { preparation: { attempt_id: "try-2", state: "preparing", calls: [{ index: 0, call_id: "call-1", name: "write_file", argument_chars: 65536, summary: "/tmp/报告.md" }] } }));
  assert.match(html, /尚未执行/); assert.match(html, /65,536 字符/); assert.match(html, /报告.md/);
  assert.doesNotMatch(html, /data-result-index|tool-detail|工具结果/);
  assert.equal(renderToStaticMarkup(React.createElement(ToolPreparation, { preparation: undefined })), "");
});

test("public inspection excludes installation-local and unknown session namespaces", () => {
  for (const mode of ["work", "bar", "minimal"]) assert.equal(supportsSessionLog(mode), true);
  for (const mode of ["local", "writing", "", "unknown"]) assert.equal(supportsSessionLog(mode), false);
});
