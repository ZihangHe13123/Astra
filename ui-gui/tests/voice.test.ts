import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { initialSession, projectEvent } from "@astra/ui-core/session-state";
import { describeCommands, suggestCommands } from "../src/renderer/command-help.js";
import { opensCommandInterface } from "../src/renderer/control-state.js";
import { VoiceSettings } from "../src/renderer/voice.js";
import { voiceAnswer, voiceButton, voiceNotice, voiceUseCommand, withVoiceOptions } from "../src/renderer/voice-state.js";
import type { CommandDescription } from "../src/bridge.js";

const voices = ["温暖搭档", "Night Owl"];
const status = (fields: Record<string, unknown> = {}) => ({ type: "voice_status", enabled: true, configured: true, state: "idle", voice: "温暖搭档", voices, error: "", ...fields });
const catalog = JSON.parse(readFileSync(new URL("../../agent/ui/commands.json", import.meta.url), "utf8")) as CommandDescription[];

test("the composer shows voice only where a speech endpoint exists, and names the voice while on", () => {
  assert.equal(voiceButton(undefined), undefined);
  assert.equal(voiceButton(status({ enabled: false, configured: false, state: "off", voice: "", voices: [] })), undefined);
  assert.deepEqual(voiceButton(status({ enabled: false, state: "off" })), { label: "朗读已关闭", on: false });
  assert.deepEqual(voiceButton(status()), { label: "朗读 · 温暖搭档", on: true });
  assert.deepEqual(voiceButton(status({ voice: "", voices: [] })), { label: "朗读已开启", on: true });
  // Switched on before any endpoint was set: the control stays, so the user can reach the settings.
  assert.equal(voiceButton(status({ configured: false, voice: "", voices: [] }))?.on, true);
});

test("the line above the composer follows speech, and reports only failures no command answered", () => {
  assert.equal(voiceNotice(undefined), undefined);
  assert.equal(voiceNotice(status()), undefined);
  assert.deepEqual(voiceNotice(status({ state: "speaking" })), { text: "正在朗读回复。", action: "stop" });
  assert.equal(voiceNotice(status({ state: "starting" }))?.action, "stop");
  // A test line can be spoken while replies are not read aloud.
  assert.equal(voiceNotice(status({ enabled: false, state: "speaking" }))?.action, "stop");
  assert.deepEqual(voiceNotice(status({ error: "Speech endpoint is unreachable: ConnectError" })),
    { text: "朗读没有成功：Speech endpoint is unreachable: ConnectError", action: "settings" });
  assert.equal(voiceNotice(status({ enabled: false, state: "off", error: "Speech endpoint is unreachable: ConnectError" }))?.action, "settings");
  assert.equal(voiceNotice(status({ configured: false, error: "Voice is not set up", message: "Voice is on." }))?.text, "朗读已开启，但还没有配置语音服务。");
  // The failed command was answered when it was given; it says nothing about speech since.
  assert.equal(voiceNotice(status({ error: "Unknown voice: nope.", message: "" })), undefined);
  assert.equal(voiceNotice(status({ enabled: false, configured: false, state: "off", error: "" })), undefined);
});

test("a typed command is answered from the state the backend reports back", () => {
  assert.equal(voiceAnswer("on", status({ message: "Voice is on · 温暖搭档." })), "朗读已开启 · 温暖搭档");
  assert.equal(voiceAnswer("off", status({ enabled: false, state: "off", message: "Voice is off." })), "朗读已关闭");
  assert.equal(voiceAnswer("use", status({ voice: "Night Owl", message: "Voice set to Night Owl." })), "音色已切换为 Night Owl");
  assert.equal(voiceAnswer("test", status({ enabled: false, state: "off", message: "Speaking a test line." })), "正在试听当前音色");
  assert.equal(voiceAnswer("stop", status({ message: "Stopped speaking." })), "已停止朗读");
  // Not carried out: the backend's reason is the answer, whatever the state still says.
  assert.equal(voiceAnswer("use", status({ message: "", error: "Unknown voice: nope. Voices: 温暖搭档, Night Owl" })), "朗读：Unknown voice: nope. Voices: 温暖搭档, Night Owl");
  assert.equal(voiceAnswer("stop", status({ message: "", error: "Usage: /voice [on|off|stop|list|use <voice>|test [text]]" })).startsWith("朗读：Usage"), true);
});

test("a voice name reaches the backend as one argument", () => {
  assert.equal(voiceUseCommand("温暖搭档"), "/voice use 温暖搭档");
  assert.equal(voiceUseCommand("Night Owl"), '/voice use "Night Owl"');
  assert.equal(voiceUseCommand(`Dad's "big" \\ voice`), `/voice use "Dad's \\"big\\" \\\\ voice"`);
});

test("slash help lists the defined voices beside the registered actions, in the desktop's language", () => {
  const raw = catalog.find(command => command.command === "/voice")!;
  const commands = describeCommands(catalog.map(command => command === raw ? withVoiceOptions(command, status()) : command), []);
  const items = suggestCommands(commands, "/voice ", [], "work");
  assert.deepEqual(items.map(item => item.value), [...raw.options.map(option => option.completion.trim()), "/voice use 温暖搭档", '/voice use "Night Owl"']);
  assert.deepEqual(items.find(item => item.value === "/voice on"), { value: "/voice on", label: "开启朗读", description: "回复生成时自动朗读", more: false, hint: undefined });
  assert.equal(items.find(item => item.value === "/voice use 温暖搭档")?.description, "当前音色");
  assert.equal(items.find(item => item.value === '/voice use "Night Owl"')?.label, "Night Owl");
  assert.deepEqual(suggestCommands(commands, "/voice use ", [], "work").map(item => item.label), voices);
  assert.equal(withVoiceOptions(raw, undefined).options.length, raw.options.length);
  assert.equal(opensCommandInterface("/voice"), true);
});

test("the settings panel shows what the backend last reported", () => {
  // The same reducer the desktop runs: the latest status replaces the one before it.
  let state = projectEvent(initialSession("voice"), status({ enabled: false, state: "off" }));
  const render = () => renderToStaticMarkup(React.createElement(VoiceSettings, { status: state.info.voice_status, ready: true, run: () => {}, close: () => {} }));
  const pressed = (html: string) => [...html.matchAll(/<button[^>]*aria-pressed="true"[^>]*><strong>([^<]*)<\/strong>/g)].map(match => match[1]);
  assert.deepEqual(pressed(render()), ["关闭", "温暖搭档"]);
  assert.doesNotMatch(render(), /停止朗读|role="alert"|role="note"/);

  state = projectEvent(state, status({ voice: "Night Owl", state: "speaking" }));
  assert.deepEqual(pressed(render()), ["开启", "Night Owl"]);
  assert.match(render(), /停止朗读/);

  state = projectEvent(state, status({ error: "Speech endpoint is unreachable: ConnectError" }));
  assert.match(render(), /role="alert">Speech endpoint is unreachable: ConnectError</);
  assert.doesNotMatch(render(), /停止朗读/);

  const blank = renderToStaticMarkup(React.createElement(VoiceSettings, { status: status({ enabled: false, configured: false, state: "off", voice: "", voices: [] }), ready: true, run: () => {}, close: () => {} }));
  assert.match(blank, /role="note"/);
  assert.match(blank, /<button disabled="">试听当前音色<\/button>/);
  assert.deepEqual(pressed(blank), ["关闭"]);
});
