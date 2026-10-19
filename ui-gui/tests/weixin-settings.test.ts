import { test } from "node:test";
import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { initialSession, projectEvent } from "@astra/ui-core/session-state";
import { WeixinSettings, weixinModelOptions, weixinView } from "../src/renderer/weixin-settings.js";

const status = (fields: Record<string, unknown> = {}) => ({ type: "weixin_status", enabled: false, logged_in: false, account: "", state: "off",
  owner: false, keep_awake: false, login: "", ...fields });
const svg = "data:image/svg+xml;charset=utf-8,%3Csvg%3E%3C%2Fsvg%3E";
const buttons = (html: string) => [...html.matchAll(/<button[^>]*>([^<]*)<\/button>/g)].map(match => match[1]);
const render = (fields: Record<string, unknown> | undefined, ready = true) =>
  renderToStaticMarkup(React.createElement(WeixinSettings, { status: fields && status(fields), ready, run: () => {} }));

test("the backend's weixin_status reaches the settings through the session state", () => {
  const state = projectEvent(initialSession("fixture"), status({ state: "online", logged_in: true, enabled: true, account: "bot-…" }) as never);
  assert.equal(weixinView(state.info.weixin_status).label, "在线");
});

test("settings offer a login first, show the code while it waits, then switch and log out", () => {
  assert.deepEqual(buttons(render({})), ["扫码登录"]);

  const waiting = render({ login: "scan", qr: { svg, text: "██" } });
  assert.match(waiting, /<img src="data:image\/svg\+xml[^"]*" alt="微信登录二维码"/);
  assert.match(waiting, /用手机微信扫一扫登录/);
  assert.match(waiting, /<button disabled="">扫码登录<\/button>/, "a second login waits for the first");
  assert.match(render({ login: "confirm", qr: { svg, text: "██" } }), /已扫码，请在手机微信里确认/);

  const online = render({ state: "online", enabled: true, logged_in: true, account: "bot-…", owner: true, keep_awake: true });
  assert.match(online, /状态：在线（bot-…） · 已阻止电脑空闲睡眠/);
  assert.deepEqual(buttons(online), ["重新扫码登录", "停用", "退出登录"]);
  assert.deepEqual(buttons(render({ state: "off", logged_in: true, account: "bot-…" })), ["重新扫码登录", "启用", "退出登录"]);
  assert.match(render({ state: "standby", enabled: true, logged_in: true }), /待命：另一个 Astra 会话正在接收/);
  assert.match(render({ state: "expired", enabled: true, logged_in: true, error: "微信登录已失效，请重新扫码登录。" }),
    /role="alert">微信登录已失效/);
});

test("only an SVG data image is drawn as the code, and nothing can be pressed without an open session", () => {
  assert.doesNotMatch(render({ login: "scan", qr: { svg: "https://example.com/qr.png", text: "" } }), /<img/);
  assert.ok(buttons(render({}, false)).length === 1 && /disabled/.test(render({}, false)));
  assert.match(render({}, false), /打开一个会话后可以设置/);
});

test("the model WeChat answers with is chosen from the catalog, following the default until one is picked", () => {
  const models = [{ key: "deepseek::deepseek-flash", name: "deepseek-flash", provider: "DeepSeek" },
    { key: "local::qwen", name: "qwen", provider: "Local" }];
  const view = (fields: Record<string, unknown>) => weixinView(status(fields) as never);

  assert.deepEqual(weixinModelOptions(view({ model: "", model_label: "", default_model: "deepseek-flash" }), models),
    [{ value: "", label: "跟随默认（deepseek-flash）" }, { value: "deepseek::deepseek-flash", label: "deepseek-flash · DeepSeek" },
      { value: "local::qwen", label: "qwen · Local" }]);
  assert.deepEqual(weixinModelOptions(view({ model: "gone::old", model_label: "old" }), models).at(-1),
    { value: "gone::old", label: "old（不在模型列表里）" }, "a saved model that left the catalog stays visible");

  const html = renderToStaticMarkup(React.createElement(WeixinSettings,
    { status: status({ model: "local::qwen", model_label: "qwen", default_model: "deepseek-flash" }), ready: true, models, run: () => {} }));
  assert.match(html, /回复用的模型<select>/);
  assert.match(html, /<option value="local::qwen" selected="">qwen · Local<\/option>/);
  assert.match(renderToStaticMarkup(React.createElement(WeixinSettings, { status: status(), ready: false, models, run: () => {} })),
    /<select disabled="">/);
});
