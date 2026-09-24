import assert from "node:assert/strict";
import {
  completeSlashCommand,
  groupedSuggestionRowCount,
  normalizeSelectedCommandIndex,
  resolveSlashCommandSubmission,
  slashCommandSuggestions,
  submitSlashCommand,
  visibleGroupedSuggestionWindow,
  visibleSuggestionWindow,
} from "./command-menu.js";

assert.deepEqual(
  slashCommandSuggestions("/").map((item) => item.command),
  ["/image", "/bar", "/minimal", "/sip", "/reset", "/compress", "/undo", "/retry", "/changes", "/think", "/model", "/mode", "/connect", "/persona", "/search", "/tool", "/gallery", "/memory", "/skills", "/learn", "/tools", "/browser", "/computer", "/conclave", "/appshot", "/tasks", "/budget", "/resume", "/cancel", "/goal", "/today", "/session", "/handoff", "/theme", "/timeline", "/health", "/doctor", "/diagnostics", "/maintenance", "/sandbox", "/vision-tiles", "/context-index", "/mcp", "/yolo", "/permissions", "/reload", "/reconnect", "/restart", "/wakeup", "/help"],
);

assert.deepEqual(
  slashCommandSuggestions("/computer ").map((item) => item.command),
  ["status", "setup", "stop"],
);
assert.equal(completeSlashCommand("/computer se", 0), "/computer setup");
assert.deepEqual(resolveSlashCommandSubmission("/computer ", 0), { kind: "submit", value: "/computer status" });
assert.deepEqual(resolveSlashCommandSubmission("/computer se", 0), { kind: "submit", value: "/computer setup" });

assert.deepEqual(
  slashCommandSuggestions("/mod").map((item) => item.command),
  ["/model", "/mode"],
);

assert.deepEqual(
  slashCommandSuggestions("/pe").map((item) => item.command),
  ["/persona", "/permissions"],
);

assert.deepEqual(
  slashCommandSuggestions("/persona").map((item) => item.command),
  ["lyra"],
);
assert.deepEqual(
  slashCommandSuggestions("/persona ").map((item) => item.command),
  ["lyra"],
);
assert.deepEqual(
  slashCommandSuggestions("/persona ly").map((item) => item.command),
  ["lyra"],
);
assert.equal(completeSlashCommand("/persona ly", 0), "/persona lyra");
assert.equal(submitSlashCommand("/persona ly", 0), "/persona lyra");
assert.deepEqual(slashCommandSuggestions("/persona unknown"), []);
assert.equal(submitSlashCommand("/pe", 0), "/persona");
assert.equal(submitSlashCommand("/resume deadbeef", 0), "/resume deadbeef");
assert.equal(submitSlashCommand("/cancel selected-id", 0), "/cancel selected-id");
assert.equal(submitSlashCommand("/handoff next steps", 0), "/handoff next steps");
assert.equal(submitSlashCommand("/CANCEL selected-id", 0), "/cancel selected-id");
assert.equal(submitSlashCommand("/RESUME id", 0), "/resume id");
assert.equal(
  submitSlashCommand(String.raw`/CANCEL  C:\Temp\scan.png inspect  `, 0),
  String.raw`/cancel  C:\Temp\scan.png inspect  `,
);
assert.equal(submitSlashCommand("/resume", 0), null);
assert.equal(submitSlashCommand("/cancel", 0), "/cancel");
assert.equal(submitSlashCommand("/handoff", 0), "/handoff");
assert.equal(
  submitSlashCommand(String.raw`/image C:\\Temp\\scan.png inspect`, 0),
  String.raw`/image C:\\Temp\\scan.png inspect`,
);

// Fuzzy search still works without a prefix.
assert.deepEqual(
  slashCommandSuggestions("/persona yra").map((item) => item.command),
  ["lyra"],
);

// Dynamic personas from the backend override the hardcoded fallback.
assert.deepEqual(
  slashCommandSuggestions("/persona ", [], [], {
    personas: [{ name: "custom-x", description: "custom persona" }],
  }).map((item) => item.command),
  ["custom-x"],
);



assert.deepEqual(
  slashCommandSuggestions("/bar ").map((item) => item.command),
  ["enter", "new", "sip", "leave", "status", "output", "sessions"],
);
assert.equal(completeSlashCommand("/bar l", 0), "/bar leave");
assert.equal(submitSlashCommand("/bar l", 0), "/bar leave");
const barContext = {
  barSessions: [
    { name: "bar_20260717_220000", messages: 8, current: true },
    { name: "bar_20260716_230000", messages: 14, current: false },
  ],
};
assert.deepEqual(
  slashCommandSuggestions("/bar", [], [], barContext).map((item) => item.command),
  ["enter", "new", "sip", "leave", "status", "output", "sessions"],
);
assert.equal(completeSlashCommand("/bar", 5, [], [], barContext), "/bar output ");
assert.equal(submitSlashCommand("/bar", 5, [], [], barContext), null);
assert.deepEqual(
  slashCommandSuggestions("/bar output ").map((item) => item.command),
  ["back", "atomic", "stream", "status"],
);
assert.equal(completeSlashCommand("/bar output ", 0), "/bar ");
assert.equal(submitSlashCommand("/bar output st", 0), "/bar output stream");
assert.equal(completeSlashCommand("/bar", 6, [], [], barContext), "/bar sessions ");
assert.equal(submitSlashCommand("/bar", 6, [], [], barContext), null);
assert.deepEqual(
  slashCommandSuggestions("/bar sessions ", [], [], barContext).map((item) => item.command),
  ["back", "bar_20260717_220000", "bar_20260716_230000"],
);
assert.equal(completeSlashCommand("/bar sessions ", 0, [], [], barContext), "/bar ");
assert.equal(
  submitSlashCommand("/bar sessions ", 2, [], [], barContext),
  "/bar bar_20260716_230000",
);
assert.deepEqual(
  slashCommandSuggestions("/bar sessions bar_20260716", [], [], barContext).map((item) => item.command),
  ["bar_20260716_230000"],
);

assert.deepEqual(
  slashCommandSuggestions("/minimal ").map((item) => item.command),
  ["enter", "new", "leave", "status", "sessions"],
);
assert.equal(completeSlashCommand("/minimal n", 0), "/minimal new");
const minimalContext = {
  minimalSessions: [
    { name: "minimal_20260815_120000", messages: 4, current: true },
    { name: "minimal_20260814_230000", messages: 9, current: false },
  ],
};
assert.deepEqual(
  slashCommandSuggestions("/minimal sessions ", [], [], minimalContext).map((item) => item.command),
  ["back", "minimal_20260815_120000", "minimal_20260814_230000"],
);
assert.equal(
  submitSlashCommand("/minimal sessions ", 2, [], [], minimalContext),
  "/minimal minimal_20260814_230000",
);

const localContext = {
  localMode: {
    command: "/fixture", label: "Fixture", description: "local fixture",
    ui: { brand: "FIXTURE MODE", exitLabel: "leave fixture", headerTag: "PRIVATE", idle: "FIXTURE OPEN", busy: "FIXTURE…", placeholder: "type a message" },
  },
  localSessions: [
    { name: "fixture_recent", messages: 2, current: true },
    { name: "fixture_older", messages: 11, current: false },
  ],
};
assert.ok(!slashCommandSuggestions("/").some((item) => item.command === "/fixture"));
assert.ok(!slashCommandSuggestions("/").some((item) => item.command === "/write"));
assert.deepEqual(slashCommandSuggestions("/fixture ", [], [], localContext).map((item) => item.command),
  ["enter", "new", "leave", "status", "undo", "retry", "sessions"]);
for (const [input, expected] of [["n", "new"], ["u", "undo"], ["r", "retry"]]) {
  assert.equal(completeSlashCommand(`/fixture ${input}`, 0, [], [], localContext), `/fixture ${expected}`);
}
assert.deepEqual(slashCommandSuggestions("/fix", [], [], localContext).map((item) => item.command), ["/fixture"]);
assert.deepEqual(slashCommandSuggestions("/fixture sessions ", [], [], localContext).map((item) => item.command),
  ["back", "fixture_recent", "fixture_older"]);
assert.equal(submitSlashCommand("/fixture sessions ", 2, [], [], localContext), "/fixture fixture_older");

assert.deepEqual(
  slashCommandSuggestions("/search ").map((item) => item.command),
  ["auto", "exa", "searxng"],
);
assert.deepEqual(slashCommandSuggestions("/search ex").map((item) => item.command), ["exa"]);
assert.equal(completeSlashCommand("/search ex", 0), "/search exa");
assert.equal(submitSlashCommand("/search ex", 0), "/search exa");

assert.deepEqual(slashCommandSuggestions("/sandbox ").map((item) => item.command), ["on", "off", "status"]);
assert.equal(completeSlashCommand("/sandbox of", 0), "/sandbox off");
assert.equal(submitSlashCommand("/sandbox of", 0), "/sandbox off");
assert.ok(slashCommandSuggestions("/").some((item) => item.command === "/vision-tiles"));
assert.deepEqual(
  slashCommandSuggestions("/vision-tiles ").map((item) => item.command),
  ["on", "off", "status"],
);
assert.equal(completeSlashCommand("/vision-tiles of", 0), "/vision-tiles off");
assert.equal(submitSlashCommand("/vision-tiles of", 0), "/vision-tiles off");
assert.ok(slashCommandSuggestions("/").some((item) => item.command === "/context-index"));
assert.deepEqual(
  slashCommandSuggestions("/context-index ").map((item) => item.command),
  ["on", "off", "session", "all", "status", "why"],
);
assert.equal(completeSlashCommand("/context-index se", 0), "/context-index session");
assert.equal(submitSlashCommand("/context-index wh", 0), "/context-index why");
assert.deepEqual(
  slashCommandSuggestions("/diagnostics ").map((item) => item.command),
  ["context", "startup", "mcp", "tasks", "json"],
);
assert.equal(submitSlashCommand("/diagnostics con", 0), "/diagnostics context");
assert.deepEqual(
  slashCommandSuggestions("/maintenance ").map((item) => item.command),
  ["preview", "apply", "checkpoint"],
);
assert.equal(completeSlashCommand("/maintenance app", 0), "/maintenance apply ");
assert.deepEqual(slashCommandSuggestions("/maintenance apply 30"), []);

assert.deepEqual(
  slashCommandSuggestions("/theme ").map((item) => item.command),
  ["hermes", "glitchcity", "classic", "nord", "dracula", "solarized", "gruvbox", "lyra", "moonlit", "phosphor", "obsidian"],
);
assert.deepEqual(
  slashCommandSuggestions("/timeline ").map((item) => item.command),
  ["on", "off"],
);
assert.deepEqual(
  slashCommandSuggestions("/").slice(0, 9).map((item) => item.group),
  ["CHAT", "CHAT", "CHAT", "CHAT", "CHAT", "CHAT", "CHAT", "CHAT", "CHAT"],
);
assert.equal(completeSlashCommand("/theme gli", 0), "/theme glitchcity");
assert.equal(submitSlashCommand("/theme gli", 0), "/theme glitchcity");
assert.equal(completeSlashCommand("/theme dr", 0), "/theme dracula");
assert.equal(submitSlashCommand("/theme dr", 0), "/theme dracula");
const themesWithCurrent = slashCommandSuggestions("/theme ", [], [], { themeName: "classic" });
assert.equal(themesWithCurrent.find((item) => item.command === "classic")?.current, true);
assert.equal(themesWithCurrent.find((item) => item.command === "glitchcity")?.current, false);
assert.equal(themesWithCurrent.every((item) => item.group === "THEMES"), true);

assert.deepEqual(
  slashCommandSuggestions("/memory ").map((item) => item.command),
  [
    "review", "status", "remember", "remember-user", "working", "inspect", "timeline",
    "why", "correct", "forget", "import-core", "clear-working",
  ],
);
assert.deepEqual(slashCommandSuggestions("/memory rem").map((item) => item.command), ["remember", "remember-user"]);
assert.equal(completeSlashCommand("/memory sta", 0), "/memory status");
assert.equal(submitSlashCommand("/memory sta", 0), "/memory status");
assert.equal(completeSlashCommand("/memory time", 0), "/memory timeline ");

assert.deepEqual(slashCommandSuggestions("/skills ").map((item) => item.command), ["list", "show", "create"]);
assert.deepEqual(
  slashCommandSuggestions("/learn ").map((item) => item.command),
  ["status", "review", "migrate", "history", "undo", "legacy pending", "legacy show", "mode"],
);
assert.equal(submitSlashCommand("/learn hist", 0), "/learn history");
assert.equal(submitSlashCommand("/learn legacy p", 0), "/learn legacy pending");
assert.equal(submitSlashCommand("/learn undo", 0), null); // Requires an explicit run ID.

assert.deepEqual(slashCommandSuggestions("hello"), []);
assert.deepEqual(slashCommandSuggestions("/unknown"), []);

assert.equal(normalizeSelectedCommandIndex(8, 8), 0);
assert.equal(normalizeSelectedCommandIndex(-1, 8), 7);

assert.deepEqual(visibleSuggestionWindow([0, 1, 2, 3, 4, 5], 0, 3), [0, 1, 2]);
assert.deepEqual(visibleSuggestionWindow([0, 1, 2, 3, 4, 5], 3, 3), [2, 3, 4]);
assert.deepEqual(visibleSuggestionWindow([0, 1, 2, 3, 4, 5], 5, 3), [3, 4, 5]);

const grouped = [
  { command: "/minimal", group: "CHAT" },
  { command: "/model", group: "MODEL" },
  { command: "/mode", group: "MODEL" },
  { command: "/memory", group: "TOOLS" },
  { command: "/mcp", group: "SYSTEM" },
];

assert.equal(groupedSuggestionRowCount(grouped), 10);
assert.deepEqual(
  visibleGroupedSuggestionWindow(grouped, 0, 8).map((item) => item.command),
  ["/minimal", "/model", "/mode", "/memory"],
);
assert.equal(groupedSuggestionRowCount(visibleGroupedSuggestionWindow(grouped, 0, 8)), 8);
assert.equal(visibleGroupedSuggestionWindow(grouped, 4, 5).at(-1)?.command, "/mcp");

assert.equal(completeSlashCommand("/mo", 0), "/model ");
assert.equal(completeSlashCommand("/r", 0), "/reset");
assert.equal(completeSlashCommand("/r", 1), "/retry");
assert.equal(completeSlashCommand("/r", 2), "/resume ");
assert.equal(completeSlashCommand("/r", 3), "/reload ");
assert.equal(completeSlashCommand("/r", 4), "/reconnect");
assert.equal(completeSlashCommand("/und", 0), "/undo ");
assert.equal(completeSlashCommand("/ret", 0), "/retry");
assert.equal(completeSlashCommand("hello", 0), null);

const sessions = [
  { name: "default", messages: 2 },
  { name: "draft_review", messages: 12, current: true },
  { name: "paper_fix", messages: 4 },
];

assert.deepEqual(
  slashCommandSuggestions("/session", sessions).map((item) => item.command),
  ["delete", "rename", "export", "default", "draft_review", "paper_fix"],
);
assert.deepEqual(
  slashCommandSuggestions("/session ", sessions).map((item) => item.command),
  ["delete", "rename", "export", "default", "draft_review", "paper_fix"],
);
assert.deepEqual(
  slashCommandSuggestions("/session dr", sessions).map((item) => item.command),
  ["draft_review"],
);
assert.equal(completeSlashCommand("/session dr", 0, sessions), "/session draft_review");
assert.equal(submitSlashCommand("/session dr", 0, sessions), "/session draft_review");
assert.equal(completeSlashCommand("/session d", 0, sessions), "/session delete ");
assert.equal(submitSlashCommand("/r", 0), "/reset");
assert.equal(submitSlashCommand("/mo", 0), "/model");
assert.equal(completeSlashCommand("/con", 0), "/connect ");
assert.equal(submitSlashCommand("/doc", 0), "/doctor");
assert.equal(completeSlashCommand("/perm", 0), "/permissions ");
assert.deepEqual(slashCommandSuggestions("/dct").map((item) => item.command), ["/doctor"]);

const models = [
  { name: "deepseek-v4-flash", current: true, provider: "OMLX · 8000", endpoint: "http://192.0.2.10:8000/v1" },
  { name: "deepseek-v4-pro" },
  { name: "Qwen3.6-35B-A3B" },
  { name: "gemma-4-12B-it" },
];

assert.deepEqual(
  slashCommandSuggestions("/model ", [], models).map((item) => item.command),
  ["deepseek-v4-flash", "deepseek-v4-pro", "Qwen3.6-35B-A3B", "gemma-4-12B-it"],
);
assert.deepEqual(
  slashCommandSuggestions("/model deepseek-v4-p", [], models).map((item) => item.command),
  ["deepseek-v4-pro"],
);
assert.equal(completeSlashCommand("/model deepseek-v4-p", 0, [], models), "/model deepseek-v4-pro");
assert.equal(submitSlashCommand("/model deepseek-v4-p", 0, [], models), "/model deepseek-v4-pro");
const currentModel = slashCommandSuggestions("/model ", [], models)[0];
assert.equal(currentModel.command, "deepseek-v4-flash");
assert.equal(currentModel.description, "current · OMLX · 8000 · http://192.0.2.10:8000/v1");

// Reasoning intensity replaces both old mode selectors.
assert.deepEqual(slashCommandSuggestions("/mode ").map((item) => item.command), ["low", "high", "xhigh", "max"]);
for (const effort of ["low", "high", "xhigh", "max"]) {
  assert.equal(submitSlashCommand(`/mode ${effort}`, 0), `/mode ${effort}`);
  assert.equal(completeSlashCommand(`/mode ${effort}`, 0), `/mode ${effort}`);
}
assert.deepEqual(slashCommandSuggestions("/mode code"), []);
assert.deepEqual(slashCommandSuggestions("/mode chat"), []);

assert.equal(submitSlashCommand("/budget", 0), "/budget");
assert.equal(submitSlashCommand("/budget 60", 0), "/budget 60");
assert.equal(submitSlashCommand("/budget off", 0), "/budget off");

assert.deepEqual(slashCommandSuggestions("/browser ").map((item) => item.command), ["status", "stop"]);
assert.equal(completeSlashCommand("/browser st", 1), "/browser stop");
