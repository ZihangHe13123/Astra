import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { initialSession, projectEvent } from "@astra/ui-core/session-state";
import { describeCommands, suggestCommands } from "../src/renderer/command-help.js";
import { MemoryReviewSettings, reviewAnswer, withProposalOptions } from "../src/renderer/memory-review.js";
import type { CommandDescription } from "../src/bridge.js";

const status = (fields: Record<string, unknown> = {}) => ({ type: "memory_consolidation_status", mode: "review", pending: 0, ...fields });
const catalog = JSON.parse(readFileSync(new URL("../../agent/ui/commands.json", import.meta.url), "utf8")) as CommandDescription[];
const pressed = (html: string) => [...html.matchAll(/<button[^>]*aria-pressed="true"[^>]*><strong>([^<]*)<\/strong>/g)].map(match => match[1]);

function section(props: Partial<React.ComponentProps<typeof MemoryReviewSettings>> = {}) {
  return React.createElement(MemoryReviewSettings, { status: status(), ready: true, run: () => {}, review: () => {}, ...props });
}

/** The buttons of a rendered section, found in the element tree, so a test can press them. */
function buttons(node: React.ReactNode): React.ReactElement<{ onClick?: () => void; children?: React.ReactNode }>[] {
  if (Array.isArray(node)) return node.flatMap(buttons);
  if (!React.isValidElement<{ children?: React.ReactNode; onClick?: () => void }>(node)) return [];
  if (typeof node.type === "function") return buttons((node.type as (props: unknown) => React.ReactNode)(node.props));
  return [...(node.type === "button" ? [node] : []), ...buttons(node.props.children)];
}

test("the settings section shows the mode the backend last reported", () => {
  // The same reducer the desktop runs: the latest status replaces the one before it.
  let state = projectEvent(initialSession("memory"), status());
  const render = (ready = true) => renderToStaticMarkup(section({ status: state.info.memory_consolidation_status, ready }));
  assert.deepEqual(pressed(render()), ["需审核"]);
  assert.doesNotMatch(render(), /条建议待审核/);

  state = projectEvent(state, status({ mode: "auto", pending: 2 }));
  assert.deepEqual(pressed(render()), ["自动同意"]);
  assert.match(render(), /2 条建议待审核/);

  // Without a ready session nothing can be switched.
  assert.equal(render(false).match(/<button disabled=""/g)?.length, 3);
  assert.match(render(false), /打开一个会话后可以切换/);
  assert.doesNotMatch(render(false), /条建议待审核/);
  // An older backend reports nothing: no choice is shown as the current one.
  assert.deepEqual(pressed(renderToStaticMarkup(section({ status: undefined }))), []);
});

test("each choice sends the command that saves it, and the waiting count opens the proposals", () => {
  const sent: string[] = [];
  let reviewed = 0;
  const found = buttons(section({ status: status({ pending: 1 }), run: command => sent.push(command), review: () => { reviewed++; } }));
  for (const button of found) button.props.onClick?.();
  assert.deepEqual(sent, ["/memory consolidation off", "/memory consolidation review", "/memory consolidation auto"]);
  assert.equal(reviewed, 1);
});

test("a typed command is answered from the status the backend reports back", () => {
  assert.equal(reviewAnswer("auto", status({ mode: "auto", message: "Memory consolidation: auto" })), "记忆整理：自动同意");
  assert.equal(reviewAnswer("review", status({ pending: 3, message: "Memory consolidation: review" })), "记忆整理：需审核，3 条建议待审核");
  assert.equal(reviewAnswer("now", status({ message: "A memory review starts at the next idle moment." })), "将在下一个空闲时刻整理一次记忆");
  // Not carried out: the backend's reason is the answer.
  assert.equal(reviewAnswer("always", status({ message: "", error: "Usage: /memory consolidation [off|review|auto|now]" })),
    "记忆整理：Usage: /memory consolidation [off|review|auto|now]");
});

test("the command palette offers the review settings in the desktop's language", () => {
  const commands = describeCommands(catalog, []);
  const items = suggestCommands(commands, "/memory consolidation", [], "work");
  assert.deepEqual(items.map(item => item.value), [
    "/memory consolidation", "/memory consolidation off", "/memory consolidation review", "/memory consolidation auto",
    "/memory consolidation now"]);
  assert.equal(items.find(item => item.value === "/memory consolidation auto")?.label, "记忆整理：自动同意");
  assert.equal(suggestCommands(commands, "/memory prop", [], "work").find(item => item.value === "/memory proposals")?.label, "待审核的建议");
});

test("the palette offers each waiting proposal to accept or dismiss", () => {
  const raw = catalog.find(command => command.command === "/memory")!;
  const waiting = status({ pending: 2, proposals: [{ id: 7, action: "remember", summary: "前端项目用 pnpm" }, { id: 9, action: "forget", summary: "养了一只猫" }] });
  const commands = describeCommands(catalog.map(command => command === raw ? withProposalOptions(command, waiting) : command), []);
  const accept = suggestCommands(commands, "/memory accept", [], "work");
  assert.deepEqual(accept.map(item => [item.value, item.label]), [
    ["/memory accept", "接受建议"], ["/memory accept 7", "接受 P7"], ["/memory accept 9", "接受 P9"], ["/memory accept all", "全部接受"]]);
  assert.equal(accept.find(item => item.value === "/memory accept 9")?.description, "忘记：养了一只猫");
  assert.equal(suggestCommands(commands, "/memory dismiss 7", [], "work")[0]?.label, "丢弃 P7");
  // Nothing waiting, or an older backend that does not list them: only the registered actions.
  assert.equal(withProposalOptions(raw, status()), raw);
  assert.equal(withProposalOptions(raw, undefined), raw);
});
