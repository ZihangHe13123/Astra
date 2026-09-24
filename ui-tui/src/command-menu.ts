import type { ProviderInfo } from "./types.js";
import type { LocalModeDefinition } from "./types.js";
import { THEMES, THEME_NAMES, type ThemeName } from "./theme.js";

export type SlashCommandSuggestion = {
  command: string;
  description: string;
  takesArgs?: boolean;
  argsRequired?: boolean;
  usage?: string;
  completion?: string;
  submitValue?: string;
  kind?: "command" | "session" | "submenu";
  group?: "CHAT" | "MODEL" | "TOOLS" | "SESSION" | "DISPLAY" | "SYSTEM" | string;
  current?: boolean;
};

export type SlashCommandSubmission =
  | { kind: "submit"; value: string }
  | { kind: "blocked"; message: string }
  | { kind: "none" };

export type CommandMenuContext = {
  providers?: ProviderInfo[];
  recentModels?: string[];
  themeName?: ThemeName;
  barSessions?: SessionMenuItem[];
  minimalSessions?: SessionMenuItem[];
  localSessions?: SessionMenuItem[];
  localMode?: LocalModeDefinition | null;
  personas?: { name: string; description: string }[];
};

const SLASH_COMMANDS: SlashCommandSuggestion[] = [
  { command: "/image", description: "attach image path with optional prompt", takesArgs: true, group: "CHAT" },
  { command: "/bar", description: "open an isolated private night shift", takesArgs: true, group: "CHAT" },
  { command: "/minimal", description: "open an isolated zero-injection coding session", takesArgs: true, group: "CHAT" },
  { command: "/sip", description: "take one manual sip from the current bar drink", group: "CHAT" },
  { command: "/reset", description: "clear current conversation", group: "CHAT" },
  { command: "/compress", description: "compress context via LLM summary", group: "CHAT" },
  { command: "/undo", description: "remove the last model reply or the last N exchanges", takesArgs: true, group: "CHAT" },
  { command: "/retry", description: "remove the last reply and resend the last request", group: "CHAT" },
  { command: "/changes", description: "review the change ledger of recent turns", takesArgs: true, group: "CHAT" },
  { command: "/think", description: "toggle reasoning display", group: "CHAT" },
  { command: "/model", description: "show or switch model", takesArgs: true, group: "MODEL" },
  { command: "/mode", description: "set reasoning effort (low/high/xhigh/max)", takesArgs: true, group: "MODEL" },
  { command: "/connect", description: "connect a provider and choose its model", takesArgs: true, group: "MODEL" },
  { command: "/persona", description: "show or switch prompt persona", takesArgs: true, group: "MODEL" },
  { command: "/search", description: "show or switch web search provider", takesArgs: true, group: "TOOLS" },
  { command: "/tool", description: "open latest or numbered tool result details", takesArgs: true, group: "TOOLS" },
  { command: "/gallery", description: "open latest or numbered image search gallery", takesArgs: true, group: "TOOLS" },
  { command: "/memory", description: "inspect or update working/core memory", takesArgs: true, group: "TOOLS" },
  { command: "/skills", description: "list, inspect, or create local skills", takesArgs: true, group: "TOOLS" },
  { command: "/learn", description: "discuss skill improvements, save, and undo changes", takesArgs: true, group: "TOOLS" },
  { command: "/tools", description: "list available tools", group: "TOOLS" },
  { command: "/browser", description: "show or release this session’s browser control", takesArgs: true, group: "TOOLS" },
  { command: "/computer", description: "show, set up, or stop local macOS Computer Use", takesArgs: true, group: "TOOLS" },
  { command: "/conclave", description: "research with experts and discuss the findings", takesArgs: true, group: "TOOLS" },
  { command: "/appshot", description: "status · shortcut · enable · disable", takesArgs: true, group: "CAPTURE" },
  { command: "/tasks", description: "list or inspect per-request task runs", takesArgs: true, group: "SESSION" },
  { command: "/budget", description: "set turn time limit in seconds, or off", takesArgs: true, group: "SESSION" },
  { command: "/resume", description: "resume an interrupted task", takesArgs: true, argsRequired: true, usage: "Usage: /resume <task-id>", group: "SESSION" },
  { command: "/cancel", description: "cancel the active or selected task", takesArgs: true, group: "SESSION" },
  { command: "/goal", description: "set/show/pause/resume the session goal (long-horizon verification mode)", takesArgs: true, group: "SESSION" },
  { command: "/today", description: "list today's task runs", group: "SESSION" },
  { command: "/session", description: "list or switch sessions", takesArgs: true, group: "SESSION" },
  { command: "/handoff", description: "compose, save, and revise a continuation brief", takesArgs: true, group: "SESSION" },
  { command: "/theme", description: "show or switch terminal color theme", takesArgs: true, group: "DISPLAY" },
  { command: "/timeline", description: "toggle conversation timeline display", takesArgs: true, group: "DISPLAY" },
  { command: "/health", description: "show harness diagnostics", group: "SYSTEM" },
  { command: "/doctor", description: "discuss diagnosis and repairs; --raw for report", takesArgs: true, group: "SYSTEM" },
  { command: "/diagnostics", description: "explain runtime state; --raw for snapshot", takesArgs: true, group: "SYSTEM" },
  { command: "/maintenance", description: "preview or apply bounded runtime cleanup", takesArgs: true, group: "SYSTEM" },
  { command: "/sandbox", description: "show or toggle Docker isolation", takesArgs: true, group: "SYSTEM" },
  { command: "/vision-tiles", description: "show or toggle DeepSeek original-pixel image tiling", takesArgs: true, group: "SYSTEM" },
  { command: "/context-index", description: "show or set proactive historical context suggestions", takesArgs: true, group: "SYSTEM" },
  { command: "/mcp", description: "show MCP server and tool status", group: "SYSTEM" },
  { command: "/yolo", description: "toggle live approvals bypass · Ctrl+Y · [on|off|status]", group: "SYSTEM" },
  { command: "/permissions", description: "inspect or change tool policy", takesArgs: true, group: "SYSTEM" },
  { command: "/reload", description: "hot-reload agent runtime (code/persona/skills/model)", takesArgs: true, group: "SYSTEM" },
  { command: "/reconnect", description: "restart backend connection", group: "SYSTEM" },
  { command: "/restart", description: "restart backend after current work is delivered; cancel stops waiting", takesArgs: true, group: "SYSTEM" },
  { command: "/wakeup", description: "session wakeups: status, cancel, after/every SECONDS PROMPT", takesArgs: true, group: "SYSTEM" },
  { command: "/help", description: "show command help", group: "SYSTEM" },
];

export type SessionMenuItem = {
  name: string;
  messages: number;
  current?: boolean;
};

export type ModelMenuItem = {
  provider_id?: string;
  source?: string;
  metadata_known?: boolean;
  name: string;
  key?: string;
  provider?: string;
  endpoint?: string;
  current?: boolean;
};

const PERSONA_PROFILES: SlashCommandSuggestion[] = [
  { command: "lyra", description: "thoughtful assistant for work and everyday conversation", completion: "/persona lyra", submitValue: "/persona lyra", kind: "command" },
];


const SEARCH_PROVIDERS: SlashCommandSuggestion[] = [
  { command: "auto", description: "smart routing with provider fallback", completion: "/search auto", submitValue: "/search auto", kind: "command" },
  { command: "exa", description: "force Exa semantic search", completion: "/search exa", submitValue: "/search exa", kind: "command" },
  { command: "searxng", description: "force local SearXNG metasearch", completion: "/search searxng", submitValue: "/search searxng", kind: "command" },
];

const MEMORY_SUBCOMMANDS: SlashCommandSuggestion[] = [
  { command: "review", description: "discuss memory evidence and proposed corrections", completion: "/memory review ", submitValue: "/memory review", kind: "command" },
  { command: "status", description: "show current working and core memory", completion: "/memory status", submitValue: "/memory status", kind: "command" },
  { command: "remember", description: "store a stable agent or environment fact in MEMORY.md", completion: "/memory remember ", kind: "command" },
  { command: "remember-user", description: "store a user profile or preference in USER.md", completion: "/memory remember-user ", kind: "command" },
  { command: "working", description: "update a working-memory field", completion: "/memory working ", kind: "command" },
  { command: "inspect", description: "inspect structured memory by query or id", completion: "/memory inspect ", kind: "command" },
  { command: "timeline", description: "show lifecycle and supersession history", completion: "/memory timeline ", kind: "command" },
  { command: "why", description: "show the last recall and retention decisions", completion: "/memory why", submitValue: "/memory why", kind: "command" },
  { command: "correct", description: "supersede one active memory with a correction", completion: "/memory correct ", kind: "command" },
  { command: "forget", description: "forget an active core or structured memory", completion: "/memory forget ", kind: "command" },
  { command: "import-core", description: "merge external Core Markdown edits", completion: "/memory import-core", submitValue: "/memory import-core", kind: "command" },
  { command: "clear-working", description: "clear this session's working memory", completion: "/memory clear-working", submitValue: "/memory clear-working", kind: "command" },
];

const CONCLAVE_SUBCOMMANDS: SlashCommandSuggestion[] = [
  { command: "config", description: "show current Conclave configuration", completion: "/conclave config", submitValue: "/conclave config", kind: "command" },
  { command: "config chairperson", description: "set chairperson LLM model", completion: "/conclave config chairperson ", kind: "submenu" },
  { command: "config expert_list", description: "override which experts to use (comma-separated names)", completion: "/conclave config expert_list ", kind: "submenu" },
  { command: "config sources", description: "set max sources per expert (1-20)", completion: "/conclave config sources ", kind: "command" },
  { command: "config max_tokens", description: "set chairperson max tokens (256-8192)", completion: "/conclave config max_tokens ", kind: "command" },
  { command: "config discussion", description: "toggle cross-discussion phase on/off", completion: "/conclave config discussion ", kind: "command" },
  { command: "config reset", description: "restore default Conclave configuration", completion: "/conclave config reset", submitValue: "/conclave config reset", kind: "command" },
];

const EXPERT_ITEMS: SlashCommandSuggestion[] = [
  { command: "学术专家", description: "Google Scholar · 学术论文与顶会", completion: "/conclave config expert_list 学术专家, ", kind: "command" },
  { command: "arXiv 专家", description: "arXiv · 最新预印本研究", completion: "/conclave config expert_list arXiv 专家, ", kind: "command" },
  { command: "GitHub 专家", description: "GitHub · 代码与开源生态", completion: "/conclave config expert_list GitHub 专家, ", kind: "command" },
  { command: "Google 专家", description: "Google · 综合英文搜索", completion: "/conclave config expert_list Google 专家, ", kind: "command" },
  { command: "Bing 专家", description: "Bing · 补充英文搜索", completion: "/conclave config expert_list Bing 专家, ", kind: "command" },
  { command: "StackOverflow 专家", description: "StackOverflow · 实战技术问答", completion: "/conclave config expert_list StackOverflow 专家, ", kind: "command" },
  { command: "知乎专家", description: "知乎 · 中文深度讨论", completion: "/conclave config expert_list 知乎专家, ", kind: "command" },
  { command: "百度专家", description: "百度 · 中文综合搜索", completion: "/conclave config expert_list 百度专家, ", kind: "command" },
  { command: "Wikipedia 专家", description: "Wikipedia · 百科知识", completion: "/conclave config expert_list Wikipedia 专家, ", kind: "command" },
  { command: "新闻专家", description: "Google News · 时效资讯", completion: "/conclave config expert_list 新闻专家, ", kind: "command" },
  { command: "Reddit 专家", description: "Reddit · 社区讨论", completion: "/conclave config expert_list Reddit 专家, ", kind: "command" },
  { command: "B站专家", description: "B站 · 中文视频教程", completion: "/conclave config expert_list B站专家, ", kind: "command" },
];

const BAR_SUBCOMMANDS: SlashCommandSuggestion[] = [
  { command: "enter", description: "open the latest isolated night shift", completion: "/bar enter", submitValue: "/bar enter", kind: "command" },
  { command: "new", description: "forget this shift and start another", completion: "/bar new", submitValue: "/bar new", kind: "command" },
  { command: "sip", description: "take one sip from the current drink", completion: "/bar sip", submitValue: "/bar sip", kind: "command" },
  { command: "leave", description: "close the bar and restore work context", completion: "/bar leave", submitValue: "/bar leave", kind: "command" },
  { command: "status", description: "show isolation state", completion: "/bar status", submitValue: "/bar status", kind: "command" },
  { command: "output", description: "choose atomic or streaming replies  ›", completion: "/bar output ", kind: "submenu" },
  { command: "sessions", description: "browse saved private night shifts  ›", completion: "/bar sessions ", kind: "submenu" },
];

const MINIMAL_SUBCOMMANDS: SlashCommandSuggestion[] = [
  { command: "enter", description: "open the latest isolated minimal session", completion: "/minimal enter", submitValue: "/minimal enter", kind: "command" },
  { command: "new", description: "start another saved minimal session", completion: "/minimal new", submitValue: "/minimal new", kind: "command" },
  { command: "leave", description: "close minimal and restore work context", completion: "/minimal leave", submitValue: "/minimal leave", kind: "command" },
  { command: "status", description: "show minimal isolation state", completion: "/minimal status", submitValue: "/minimal status", kind: "command" },
  { command: "sessions", description: "browse saved minimal sessions  ›", completion: "/minimal sessions ", kind: "submenu" },
];

function localSubcommands(mode: LocalModeDefinition): SlashCommandSuggestion[] {
  return [
    ["enter", "open the latest isolated session"],
    ["new", "start another saved session"],
    ["leave", "close this mode and restore work context"],
    ["status", "show isolation state"],
    ["undo", "remove the last model reply · undo N for N exchanges"],
    ["retry", "remove the last reply and resend the last request"],
    ["sessions", "browse saved sessions  ›"],
  ].map(([command, description]) => ({
    command, description, group: `${mode.label.toUpperCase()} MODE`,
    completion: `${mode.command} ${command}${command === "sessions" ? " " : ""}`,
    submitValue: command === "sessions" ? undefined : `${mode.command} ${command}`,
    kind: command === "sessions" ? "submenu" : "command",
  }));
}

const BAR_OUTPUT_OPTIONS: SlashCommandSuggestion[] = [
  { command: "atomic", description: "validated turn · text and scene state commit together", completion: "/bar output atomic", submitValue: "/bar output atomic", kind: "command" },
  { command: "stream", description: "show prose immediately · scene tools update afterward", completion: "/bar output stream", submitValue: "/bar output stream", kind: "command" },
  { command: "status", description: "show the saved output mode", completion: "/bar output status", submitValue: "/bar output status", kind: "command" },
];

const SANDBOX_MODES: SlashCommandSuggestion[] = [
  { command: "on", description: "run Shell/Python in isolated Docker", completion: "/sandbox on", submitValue: "/sandbox on", kind: "command" },
  { command: "off", description: "run on host with timeout and dangerous-command checks", completion: "/sandbox off", submitValue: "/sandbox off", kind: "command" },
  { command: "status", description: "show the active execution backend", completion: "/sandbox status", submitValue: "/sandbox status", kind: "command" },
];

const VISION_TILE_OPTIONS: SlashCommandSuggestion[] = [
  { command: "on", description: "preserve large-image detail with automatic tiles", completion: "/vision-tiles on", submitValue: "/vision-tiles on", kind: "command" },
  { command: "off", description: "send originals directly; DeepSeek may downscale them", completion: "/vision-tiles off", submitValue: "/vision-tiles off", kind: "command" },
  { command: "status", description: "show saved and active tiling state", completion: "/vision-tiles status", submitValue: "/vision-tiles status", kind: "command" },
];

const CONTEXT_INDEX_OPTIONS: SlashCommandSuggestion[] = [
  { command: "on", description: "enable Session and cached Activity suggestions", completion: "/context-index on", submitValue: "/context-index on", kind: "command" },
  { command: "off", description: "disable proactive historical suggestions", completion: "/context-index off", submitValue: "/context-index off", kind: "command" },
  { command: "session", description: "recommend Session history only", completion: "/context-index session", submitValue: "/context-index session", kind: "command" },
  { command: "all", description: "recommend Session and cached Activity history", completion: "/context-index all", submitValue: "/context-index all", kind: "command" },
  { command: "status", description: "show the saved and active mode", completion: "/context-index status", submitValue: "/context-index status", kind: "command" },
  { command: "why", description: "audit the last recommendation decision", completion: "/context-index why", submitValue: "/context-index why", kind: "command" },
];

const MODE_OPTIONS: SlashCommandSuggestion[] = [
  { command: "low", description: "light reasoning", completion: "/mode low", submitValue: "/mode low", kind: "command" },
  { command: "high", description: "standard reasoning (default)", completion: "/mode high", submitValue: "/mode high", kind: "command" },
  { command: "xhigh", description: "deeper reasoning, costs more than high", completion: "/mode xhigh", submitValue: "/mode xhigh", kind: "command" },
  { command: "max", description: "deepest reasoning, costs the most", completion: "/mode max", submitValue: "/mode max", kind: "command" },
];

const DIAGNOSTIC_SECTIONS: SlashCommandSuggestion[] = [
  { command: "context", description: "token composition and prompt cache", completion: "/diagnostics context", submitValue: "/diagnostics context", kind: "command" },
  { command: "startup", description: "startup phase timings", completion: "/diagnostics startup", submitValue: "/diagnostics startup", kind: "command" },
  { command: "mcp", description: "live MCP readiness snapshot", completion: "/diagnostics mcp", submitValue: "/diagnostics mcp", kind: "command" },
  { command: "tasks", description: "durable tasks and retained processes", completion: "/diagnostics tasks", submitValue: "/diagnostics tasks", kind: "command" },
  { command: "json", description: "machine-readable full snapshot", completion: "/diagnostics json", submitValue: "/diagnostics json", kind: "command" },
];

const MAINTENANCE_ACTIONS: SlashCommandSuggestion[] = [
  { command: "preview", description: "show candidates without changing files", completion: "/maintenance preview ", kind: "command" },
  { command: "apply", description: "delete expired generated artifacts", completion: "/maintenance apply ", kind: "command" },
  { command: "checkpoint", description: "checkpoint project SQLite WAL files only", completion: "/maintenance checkpoint", submitValue: "/maintenance checkpoint", kind: "command" },
];

const THEME_OPTIONS: SlashCommandSuggestion[] = THEME_NAMES.map((name) => ({
  command: name,
  description: THEMES[name].description,
  completion: `/theme ${name}`,
  submitValue: `/theme ${name}`,
  kind: "command",
  group: "THEMES",
}));

const TIMELINE_OPTIONS: SlashCommandSuggestion[] = [
  { command: "on", description: "show timestamps and the shared time rail", completion: "/timeline on", submitValue: "/timeline on", kind: "command", group: "TIMELINE" },
  { command: "off", description: "hide timestamps while preserving model time awareness", completion: "/timeline off", submitValue: "/timeline off", kind: "command", group: "TIMELINE" },
];

function fuzzyMatch(value: string, query: string): boolean {
  if (!query) return true;
  const candidate = value.toLowerCase();
  if (candidate.includes(query)) return true;
  let cursor = 0;
  for (const char of candidate) {
    if (char === query[cursor]) cursor += 1;
    if (cursor === query.length) return true;
  }
  return false;
}

const SKILL_SUBCOMMANDS: SlashCommandSuggestion[] = [
  { command: "list", description: "list local skills", completion: "/skills list", submitValue: "/skills list", kind: "command" },
  { command: "show", description: "show a skill or supporting file", completion: "/skills show ", kind: "command" },
  { command: "create", description: "draft a skill together before saving", completion: "/skills create ", submitValue: "/skills create", kind: "command" },
];

const LEARN_SUBCOMMANDS: SlashCommandSuggestion[] = [
  { command: "status", description: "show direct skill learning status", completion: "/learn status", submitValue: "/learn status", kind: "command" },
  { command: "review", description: "discuss skill improvements and choose changes", completion: "/learn review", submitValue: "/learn review", kind: "command" },
  { command: "migrate", description: "import historical automatic summaries; preserve user skills", completion: "/learn migrate", submitValue: "/learn migrate", kind: "command" },
  { command: "history", description: "show changes and reasons", completion: "/learn history", submitValue: "/learn history", kind: "command" },
  { command: "undo", description: "restore a learning change by run ID", completion: "/learn undo ", kind: "command" },
  { command: "legacy pending", description: "inspect historical candidates", completion: "/learn legacy pending", submitValue: "/learn legacy pending", kind: "command" },
  { command: "legacy show", description: "read one historical candidate", completion: "/learn legacy show ", kind: "command" },
  { command: "mode", description: "set off or review mode", completion: "/learn mode ", kind: "command" },
];

const SESSION_SUBCOMMANDS: SlashCommandSuggestion[] = [
  { command: "delete", description: "delete a saved session", completion: "/session delete ", kind: "session" },
  { command: "rename", description: "rename a saved session", completion: "/session rename ", kind: "session" },
  { command: "export", description: "export a session to Markdown", completion: "/session export ", kind: "session" },
];

const BROWSER_SUBCOMMANDS: SlashCommandSuggestion[] = [
  { command: "status", description: "inspect browser control without acquiring it", completion: "/browser status", submitValue: "/browser status", kind: "command" },
  { command: "stop", description: "release browser control; keep the user browser open", completion: "/browser stop", submitValue: "/browser stop", kind: "command" },
];

const COMPUTER_SUBCOMMANDS: SlashCommandSuggestion[] = [
  { command: "status", description: "show bounded Computer Use readiness without opening settings", completion: "/computer status", submitValue: "/computer status", kind: "command" },
  { command: "setup", description: "open only missing macOS privacy settings", completion: "/computer setup", submitValue: "/computer setup", kind: "command" },
  { command: "stop", description: "close the local Computer Use session", completion: "/computer stop", submitValue: "/computer stop", kind: "command" },
];

export function slashCommandSuggestions(
  input: string,
  sessions: SessionMenuItem[] = [],
  models: ModelMenuItem[] = [],
  context: CommandMenuContext = {},
): SlashCommandSuggestion[] {
  const trimmed = input.trimStart();
  if (!trimmed.startsWith("/")) return [];
  const diagnosticsMatch = trimmed.match(/^\/diagnostics(?:\s+([\s\S]*))?$/i);
  if (diagnosticsMatch) {
    const query = (diagnosticsMatch[1] ?? "").trim().toLowerCase();
    return DIAGNOSTIC_SECTIONS.filter((item) => fuzzyMatch(item.command, query));
  }
  const browserMatch = trimmed.match(/^\/browser(?:\s+([\s\S]*))?$/i);
  if (browserMatch) {
    const query = (browserMatch[1] ?? "").trim().toLowerCase();
    return BROWSER_SUBCOMMANDS.filter((item) => item.command.startsWith(query));
  }
  const computerMatch = trimmed.match(/^\/computer(?:\s+([\s\S]*))?$/i);
  if (computerMatch && (trimmed.toLowerCase() === "/computer" || trimmed === "/computer " || computerMatch[1] !== undefined)) {
    const query = (computerMatch[1] ?? "").trim().toLowerCase();
    return COMPUTER_SUBCOMMANDS
      .filter((item) => item.command.startsWith(query))
      .map((item) => ({ ...item, group: "COMPUTER" }));
  }
  const maintenanceMatch = trimmed.match(/^\/maintenance(?:\s+([\s\S]*))?$/i);
  if (maintenanceMatch) {
    const query = (maintenanceMatch[1] ?? "").trim().toLowerCase();
    if (/^(?:preview|apply)\s+\d+$/.test(query)) return [];
    return MAINTENANCE_ACTIONS.filter((item) => fuzzyMatch(item.command, query));
  }
  const barMatch = trimmed.match(/^\/bar(?:\s+([\s\S]*))?$/i);
  if (barMatch && (trimmed.toLowerCase() === "/bar" || trimmed === "/bar " || barMatch[1] !== undefined)) {
    const rawArgs = barMatch[1] ?? "";
    const outputMatch = rawArgs.match(/^output(?:\s+([\s\S]*))?$/i);
    if (outputMatch) {
      const query = (outputMatch[1] ?? "").trim().toLowerCase();
      const back: SlashCommandSuggestion = {
        command: "back",
        description: "return to bar actions",
        completion: "/bar ",
        kind: "submenu",
        group: "BAR OUTPUT",
      };
      const modes = BAR_OUTPUT_OPTIONS
        .filter((item) => item.command.startsWith(query))
        .map((item) => ({ ...item, group: "BAR OUTPUT" }));
      return query ? modes : [back, ...modes];
    }
    const sessionMatch = rawArgs.match(/^sessions(?:\s+([\s\S]*))?$/i);
    if (sessionMatch) {
      const query = (sessionMatch[1] ?? "").trim().toLowerCase();
      const back: SlashCommandSuggestion = {
        command: "back",
        description: "return to bar actions",
        completion: "/bar ",
        kind: "submenu",
        group: "BAR SESSIONS",
      };
      const sessions = (context.barSessions ?? [])
        .filter((session) => session.name.toLowerCase().startsWith(query))
        .map((session) => ({
          command: session.name,
          description: `${session.current ? "current · " : ""}${session.messages} msgs · isolated bar session`,
          completion: `/bar ${session.name}`,
          submitValue: `/bar ${session.name}`,
          kind: "session" as const,
          group: "BAR SESSIONS",
          current: session.current,
        }));
      return query ? sessions : [back, ...sessions];
    }
    const query = rawArgs.trim().toLowerCase();
    const actions = BAR_SUBCOMMANDS.map((item) => ({ ...item, group: "BAR MODE" }));
    return actions.filter((item) => item.command.toLowerCase().startsWith(query));
  }
  const minimalMatch = trimmed.match(/^\/minimal(?:\s+([\s\S]*))?$/i);
  if (minimalMatch && (trimmed.toLowerCase() === "/minimal" || trimmed === "/minimal " || minimalMatch[1] !== undefined)) {
    const rawArgs = minimalMatch[1] ?? "";
    const sessionMatch = rawArgs.match(/^sessions(?:\s+([\s\S]*))?$/i);
    if (sessionMatch) {
      const query = (sessionMatch[1] ?? "").trim().toLowerCase();
      const back: SlashCommandSuggestion = {
        command: "back",
        description: "return to minimal actions",
        completion: "/minimal ",
        kind: "submenu",
        group: "MINIMAL SESSIONS",
      };
      const sessions = (context.minimalSessions ?? [])
        .filter((session) => session.name.toLowerCase().startsWith(query))
        .map((session) => ({
          command: session.name,
          description: `${session.current ? "current · " : ""}${session.messages} msgs · isolated minimal session`,
          completion: `/minimal ${session.name}`,
          submitValue: `/minimal ${session.name}`,
          kind: "session" as const,
          group: "MINIMAL SESSIONS",
          current: session.current,
        }));
      return query ? sessions : [back, ...sessions];
    }
    const query = rawArgs.trim().toLowerCase();
    const actions = MINIMAL_SUBCOMMANDS.map((item) => ({ ...item, group: "MINIMAL MODE" }));
    return actions.filter((item) => item.command.toLowerCase().startsWith(query));
  }
  const local = context.localMode;
  if (local && (trimmed.toLowerCase() === local.command || trimmed.toLowerCase().startsWith(local.command + " "))) {
    const rawArgs = trimmed.slice(local.command.length).trimStart();
    const sessionMatch = rawArgs.match(/^sessions(?:\s+([\s\S]*))?$/i);
    if (sessionMatch) {
      const query = (sessionMatch[1] ?? "").trim().toLowerCase();
      const back: SlashCommandSuggestion = {
        command: "back",
        description: `return to ${local.label.toLowerCase()} actions`,
        completion: `${local.command} `,
        kind: "submenu",
        group: `${local.label.toUpperCase()} SESSIONS`,
      };
      const sessions = (context.localSessions ?? [])
        .filter((session) => session.name.toLowerCase().startsWith(query))
        .map((session) => ({
          command: session.name,
          description: `${session.current ? "current · " : ""}${session.messages} msgs · isolated session`,
          completion: `${local.command} ${session.name}`,
          submitValue: `${local.command} ${session.name}`,
          kind: "session" as const,
          group: `${local.label.toUpperCase()} SESSIONS`,
          current: session.current,
        }));
      return query ? sessions : [back, ...sessions];
    }
    const query = rawArgs.trim().toLowerCase();
    const actions = localSubcommands(local);
    return actions.filter((item) => item.command.toLowerCase().startsWith(query));
  }
  const modelMatch = trimmed.match(/^\/model(?:\s+([\s\S]*))?$/i);
  if (modelMatch && (trimmed.toLowerCase() === "/model" || trimmed === "/model " || modelMatch[1] !== undefined)) {
    const raw = (modelMatch[1] ?? "").trim();
    const query = raw.toLowerCase();
    const modelItem = (model: ModelMenuItem, group = "MODELS"): SlashCommandSuggestion => ({
      command: model.name,
      description: (context.providers === undefined
        ? [model.current ? "current" : "", model.provider ?? "", model.endpoint ?? "available"]
        : [model.current ? "current" : "", model.source ?? "preset",
           model.metadata_known === false ? "capabilities unknown" : "", model.provider ?? ""])
        .filter(Boolean).join(" · "),
      completion: `/model ${model.key ?? model.name}`, submitValue: `/model ${model.key ?? model.name}`,
      kind: "command", group, current: model.current,
    });
    if (context.providers !== undefined) {
      const split = raw.indexOf("::");
      if (split >= 0) {
        const id = raw.slice(0, split);
        const filter = raw.slice(split + 2).trim().toLowerCase();
        const provider = context.providers.find(p => p.id === id);
        const matching = models.filter(m => (m.provider_id ?? m.key?.split("::")[0]) === id
          && m.name.toLowerCase().includes(filter));
        const items = matching.map(m => modelItem(m));
        const manual = raw.slice(split + 2).trim();
        if (manual && !matching.some(m => m.name === manual)) items.push({
          command: `Use model ID: ${manual}`, description: "Unlisted model · conservative defaults",
          submitValue: `/model ${id}::${manual}`, completion: `/model ${id}::${manual}`, kind: "command", group: "MANUAL",
        });
        if (!filter) items.push({ command: "Type a model ID…", description: "Type after :: to search or enter an unlisted ID",
          completion: `/model ${id}:: `, kind: "submenu", group: "MODELS" });
        return [...items, { command: "Refresh model list", description: provider?.error || `${provider?.source ?? "preset"} · query this provider`,
          submitValue: `/model-refresh ${id}`, kind: "command", group: "PROVIDER" },
          { command: "Back to providers", description: "Choose another provider", completion: "/model ", kind: "submenu", group: "PROVIDER" }];
      }
      const providers: SlashCommandSuggestion[] = context.providers.filter(p => p.connected &&
        [p.label, p.id].some(v => v.toLowerCase().includes(query))).map(p => ({
        command: p.label, description: `${p.count} models · ${p.source}${p.error ? ` · ${p.error}` : ""}`,
        completion: `/model ${p.id}::`, kind: "submenu", group: "PROVIDERS",
      }));
      const recent = (context.recentModels ?? []).map(k => models.find(m => m.key === k))
        .filter((m): m is ModelMenuItem => !!m && (!query || m.name.toLowerCase().includes(query)))
        .map(m => modelItem(m, "RECENT"));
      // Typed model names still work directly, without forcing navigation.
      const matches = query ? models.filter(m => [m.name, m.key].some(v => v?.toLowerCase().includes(query)))
        .map(m => modelItem(m)) : [];
      return [...providers, ...recent, ...matches, { command: "Connect a provider…",
        description: "Add or update an API connection", submitValue: "/connect", kind: "command", group: "CONNECT" }];
    }
    return models.filter(m => [m.name, m.key, m.provider].some(v => v?.toLowerCase().includes(query)))
      .map(m => modelItem(m));
  }

  const personaMatch = trimmed.match(/^\/persona(?:\s+([\s\S]*))?$/i);
  if (personaMatch && (trimmed.toLowerCase() === "/persona" || trimmed === "/persona " || personaMatch[1] !== undefined)) {
    const query = (personaMatch[1] ?? "").trim().toLowerCase();
    const dynamic: SlashCommandSuggestion[] = (context.personas ?? []).map((persona) => ({
      command: persona.name,
      description: persona.description,
      completion: `/persona ${persona.name}`,
      submitValue: `/persona ${persona.name}`,
      kind: "command",
    }));
    const source = dynamic.length > 0 ? dynamic : PERSONA_PROFILES;
    const prefixMatches = source.filter((persona) => persona.command.toLowerCase().startsWith(query));
    const matches = prefixMatches.length > 0
      ? prefixMatches
      : source.filter((persona) => fuzzyMatch(persona.command, query));
    return matches.map((persona) => ({ ...persona, group: "PERSONAS" }));
  }

  const searchMatch = trimmed.match(/^\/search(?:\s+([\s\S]*))?$/i);
  if (searchMatch && (trimmed.toLowerCase() === "/search" || trimmed === "/search " || searchMatch[1] !== undefined)) {
    const query = (searchMatch[1] ?? "").trim().toLowerCase();
    return SEARCH_PROVIDERS.filter((provider) => provider.command.startsWith(query)).map((provider) => ({ ...provider, group: "SEARCH" }));
  }
  const sandboxMatch = trimmed.match(/^\/sandbox(?:\s+([\s\S]*))?$/i);
  if (sandboxMatch && (trimmed.toLowerCase() === "/sandbox" || trimmed === "/sandbox " || sandboxMatch[1] !== undefined)) {
    const query = (sandboxMatch[1] ?? "").trim().toLowerCase();
    return SANDBOX_MODES.filter((mode) => mode.command.startsWith(query)).map((mode) => ({ ...mode, group: "SANDBOX" }));
  }
  const visionTilesMatch = trimmed.match(/^\/vision-tiles(?:\s+([\s\S]*))?$/i);
  if (visionTilesMatch && (trimmed.toLowerCase() === "/vision-tiles" || trimmed === "/vision-tiles " || visionTilesMatch[1] !== undefined)) {
    const query = (visionTilesMatch[1] ?? "").trim().toLowerCase();
    return VISION_TILE_OPTIONS.filter((item) => item.command.startsWith(query)).map((item) => ({ ...item, group: "VISION TILES" }));
  }
  const contextIndexMatch = trimmed.match(/^\/context-index(?:\s+([\s\S]*))?$/i);
  if (contextIndexMatch && (trimmed.toLowerCase() === "/context-index" || trimmed === "/context-index " || contextIndexMatch[1] !== undefined)) {
    const query = (contextIndexMatch[1] ?? "").trim().toLowerCase();
    return CONTEXT_INDEX_OPTIONS.filter((item) => item.command.startsWith(query)).map((item) => ({ ...item, group: "CONTEXT INDEX" }));
  }
  const modeMatch = trimmed.match(/^\/mode(?:\s+([\s\S]*))?$/i);
  if (modeMatch && (trimmed.toLowerCase() === "/mode" || trimmed === "/mode " || modeMatch[1] !== undefined)) {
    const query = (modeMatch[1] ?? "").trim().toLowerCase();
    return MODE_OPTIONS.filter((mode) => mode.command.startsWith(query)).map((mode) => ({ ...mode, group: "MODE" }));
  }
  const themeMatch = trimmed.match(/^\/theme(?:\s+([\s\S]*))?$/i);
  if (themeMatch && (trimmed.toLowerCase() === "/theme" || trimmed === "/theme " || themeMatch[1] !== undefined)) {
    const query = (themeMatch[1] ?? "").trim().toLowerCase();
    return THEME_OPTIONS.filter((theme) => fuzzyMatch(theme.command, query)).map((theme) => ({
      ...theme,
      current: theme.command === context.themeName,
      description: `${theme.command === context.themeName ? "current · " : ""}${theme.description}`,
    }));
  }
  const timelineMatch = trimmed.match(/^\/timeline(?:\s+([\s\S]*))?$/i);
  if (timelineMatch && (trimmed.toLowerCase() === "/timeline" || trimmed === "/timeline " || timelineMatch[1] !== undefined)) {
    const query = (timelineMatch[1] ?? "").trim().toLowerCase();
    return TIMELINE_OPTIONS.filter((item) => item.command.startsWith(query));
  }
  const memoryMatch = trimmed.match(/^\/memory(?:\s+([\s\S]*))?$/i);
  if (memoryMatch && (trimmed.toLowerCase() === "/memory" || trimmed === "/memory " || memoryMatch[1] !== undefined)) {
    const query = (memoryMatch[1] ?? "").trim().toLowerCase();
    return MEMORY_SUBCOMMANDS.filter((item) => item.command.startsWith(query)).map((item) => ({ ...item, group: "MEMORY" }));
  }
  const skillsMatch = trimmed.match(/^\/skills(?:\s+([\s\S]*))?$/i);
  if (skillsMatch && (trimmed.toLowerCase() === "/skills" || trimmed === "/skills " || skillsMatch[1] !== undefined)) {
    const query = (skillsMatch[1] ?? "").trim().toLowerCase();
    return SKILL_SUBCOMMANDS.filter((item) => item.command.startsWith(query)).map((item) => ({ ...item, group: "SKILLS" }));
  }
  const learnMatch = trimmed.match(/^\/learn(?:\s+([\s\S]*))?$/i);
  if (learnMatch && (trimmed.toLowerCase() === "/learn" || trimmed === "/learn " || learnMatch[1] !== undefined)) {
    const query = (learnMatch[1] ?? "").trim().toLowerCase();
    return LEARN_SUBCOMMANDS.filter((item) => item.command.startsWith(query)).map((item) => ({ ...item, group: "LEARNING" }));
  }
  const conclaveMatch = trimmed.match(/^\/conclave(?:\s+([\s\S]*))?$/i);
  if (conclaveMatch && (trimmed.toLowerCase() === "/conclave" || trimmed === "/conclave " || conclaveMatch[1] !== undefined)) {
    const query = (conclaveMatch[1] ?? "").trim().toLowerCase();
    const subcommands = CONCLAVE_SUBCOMMANDS.filter((item) => item.command.startsWith(query)).map((item) => ({ ...item, group: "CONCLAVE" }));
    // If user is at config chairperson/expert level, also show available models
    const modelMatch = query.match(/^config (chairperson)\s*(.*)/i);
    if (modelMatch) {
      const modelQuery = modelMatch[2].toLowerCase();
      const modelItems: SlashCommandSuggestion[] = models
        .filter((m) => m.name.toLowerCase().includes(modelQuery) || (m.provider ?? "").toLowerCase().includes(modelQuery))
        .map((m) => ({
          command: m.name,
          description: `${m.provider ?? "?"} · ${m.endpoint ?? ""}`,
          completion: `/conclave config ${modelMatch[1]} ${m.key ?? m.name}`,
          submitValue: `/conclave config ${modelMatch[1]} ${m.key ?? m.name}`,
          kind: "command",
          group: "MODELS",
        }));
      return [...subcommands, ...modelItems];
    }
    // If user is at config expert_list level, show expert names
    const expertListMatch = query.match(/^config expert_list\s*(.*)/i);
    if (expertListMatch) {
      // Only filter by the part after the last comma (the one being typed)
      const fullText = expertListMatch[1] || "";
      const parts = fullText.split(",");
      const currentQuery = parts[parts.length - 1].trim().toLowerCase();
      const expertItems: SlashCommandSuggestion[] = EXPERT_ITEMS
        .filter((e) => e.command.toLowerCase().includes(currentQuery))
        .map((e) => ({ ...e, group: "EXPERTS" }));
      // If already has experts selected, hide subcommands — only show experts for next Tab
      if (fullText.trim()) {
        return expertItems;
      }
      return [...subcommands, ...expertItems];
    }
    return subcommands;
  }

  const sessionMatch = trimmed.match(/^\/session(?:\s+([\s\S]*))?$/i);
  if (sessionMatch && (trimmed.toLowerCase() === "/session" || trimmed === "/session " || sessionMatch[1] !== undefined)) {
    const query = (sessionMatch[1] ?? "").trim().toLowerCase();
    const subcommands = SESSION_SUBCOMMANDS.filter((item) => item.command.startsWith(query)).map((item) => ({ ...item, group: "ACTIONS" }));
    const sessionItems: SlashCommandSuggestion[] = sessions
      .filter((session) => session.name.toLowerCase().startsWith(query))
      .map((session) => ({
        command: session.name,
        description: `${session.messages} msgs${session.current ? " · current" : ""}`,
        completion: `/session ${session.name}`,
        submitValue: `/session ${session.name}`,
        kind: "session" as const,
        group: "SESSIONS",
        current: session.current,
      }));
    return [...subcommands, ...sessionItems];
  }
  const query = trimmed.split(/\s+/, 1)[0].toLowerCase();
  if (!query) return [];
  const commands = [...SLASH_COMMANDS];
  if (context.localMode) {
    commands.splice(commands.findIndex((item) => item.command === "/minimal") + 1, 0,
      { command: context.localMode.command, description: context.localMode.description, takesArgs: true, group: "CHAT" });
  }
  const prefixMatches = commands.filter((item) => item.command.startsWith(query));
  const matches = prefixMatches.length > 0 ? prefixMatches : commands.filter((item) => fuzzyMatch(item.command, query));
  return matches
    .map((item) => ({
      ...item,
      completion: item.command + (item.takesArgs ? " " : ""),
      submitValue: item.command,
      kind: "command",
    }));
}

export function normalizeSelectedCommandIndex(index: number, count: number): number {
  if (count <= 0) return 0;
  return ((index % count) + count) % count;
}

export function visibleSuggestionWindow<T>(items: T[], selectedIndex: number, maxRows: number): T[] {
  const rows = Math.max(1, Math.floor(maxRows));
  if (items.length <= rows) return items;
  const selected = normalizeSelectedCommandIndex(selectedIndex, items.length);
  const half = Math.floor(rows / 2);
  const start = Math.max(0, Math.min(selected - half, items.length - rows));
  return items.slice(start, start + rows);
}

type GroupedSuggestion = { group?: string };

export function groupedSuggestionRowCount(items: GroupedSuggestion[]): number {
  let rows = 1;
  let previousGroup: string | undefined;
  for (const item of items) {
    rows += 1;
    if (item.group && item.group !== previousGroup) rows += 1;
    previousGroup = item.group;
  }
  return rows;
}

export function visibleGroupedSuggestionWindow<T extends GroupedSuggestion>(
  items: T[],
  selectedIndex: number,
  maxRows: number,
): T[] {
  if (items.length === 0) return [];

  const selected = normalizeSelectedCommandIndex(selectedIndex, items.length);
  const rowBudget = Math.max(1, Math.floor(maxRows));
  let start = selected;
  let end = selected + 1;
  let preferLeft = true;

  while (start > 0 || end < items.length) {
    const candidates = preferLeft
      ? [{ start: start - 1, end }, { start, end: end + 1 }]
      : [{ start, end: end + 1 }, { start: start - 1, end }];
    const next = candidates.find((candidate) => (
      candidate.start >= 0
      && candidate.end <= items.length
      && groupedSuggestionRowCount(items.slice(candidate.start, candidate.end)) <= rowBudget
    ));
    if (!next) break;
    start = next.start;
    end = next.end;
    preferLeft = !preferLeft;
  }

  return items.slice(start, end);
}

export function completeSlashCommand(
  input: string,
  selectedIndex: number,
  sessions: SessionMenuItem[] = [],
  models: ModelMenuItem[] = [],
  context: CommandMenuContext = {},
): string | null {
  const suggestions = slashCommandSuggestions(input, sessions, models, context);
  if (suggestions.length === 0) return null;
  const selected = suggestions[normalizeSelectedCommandIndex(selectedIndex, suggestions.length)];
  // Expert list: append name + ", " instead of replacing entire input
  if (selected.group === "EXPERTS" && /^\/conclave config expert_list\s/i.test(input)) {
    const base = input.endsWith(" ") ? input : input + " ";
    return base + selected.command + ", ";
  }
  return selected.completion ?? selected.command;
}

export function resolveSlashCommandSubmission(
  input: string,
  selectedIndex: number,
  sessions: SessionMenuItem[] = [],
  models: ModelMenuItem[] = [],
  context: CommandMenuContext = {},
): SlashCommandSubmission {
  const suggestions = slashCommandSuggestions(input, sessions, models, context);
  if (suggestions.length === 0) return { kind: "none" };
  const selected = suggestions[normalizeSelectedCommandIndex(selectedIndex, suggestions.length)];
  const commandInput = input.trimStart();
  const firstToken = commandInput.split(/\s+/, 1)[0]?.toLowerCase() ?? "";
  const isExactRoot = selected.command.startsWith("/")
    && firstToken === selected.command.toLowerCase();
  if (isExactRoot) {
    const argumentSuffix = commandInput.slice(firstToken.length);
    const argumentText = argumentSuffix.trim();
    if (!argumentText && selected.argsRequired) {
      return { kind: "blocked", message: selected.usage ?? `Usage: ${selected.command}` };
    }
    return { kind: "submit", value: selected.command + argumentSuffix };
  }
  const value = selected.submitValue ?? null;
  return value ? { kind: "submit", value } : { kind: "none" };
}

export function submitSlashCommand(
  input: string,
  selectedIndex: number,
  sessions: SessionMenuItem[] = [],
  models: ModelMenuItem[] = [],
  context: CommandMenuContext = {},
): string | null {
  const result = resolveSlashCommandSubmission(input, selectedIndex, sessions, models, context);
  return result.kind === "submit" ? result.value : null;
}
