import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { commandUsage, describeCommands, modeSessionTarget, suggestCommands, type CommandMode } from "../src/renderer/command-help.js";
import type { CommandDescription } from "../src/bridge.js";

const modes: CommandMode[] = [
  { mode: "bar", command: "/bar", label: "酒吧", description: "独立酒吧场景" },
  { mode: "minimal", command: "/minimal", label: "极简", description: "独立轻量对话" },
];
const raw = JSON.parse(readFileSync(new URL("../../agent/ui/commands.json", import.meta.url), "utf8")) as CommandDescription[];
const catalog = describeCommands(raw, modes);
const suggest = (text: string, mode = "work") => suggestCommands(catalog, text, modes, mode);

test("slash help searches registered roots by spelling or Chinese without treating prose as commands", () => {
  assert.ok(suggest("/").length >= raw.length);
  assert.equal(suggest("/模型").some(item => item.value === "/model"), true);
  assert.equal(suggest("/min")[0]?.value, "/minimal");
  assert.equal(suggest("ordinary /bar").length, 0);
  assert.equal(suggest("/bar\nmultiline").length, 0);
  assert.equal(suggest("/bar\rmultiline").length, 0);
  assert.equal(suggest("/unknown").length, 0);
});

test("mode help exposes exact subcommands, nested options and translations without TUI back entries", () => {
  const bar = suggest("/bar");
  assert.equal(bar[0].value, "/bar");
  assert.equal(bar[0].more, true);
  assert.equal(bar.filter(item => item.value === "/bar").length, 1);
  assert.equal(bar.find(item => item.value === "/bar new")?.description.includes("保留"), true);
  assert.deepEqual(suggest("/bar output ").map(item => item.value), ["/bar output atomic", "/bar output stream", "/bar output status"]);
  assert.equal(suggest("/bar 返回")[0].value, "/bar leave");
  assert.equal(suggest("/bar custom-session").length, 0);
  assert.equal(suggest("/memory remember a fact").length, 0);
  assert.equal(commandUsage("/bar custom-session", modes), "/bar new · /bar leave · /bar <会话名>");
  assert.equal(new Set(bar.map(item => item.value)).size, bar.length);
});

test("active mode actions lead and cross-mode choices explain existing isolation", () => {
  assert.equal(suggest("/", "bar")[0].value, "/bar leave");
  assert.equal(suggest("/minimal", "bar")[0].hint, "先执行 /bar leave 返回通用模式");
  assert.equal(suggest("/bar leave", "bar")[0].hint, undefined);
});

test("only exact registered mode-history commands route to their namespaces", () => {
  assert.equal(modeSessionTarget("/bar sessions", modes), "bar");
  assert.equal(modeSessionTarget("/minimal sessions ", modes), "minimal");
  assert.equal(modeSessionTarget("/bar sessions-custom", modes), undefined);
  assert.equal(modeSessionTarget("/bar sessions custom", modes), undefined);
  assert.equal(modeSessionTarget("/missing sessions", modes), undefined);
});

test("installation-local modes are discovered without inventing absent commands", () => {
  const local = { mode: "local", command: "/draftroom", label: "草稿室", description: "本机模式" };
  const commands = [{ command: local.command, id: "local", takes_args: true, group: "CHAT", description: local.description,
    options: [{ command: "leave", completion: "/draftroom leave", description: "return" }, { command: "list", completion: "/draftroom list", description: "sessions" }] }];
  const described = describeCommands(commands, [local]);
  assert.equal(suggestCommands(described, "/", [local], "local")[0].value, "/draftroom leave");
  assert.equal(suggestCommands(described, "/酒吧", [local], "local").length, 0);
  assert.equal(modeSessionTarget("/draftroom list", [local]), "local");
});
