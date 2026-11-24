import assert from "node:assert/strict";
import test from "node:test";
import * as commandMenu from "./command-menu.js";

const { completeSlashCommand, slashCommandSuggestions, submitSlashCommand } = commandMenu;

const sessions = [
  { name: "in-b", messages: 2, workspace: "/w/B" },
  { name: "in-a", messages: 3, workspace: "/w/A" },
  { name: "no-folder", messages: 4, workspace: null },
  { name: "now-open", messages: 1, current: true, workspace: null },
];
const context = { sessionFolder: "/w/B/", home: "/Users/u" };
const allNames = sessions.map(({ name }) => name);

test("the default session menu keeps the local and current sessions", () => {
  const suggestions = slashCommandSuggestions("/session ", sessions, [], context);
  const actions = suggestions.filter(({ group }) => group === "ACTIONS");
  assert.deepEqual(suggestions.filter(({ group }) => group === "SESSIONS").map(({ command }) => command), [
    "in-b", "now-open",
  ]);
  assert.deepEqual(actions.map(({ command }) => command), ["delete", "rename", "export", "--all"]);
  assert.equal(actions.at(-1)?.description, "2 more in other folders  ›");
});

test("the all-folders view labels folders and searches by session name", () => {
  const suggestions = slashCommandSuggestions("/session --all ", sessions, [], context);
  assert.deepEqual(suggestions.filter(({ group }) => group === "SESSIONS").map(({ command }) => command), allNames);
  assert.match(suggestions.find(({ command }) => command === "in-a")!.description, /\/w\/A/);
  assert.match(suggestions.find(({ command }) => command === "no-folder")!.description, /\(no folder recorded\)/);
  assert.match(suggestions.find(({ command }) => command === "now-open")!.description, / · current$/);
  assert.ok(suggestions.every(({ command }) => command !== "--all"));

  const search = slashCommandSuggestions("/session --all in-a", sessions, [], context);
  assert.deepEqual(search.filter(({ group }) => group === "SESSIONS").map(({ command }) => command), ["in-a"]);
});

for (const { missing, items, sessionFolder } of [
  {
    missing: "row workspaces",
    items: sessions.map(({ name, messages, current }) => ({ name, messages, current })),
    sessionFolder: context.sessionFolder,
  },
  { missing: "the active folder", items: sessions, sessionFolder: "" },
]) {
  test(`the default menu keeps every session without ${missing}`, () => {
    const suggestions = slashCommandSuggestions("/session ", items, [], { ...context, sessionFolder });
    assert.deepEqual(suggestions.filter(({ group }) => group === "SESSIONS").map(({ command }) => command), allNames);
    assert.ok(suggestions.every(({ command }) => command !== "--all"));
  });
}

for (const input of ["/session", "/session --all"]) {
  test(`Enter on ${input} leaves the command text for the backend`, () => {
    assert.equal(submitSlashCommand(input, 0, sessions, [], context), null);
  });
}

test("Enter on an all-folders search result submits the session name", () => {
  assert.equal(submitSlashCommand("/session --all in-a", 0, sessions, [], context), "/session in-a");
});

test("Tab on the all-folders action completes the submenu", () => {
  assert.equal(completeSlashCommand("/session ", 3, sessions, [], context), "/session --all ");
  assert.equal(slashCommandSuggestions("/session ", sessions, [], context)[3]?.kind, "submenu");
});

test("the folder label shortens only the home folder and its descendants", () => {
  assert.equal(commandMenu.sessionFolderLabel("/Users/u", context.home), "~");
  assert.equal(commandMenu.sessionFolderLabel("/Users/u/x/y", context.home), "~/x/y");
  assert.equal(commandMenu.sessionFolderLabel("/Users/ux", context.home), "/Users/ux");
  assert.equal(commandMenu.sessionFolderLabel(null, context.home), "(no folder recorded)");
});

test("folder comparison accepts a trailing separator and requires both paths", () => {
  assert.equal(commandMenu.sameSessionFolder("/w/B/", "/w/B"), true);
  assert.equal(commandMenu.sameSessionFolder("", ""), false);
});
