import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { RequestMetricsCard } from "../src/renderer/request-timeline.js";

test("request inspector separates measured usage from estimates and missing values", () => {
  const html = renderToStaticMarkup(React.createElement(RequestMetricsCard, { record: {
    request_id: "r1", step: 2, model: "fixture", total_ms: 1200, pre_request_ms: 100,
    usage: { prompt_tokens: 0 }, fingerprint: { request_tokens_estimate: 800 }, phases_ms: {},
  } }));
  assert.match(html, /1\.20 秒/);
  assert.match(html, /输入 token/);
  assert.match(html, /客户端估算/);
  assert.match(html, /未采集/);
  assert.doesNotMatch(html, /NaN|Infinity/);
});

test("request inspector renders recorded phases and cache decrease without prompt text", () => {
  const html = renderToStaticMarkup(React.createElement(RequestMetricsCard, { record: {
    request_id: "r2", step: 1, model: "fixture", failed: true,
    usage: { prompt_cache_hit_tokens: 10, prompt_cache_miss_tokens: 90 },
    fingerprint: {}, phases_ms: { memory_prepare: 15 }, cache: { status: "material_drop", causes: ["tool_schemas"] },
  } }));
  assert.match(html, /缓存命中下降/);
  assert.match(html, /工具定义变化/);
  assert.match(html, /memory_prepare/);
  assert.match(html, /失败/);
});
