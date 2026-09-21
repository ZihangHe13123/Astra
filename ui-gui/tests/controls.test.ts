import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { initialSession, projectEvent, type SessionState } from "@astra/ui-core/session-state";
import { ModelPicker, ModelSettings } from "../src/renderer/controls.js";
import { connectionFlow, filterCommands, moveSelection, opensCommandInterface } from "../src/renderer/control-state.js";
import type { CommandDescription } from "../src/bridge.js";

const commands = ["/model", "/session", "/reset"].map(command => ({ id: command, command, description: "", options: [], takes_args: true, group: "chat" })) as CommandDescription[];
const names = { model: "选择模型", session: "会话管理", reset: "清空对话" };
const send = async () => {};
const close = () => {};
function ready(): SessionState {
  return { ...initialSession("test"), status: "ready", info: { model_info: { type: "model_info", model_key: "ready::one",
    providers: [{ id: "ready", connected: true }, { id: "codex", connected: false }, { id: "vendor", connected: false }],
    models: [{ key: "ready::one", name: "One", provider_id: "ready", provider: "Ready" }, { key: "codex::gpt", name: "GPT", provider_id: "codex", provider: "ChatGPT" },
      { key: "vendor::plan", name: "Plan", provider_id: "vendor", provider: "Vendor", endpoint: "https://plan.example/v1" }],
    connection_routes: [{ id: "codex", provider: "ChatGPT", auth_mode: "oauth", label: "Subscription" },
      { id: "vendor", provider: "Vendor", base_url: "https://api.example/v1", label: "API" },
      { id: "plan", provider: "Vendor", base_url: "https://plan.example/v1", label: "Plan" }],
  } } };
}
const renderSettings = (state: SessionState, initialModelKey?: string) => renderToStaticMarkup(React.createElement(ModelSettings, { state, send, close, initialModelKey }));

test("palette supports translated search, typed arguments and bounded keyboard selection", () => {
  assert.equal(filterCommands(commands, "模型", names)[0]?.command, "/model");
  assert.equal(filterCommands(commands, "/session saved-session", names)[0]?.command, "/session");
  assert.equal(filterCommands(commands, "absent", names).length, 0);
  assert.equal(moveSelection(0, 3, -1), 2); assert.equal(moveSelection(2, 3, 1), 0); assert.equal(moveSelection(0, 0, 1), 0);
  assert.equal(opensCommandInterface("/model"), true); assert.equal(opensCommandInterface("/session"), true);
  assert.equal(opensCommandInterface("/reset"), false);
});

test("reopening model settings restores the pending device challenge and cancellation", () => {
  let state = projectEvent(ready(), { type: "connection_pending", request_id: "device", route_id: "codex" });
  let html = renderSettings(state);
  assert.match(html, /正在连接/); assert.match(html, /取消连接/);
  state = projectEvent(state, { type: "connection_auth", request_id: "device", user_code: "TEST-CODE", verification_uri: "https://auth.example/device" });
  html = renderSettings(state); // a fresh component instance, as on closing and reopening the panel
  assert.match(html, /TEST-CODE/); assert.match(html, /取消登录/);
  assert.match(html, /value="codex" selected=""/);
  assert.equal(connectionFlow(state.info).requestId, "device");
  state = projectEvent(state, { type: "connection_result", request_id: "device", notice: "Signed in" });
  html = renderSettings(state);
  assert.doesNotMatch(html, /TEST-CODE|取消登录/); assert.match(html, /Signed in/);
  assert.equal(connectionFlow(state.info).connecting, false);
});

test("picker configuration opens the model's exact billing route and preserves advanced inputs", () => {
  let state = projectEvent(ready(), {type:"connection_pending",request_id:"old",route_id:"codex"});
  state = projectEvent(state, {type:"connection_result",request_id:"old",notice:"Signed in"});
  const html = renderSettings(state, "vendor::plan");
  assert.match(html, /value="plan" selected=""/);
  assert.match(html, /value="https:\/\/plan\.example\/v1"/);
  assert.match(html, /API 地址/); assert.match(html, /API Key/); assert.match(html, /环境变量/); assert.match(html, /精确模型 ID/);
});

test("compact model picker presents configuration access alongside known models", () => {
  const html = renderToStaticMarkup(React.createElement(ModelPicker, { state: ready(), send, close, onConfigure() {} }));
  assert.match(html, /aria-label="搜索模型"/); assert.match(html, /未连接 · 点击配置/); assert.match(html, /模型与账号/);
  assert.doesNotMatch(html, /API Key/);
});
