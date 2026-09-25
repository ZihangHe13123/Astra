import type { ProviderInfo, ConnectionRoute } from "./types.js";
import { ConnectionPanel } from "./components/connection-panel.js";
import { appshotRejectionNotice } from "./appshot-rejection.js";
import { openImageGallery } from "./image-gallery.js";
import type { AppshotConsumer, AppshotDraftState } from "./appshot-client.js";
import type { AppshotManifestReader } from "./appshot-input.js";
import type { GenerationProgress, GenerationStats, InputSubmission, LocalModeDefinition } from "./types.js";
import { generationProgressLabel } from "./generation-progress.js";
import { COMPACTION_LABEL, compactionNotice } from "./context-compaction.js";
import { formatTurnChangesSummary, TurnChangesRenderGate } from "./turn-changes.js";
import { createBackendEventReceiver, EventDeliveryState, writeProtocolDiagnostic } from "./backend-protocol.js";
import { RuntimeTiming } from "./runtime-timing.js";
import { ControlledBackendRestart } from "./controlled-restart.js";
import { randomUUID } from "node:crypto";
import { createTextEventBatcher } from "./text-event-batcher.js";
import { useTerminalSize } from "./terminal-size.js";
import type { TuiLifecycle } from "./tui-lifecycle.js";
import { createBackendHandshake } from "./backend-handshake.js";
import { AppshotClient } from "./appshot-client.js";
import { productionWindowsAppshotDependencies } from "./appshot-windows.js";
import { startAppshotFocusMonitor } from "./appshot-focus.js";
import { runAppshotCommand } from "./appshot-commands.js";
// App — scrollback-first Ink UI.
// Windows Terminal reliably supports mouse-wheel scrolling in the native
// terminal scrollback. We therefore append to terminal scrollback with <Static>
// and keep live items append-only, as required by Ink's Static cursor. Session
// restoration is capped separately so reopening a large chat stays responsive.
import React, { useState, useEffect, useLayoutEffect, useCallback, useReducer, useRef, useMemo, memo } from "react";
import { Text, Box, Static, useInput, useStdout } from "ink";
import stringWidth from "string-width";
import { spawn } from "child_process";
import { createInterface } from "readline";
import { InputBar, type AppshotInputHandle } from "./components/input-bar.js";
import { ActivityDock, filterQueuedApprovalTools, formatToolElapsed, type ActiveTool, type PendingApprovalSummary } from "./components/activity-dock.js";
import { mergeComputerStateEvent, type ComputerUiState } from "./components/status-bar.js";
import { AppHeader, responsiveMode } from "./components/app-header.js";
import { StartupScreen } from "./components/startup-screen.js";
import { WelcomeScreen } from "./components/welcome-screen.js";
import { ToolApprovalPanel } from "./components/tool-approval-panel.js";
import { QuestionCard } from "./components/question-card.js";
import { BarShelf } from "./components/bar-shelf.js";
import { BarHeader, BarStatusDock, isQuietBarAction } from "./components/bar-chrome.js";
import { MinimalStatusDock } from "./components/minimal-chrome.js";
import { LocalStatusDock } from "./components/local-chrome.js";
import { StreamingControlLayout } from "./components/streaming-control-layout.js";
import { formatImageInputDisplay, parseImageInput } from "./image-command.js";
import { isBenignMacOSAllocatorDiagnostic, isBackendModelProgress } from "./backend-stderr.js";
import type { ModelMenuItem, SessionMenuItem } from "./command-menu.js";
import { startsConversationCommand } from "./conversation-commands.js";
import type {
  BarAmbianceState,
  BarDrinkState,
  BarShiftState,
  ChatMessage,
  LyraGlassState,
  PyEvent,
  TaskInfo,
  ToolApprovalRequest,
  TuiCommand,
  WorkingMemory,
  StartupInfo,
  RuntimeMode,
  UserQuestionAnswer,
  UserQuestionRequest,
  VisionPreprocessEvent,
  ComputerStateEvent,
} from "./types.js";
import { loadThemeName, saveThemeName, THEMES, THEME_NAMES, isThemeName } from "./theme.js";
import { loadTimelineDisplay, saveTimelineDisplay } from "./tui-settings.js";
import {
  resolveTimelineCommand,
  shouldMessageUseTimeline,
  shouldResetTimelineCursor,
} from "./timeline-display.js";
import { ThemeProvider, useTheme } from "./theme-context.js";
import {
  INLINE_CODE_DELIMITER,
  MALFORMED_MATH_PREFIX,
  markdownBlocksToLines,
  type RenderLine,
} from "./markdown-render-lines.js";
import { parseCompleteMarkdown, type InlineSpan } from "./terminal-markdown.js";
import { formatTerminalHyperlink } from "./terminal-hyperlink.js";
import {
  StreamingMarkdownCoordinator,
  type StreamingRole,
  type StreamingUpdate,
} from "./streaming-markdown.js";
import {
  createStreamingDisplayState,
  createStreamingDisplayBatcher,
  visibleDynamicDisplayLines,
  streamingDisplayReducer,
} from "./streaming-display-state.js";
import { reduceAgentTeamEvent, type AgentTeamView } from "./agent-team-state.js";
import { reduceQuestionProtocolState } from "./question-protocol-state.js";
import { restoreRecentHistory } from "./static-history.js";
import {
  clampToolDetailOffset,
  shouldCollapseToolResult,
  summarizeToolResult,
  toolResultBody,
  wrapToolResult,
  type ToolResultRecord,
} from "./tool-results.js";
import {
  approvalRequestLines,
  escapeApprovalDisplayText,
} from "./approval-preview.js";
import { transitionApprovalInput } from "./approval-interaction.js";
import { formatQuestionAnswerSummary } from "./question-interaction.js";
import {
  MessageTimeRailCursor,
  compactRailSeparator,
  compactRolePrefix,
  stripLegacyAssistantTimeMarker,
  type TimeRail,
  type TimeRailLayout,
} from "./message-time-rail.js";

type Role = ChatMessage["role"];

type AddMessageOptions = {
  toolCompletion?: boolean;
  timestamp?: number;
};

export type { RenderLine } from "./markdown-render-lines.js";

export const MESSAGE_TIME_RAIL_WIDTH = 5 + Math.max(
  ...THEME_NAMES.map((name) => stringWidth(compactRailSeparator(
    THEMES[name].chrome?.separator ?? " │ ",
  ))),
);

function roleColor(role: Role, theme: ReturnType<typeof useTheme>): string {
  return {
    user: theme.accent,
    assistant: theme.text,
    reasoning: theme.muted,
    tool: theme.accentAlt,
    error: theme.danger,
    system: theme.success,
  }[role];
}

const rolePrefix: Record<Role, string> = {
  user: "you  ",
  assistant: "lyra ",
  reasoning: "think",
  tool: "tool ",
  error: "err  ",
  system: "sys  ",
};

const minimalRolePrefix: Record<Role, string> = {
  user: "you  ",
  assistant: "assistant ",
  reasoning: "think ",
  tool: "tool  ",
  error: "error ",
  system: "system ",
};

const minimalRoleLabel: Record<Role, string> = {
  user: "YOU  > ",
  assistant: "ASSISTANT > ",
  reasoning: "THINK > ",
  tool: "TOOL  > ",
  error: "ERROR > ",
  system: "SYSTEM > ",
};

function rolePrefixForMode(role: Role, mode: RuntimeMode): string {
  return mode === "minimal" ? minimalRolePrefix[role] : rolePrefix[role];
}

const CONVERSATION_ROLES: Role[] = ["user", "reasoning", "assistant", "tool"];

function isRoleStartPrefix(role: Role, prefix: string): boolean {
  return prefix === rolePrefix[role] || prefix === minimalRolePrefix[role];
}

export function messageRolePrefixWidth(role: Role, runtimeMode: RuntimeMode): number {
  if (runtimeMode === "minimal") {
    return stringWidth(compactRolePrefix(minimalRoleLabel[role]));
  }
  if (!CONVERSATION_ROLES.includes(role)) {
    return stringWidth(rolePrefixForMode(role, runtimeMode));
  }
  return Math.max(...THEME_NAMES.map((name) => stringWidth(compactRolePrefix(
    THEMES[name].chrome?.rolePrefixes?.[role] ?? rolePrefix[role],
  ))));
}

// Keep session restore bounded, but never trim the live array passed to Ink
// <Static>: Static discovers new output from items.length, so shifting old
// entries at a fixed length makes rendering stop at the boundary.
const MAX_RESTORED_RENDER_LINES = Math.max(
  200,
  Number(process.env.TUI_MAX_RESTORED_LINES ?? process.env.TUI_MAX_RENDER_LINES ?? "2500") || 2500,
);
const TOOL_COLLAPSE_CHARS = Math.max(120, Number(process.env.TUI_TOOL_COLLAPSE_CHARS ?? "700") || 700);
const TOOL_COLLAPSE_LINES = Math.max(2, Number(process.env.TUI_TOOL_COLLAPSE_LINES ?? "6") || 6);
const MAX_TOOL_RESULTS = Math.max(10, Number(process.env.TUI_MAX_TOOL_RESULTS ?? "100") || 100);
const ENTER_ALTERNATE_SCREEN = "\x1b[?1049h\x1b[2J\x1b[H";
const LEAVE_ALTERNATE_SCREEN = "\x1b[?1049l";

export function formatQuestionAnswerDisplaySummary(
  request: UserQuestionRequest,
  answers: UserQuestionAnswer[],
): string {
  return escapeApprovalDisplayText(formatQuestionAnswerSummary(request, answers));
}

function InlineSpans({ spans, color, dim }: { spans: InlineSpan[]; color: string; dim?: boolean }) {
  const theme = useTheme();
  return (
    <>
      {spans.map((span, index) => {
        if (span.kind === "strong") {
          return <Text key={index} bold={theme.strongBold} color={theme.strongUsesBaseColor ? color : theme.emphasis} dimColor={dim}>{span.text}</Text>;
        }
        if (span.kind === "code") {
          const value = theme.codeDelimiters
            ? `${INLINE_CODE_DELIMITER}${span.text}${INLINE_CODE_DELIMITER}`
            : span.text;
          return <Text key={index} color={theme.code} backgroundColor={theme.codeBackground} dimColor={dim}>{value}</Text>;
        }
        if (span.kind === "emphasis") {
          return <Text key={index} italic color={theme.muted} dimColor={dim}>{span.text}</Text>;
        }
        if (span.kind === "strikethrough") {
          return <Text key={index} strikethrough color={color} dimColor={dim}>{span.text}</Text>;
        }
        if (span.kind === "math") {
          return <Text key={index} color={span.malformed ? theme.warning : theme.accentAlt}>{span.malformed ? MALFORMED_MATH_PREFIX : ""}{span.text}</Text>;
        }
        if (span.kind === "link") {
          return (
            <Text key={index} color={theme.accentAlt} underline dimColor={dim}>
              {formatTerminalHyperlink(span.text, span.href)}
            </Text>
          );
        }
        return <Text key={index} color={color} dimColor={dim}>{span.text}</Text>;
      })}
    </>
  );
}

function InlineLineContent({
  line,
  color,
  dim,
}: {
  line: RenderLine;
  color: string;
  dim?: boolean;
}) {
  const spans = line.spans ?? [{ kind: "text" as const, text: line.text || " " }];
  return <InlineSpans spans={spans} color={color} dim={dim} />;
}

export function messageToLines(
  msg: ChatMessage,
  width: number,
  showReasoning: boolean,
  runtimeMode: RuntimeMode = "work",
  timeRail?: TimeRail,
): RenderLine[] {
  if (msg.role === "reasoning" && !showReasoning) return [];
  const content = msg.role === "assistant"
    ? stripLegacyAssistantTimeMarker(msg.content)
    : msg.content;
  const bodyWidth = Math.max(30, width - 8);
  const result = markdownBlocksToLines(parseCompleteMarkdown(content), {
    role: msg.role,
    baseKey: String(msg.id ?? msg.timestamp),
    bodyWidth,
    firstPrefix: rolePrefixForMode(msg.role, runtimeMode),
    continuationPrefix: "    ",
    timeRail,
  });
  return [
    ...result.lines,
    {
      key: `${msg.id ?? msg.timestamp}-gap`,
      role: "system",
      prefix: "    ",
      text: " ",
      kind: "text",
      spans: [],
    },
  ];
}

export function streamingUpdateToRenderLines(
  role: StreamingRole,
  update: StreamingUpdate,
  columns: number,
  runtimeMode: RuntimeMode,
  committedBaseKey: string,
) {
  const prefix = rolePrefixForMode(role, runtimeMode);
  const bodyWidth = Math.max(30, columns - 8);
  const committed = markdownBlocksToLines(update.committed, {
    role,
    baseKey: committedBaseKey,
    bodyWidth,
    firstPrefix: update.startingLineIndex === 0 ? prefix : "    ",
    continuationPrefix: "    ",
    timeRail: update.timeRail,
    startingLineIndex: update.startingLineIndex,
  });
  const previewStartingLineIndex = update.startingLineIndex + committed.consumedLines;
  const preview = markdownBlocksToLines(update.preview, {
    role,
    baseKey: `preview-${role}`,
    bodyWidth,
    firstPrefix: previewStartingLineIndex === 0 ? prefix : "    ",
    continuationPrefix: "    ",
    timeRail: update.timeRail,
    startingLineIndex: previewStartingLineIndex,
  });
  return { committed, preview };
}

export type SubmitStreamLifecycle = "preserve" | "clear";

export function submitStreamLifecyclePolicy(
  text: string,
  timelineCommand: ReturnType<typeof resolveTimelineCommand> = resolveTimelineCommand(text, false),
): SubmitStreamLifecycle {
  if (timelineCommand !== null || /^\/(?:cancel|theme|restart|wakeup|yolo|help)(?:\s|$)/i.test(text.trim())) {
    return "preserve";
  }
  return "clear";
}

export const MessageLine = memo(function MessageLine({ line, runtimeMode = "work" }: { line: RenderLine; runtimeMode?: RuntimeMode }) {
  const theme = useTheme();
  const color = line.kind === "header" ? theme.header : roleColor(line.role, theme);
  const dim = line.role === "tool";
  const isRoleStart = isRoleStartPrefix(line.role, line.prefix);
  const configuredRolePrefix = runtimeMode === "minimal"
    ? minimalRoleLabel[line.role]
    : theme.chrome?.rolePrefixes?.[line.role] ?? rolePrefix[line.role];
  const compactConfiguredRolePrefix = compactRolePrefix(configuredRolePrefix);
  const isConversationContinuation = Boolean(
    line.timeRail
    && CONVERSATION_ROLES.includes(line.role)
    && !isRoleStart
    && line.prefix.trim() === "",
  );
  const displayedPrefix = line.timeRail && isRoleStart
    ? compactConfiguredRolePrefix
    : isConversationContinuation
      ? " ".repeat(stringWidth(compactConfiguredRolePrefix))
      : line.prefix;
  const displayedSeparator = compactRailSeparator(theme.chrome?.separator ?? " │ ");
  const conversationRoleColor = line.role === "user"
    ? theme.accent
    : line.role === "reasoning"
      ? theme.emphasis
      : theme.accentAlt;
  const lead = (
    <>
      {line.timeRail ? (
        <>
          <Text
            bold={line.timeRail.kind === "absolute"}
            dimColor={line.timeRail.kind === "relative"}
            color={theme.accentAlt}
          >
            {line.timeRail.label.padStart(5, " ")}
          </Text>
          <Text bold color={theme.border}>{displayedSeparator}</Text>
        </>
      ) : null}
      {line.timeRail && isRoleStart ? (
        <Text bold color={conversationRoleColor}>{displayedPrefix}</Text>
      ) : displayedPrefix}
    </>
  );

  if (line.kind === "rule") {
    return <Text dimColor color={theme.subtle}>{lead}{line.text}</Text>;
  }

  if (line.kind === "table") {
    return <Text color={theme.accent}>{lead}{line.text}</Text>;
  }

  if (line.kind === "code") {
    return (
      <Text>
        <Text>{lead}</Text>
        <Text color={theme.codeBlock} backgroundColor={theme.codeBlockBackground}>
          {line.text || " "}
        </Text>
      </Text>
    );
  }

  if (line.kind === "math") {
    return (
      <Text>
        <Text color={theme.accent}>{lead}</Text>
        <Text color={line.malformedMath ? theme.warning : theme.accentAlt}>
          {line.malformedMath ? MALFORMED_MATH_PREFIX : ""}{line.text}
        </Text>
      </Text>
    );
  }

  if (line.kind === "header") {
    return (
      <Text bold color={theme.header}>
        {lead}<InlineLineContent line={line} color={theme.header} />
      </Text>
    );
  }

  if (line.kind === "quote") {
    const quoteDim = line.role !== "reasoning";
    return (
      <Text color={theme.muted} dimColor={quoteDim}>
        {lead}<InlineLineContent line={line} color={theme.muted} dim={quoteDim} />
      </Text>
    );
  }

  if (line.kind === "list") {
    return (
      <Text>
        <Text color={theme.accent}>{lead}</Text>
        <InlineLineContent line={line} color={color} dim={dim} />
      </Text>
    );
  }

  return (
    <Text>
      <Text
        color={line.role === "assistant" ? theme.accent : roleColor(line.role, theme)}
        bold={theme.prefixBold && displayedPrefix.trim().length > 0}
      >{lead}</Text>
      <InlineLineContent line={line} color={color} dim={dim} />
    </Text>
  );
});

const InputBarMemo = memo(InputBar);
const ActivityDockMemo = memo(ActivityDock);
const AppHeaderMemo = memo(AppHeader);
const BarHeaderMemo = memo(BarHeader);
const BarStatusDockMemo = memo(BarStatusDock);
const StartupScreenMemo = memo(StartupScreen);
const WelcomeScreenMemo = memo(WelcomeScreen);

const HistoryOutput = memo(function HistoryOutput({ lines, generation, runtimeMode }: {
  lines: RenderLine[]; generation: string; runtimeMode: RuntimeMode;
}) {
  return <Static key={generation} items={lines}>
    {(line) => <MessageLine key={line.key} line={line} runtimeMode={runtimeMode} />}
  </Static>;
});

function ToolDetailsPanel({
  result,
  lines,
  pageSize,
  offset,
}: {
  result: ToolResultRecord;
  lines: string[];
  pageSize: number;
  offset: number;
}) {
  const theme = useTheme();
  const chrome = theme.chrome;
  const safeOffset = clampToolDetailOffset(offset, lines.length, pageSize);
  const visible = lines.slice(safeOffset, safeOffset + pageSize);
  const end = Math.min(lines.length, safeOffset + pageSize);
  return (
    <Box borderStyle={chrome?.frameStyle ?? "round"} borderColor={theme.accentAlt} paddingX={1} flexDirection="column">
      <Text bold={theme.prefixBold} color={theme.accentAlt}>
        {chrome?.detailTitle ? `${chrome.detailTitle} // ` : ""}Tool #{result.id} · {result.name} · lines {safeOffset + 1}-{end}/{lines.length}
      </Text>
      {visible.map((line, index) => (
        <Text key={`${result.id}-${safeOffset + index}`} color={result.error ? theme.danger : theme.text}>
          {line || " "}
        </Text>
      ))}
      {result.artifactPath && (
        <Text dimColor color={theme.muted}>full result → {result.artifactPath}</Text>
      )}
      <Text dimColor color={theme.muted}>↑/↓ scroll · PgUp/PgDn page · Esc or Ctrl+O close</Text>
    </Box>
  );
}

const DEFAULT_MODEL_LIST: ModelMenuItem[] = [
  { name: "deepseek-flash" },
  { name: "deepseek-v4-pro" },
  { name: "Qwen3.6-35B-A3B" },
  { name: "gemma-4-12B-it" },
];

export function shouldShowWelcome({
  backendStatus,
  runtimeMode,
  historyLineCount,
  openToolResultId,
  busy,
  activeTask,
  activeToolCount,
  activeProcessCount,
  approvalCount,
  commandMenuVisible,
}: {
  backendStatus: "connecting" | "ready" | "disconnected";
  runtimeMode: RuntimeMode;
  historyLineCount: number;
  openToolResultId: number | null;
  busy: boolean;
  activeTask: boolean;
  activeToolCount: number;
  activeProcessCount: number;
  approvalCount: number;
  commandMenuVisible: boolean;
}): boolean {
  return backendStatus === "ready"
    && runtimeMode === "work"
    && historyLineCount === 0
    && openToolResultId === null
    && !busy
    && !activeTask
    && activeToolCount === 0
    && activeProcessCount === 0
    && approvalCount === 0
    && !commandMenuVisible;
}

export function shouldRefreshToolClock({
  activeToolCount,
  activeProcessCount,
  approvalCount,
}: {
  activeToolCount: number;
  activeProcessCount: number;
  approvalCount: number;
}): boolean {
  return (activeToolCount > 0 || activeProcessCount > 0) && approvalCount === 0;
}

export function formatVisionPreprocessMessage(event: VisionPreprocessEvent): string | null {
  return event.message ? `[vision] ${event.message}` : null;
}

export default function App({ appshotClientFactory, appshotManifestReader, lifecycle }: { appshotClientFactory?: (consumer:AppshotConsumer) => AppshotClient; appshotManifestReader?:AppshotManifestReader; lifecycle?: TuiLifecycle } = {}) {
  const appshotInputRef = useRef<AppshotInputHandle>(null);
  const appshotStatusTimerRef = useRef<ReturnType<typeof setTimeout>>();
  const appshotClientRef = useRef<AppshotClient>();
  const [themeName, setThemeName] = useState(loadThemeName);
  const [timelineVisible, setTimelineVisible] = useState(loadTimelineDisplay);
  const theme = THEMES[themeName];
  const [displayState, rawDispatchDisplay] = useReducer(
    streamingDisplayReducer,
    undefined,
    () => createStreamingDisplayState(),
  );
  const [displayBatcher] = useState(() => createStreamingDisplayBatcher(rawDispatchDisplay));
  const dispatchDisplay = displayBatcher.dispatch;
  const historyLines = displayState.historyLines;
  const historyGeneration = displayState.historyGeneration;
  const [busy, setBusy] = useState(false);
  const [backendStatus, setBackendStatus] = useState<"connecting" | "ready" | "disconnected">("connecting");
  const [showReasoning, setShowReasoning] = useState(true);
  const [info, setInfo] = useState({
    model: "deepseek-flash", total: 0, prompt: 0, completion: 0, ctxPct: 0,
    cacheHit: 0, cacheMiss: 0,
    modelKey: "deepseek-flash",
    contextUsed: 0,
    contextLimit: 128_000 as number | undefined,
    reasoningEffort: undefined as "low" | "high" | "xhigh" | "max" | undefined,
    codeMode: undefined as "native" | "code" | "both" | undefined,
    personas: [] as { name: string; description: string }[],
  });
  const [sessionName, setSessionName] = useState("");
  const [runtimeMode, setRuntimeMode] = useState<RuntimeMode>("work");
  const [computer, setComputer] = useState<ComputerUiState>({ active: false, handedOff: false, control: "inactive" });

  const [sessionList, setSessionList] = useState<SessionMenuItem[]>([]);
  const [barSessionList, setBarSessionList] = useState<SessionMenuItem[]>([]);
  const [barSessionName, setBarSessionName] = useState("");
  const [minimalSessionList, setMinimalSessionList] = useState<SessionMenuItem[]>([]);
  const [minimalSessionName, setMinimalSessionName] = useState("");
  const [localSessionList, setLocalSessionList] = useState<SessionMenuItem[]>([]);
  const [localSessionName, setLocalSessionName] = useState("");
  const [localMode, setLocalMode] = useState<LocalModeDefinition | null>(null);
  const [barDrink, setBarDrink] = useState<BarDrinkState>({
    active: false, name: "", note: "", tone: "clear", temperature: "cold", fill: 0,
  });
  const [barAmbiance, setBarAmbiance] = useState<BarAmbianceState>({
    weather: "rain", power: "stable", music: "low_synth", radio: "static",
  });
  const [lyraGlass, setLyraGlass] = useState<LyraGlassState>({
    active: false, name: "", note: "", fill: 0,
  });
  const [barShift, setBarShift] = useState<BarShiftState>({ turn_count: 0, phase: "early" });
  const [barOutputMode, setBarOutputMode] = useState<"atomic" | "stream">("atomic");
  const [barNotice, setBarNotice] = useState("");
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [connectionRoutes, setConnectionRoutes] = useState<ConnectionRoute[]>([]);
  const [recentModelKeys, setRecentModelKeys] = useState<string[]>([]);
  const [connectionOpen, setConnectionOpen] = useState(false);
  const connectionOpenRef = useRef(false);
  const [connectionPending, setConnectionPending] = useState(false);
  const [connectionError, setConnectionError] = useState("");
  const [connectionAuth, setConnectionAuth] = useState<{ verification_uri: string; user_code: string } | null>(null);
  const connectionRequestRef = useRef("");
  const [modelList, setModelList] = useState<ModelMenuItem[]>(DEFAULT_MODEL_LIST);
  const [activeTask, setActiveTask] = useState<TaskInfo | null>(null);
  const activeTaskRef = useRef<TaskInfo | null>(null);
  const updateActiveTask = useCallback((task: TaskInfo | null) => {
    activeTaskRef.current = task;
    setActiveTask(task);
  }, []);
  const [learningReviewStatus, setLearningReviewStatus] = useState<"queued" | "running" | null>(null);
  const [yolo, setYolo] = useState(false);
  const [workingMemory, setWorkingMemory] = useState<WorkingMemory>({});
  const [toolResults, setToolResults] = useState<ToolResultRecord[]>([]);
  const [generationStats, setGenerationStats] = useState<GenerationStats | null>(null);
  const [generationProgress, setGenerationProgress] = useState<GenerationProgress | null>(null);
  const [compacting, setCompacting] = useState(false);
  const generationModelKeyRef = useRef<string>();
  const generationSessionIdRef = useRef<string>();
  const sessionNameRef = useRef("");
  const turnChangesGateRef = useRef<TurnChangesRenderGate | null>(null);
  if (!turnChangesGateRef.current) turnChangesGateRef.current = new TurnChangesRenderGate();
  const [openToolResultId, setOpenToolResultId] = useState<number | null>(null);
  const [toolDetailOffset, setToolDetailOffset] = useState(0);
  const [activeTools, setActiveTools] = useState<ActiveTool[]>([]);
  const [activeProcesses, setActiveProcesses] = useState<ActiveTool[]>([]);
  const [agentTeams, setAgentTeams] = useState<AgentTeamView[]>([]);
  const [toolClock, setToolClock] = useState(Date.now());
  const previousApprovalCountRef = useRef(0);
  const [activityExpanded, setActivityExpanded] = useState(false);
  const [commandMenuVisible, setCommandMenuVisible] = useState(false);
  const [approvalRequests, setApprovalRequests] = useState<ToolApprovalRequest[]>([]);
  const [questionProtocol, setQuestionProtocol] = useState({
    request: null as UserQuestionRequest | null,
    rejection: null as { requestId: string; reason: string } | null,
  });
  const questionRequest = questionProtocol.request;
  const [approvalDetailOpen, setApprovalDetailOpen] = useState(false);
  const [approvalDetailOffset, setApprovalDetailOffset] = useState(0);
  const startupAnimationEnabled = process.env.TUI_STARTUP_ANIMATION !== "0"
    && Boolean(process.stdin.isTTY && (process.stderr.isTTY || process.stdout.isTTY));
  const [startupInfo, setStartupInfo] = useState<StartupInfo | null>(null);
  const [startupVisible, setStartupVisible] = useState(startupAnimationEnabled);

  const { stdout } = useStdout();
  const { columns: terminalColumns, rows } = useTerminalSize(stdout);
  // Keep every rendered line at least one cell away from the right edge.
  // Windows consoles scroll the buffer as soon as the bottom-right cell is
  // written; with scrollback-first output the live full-width border rows can
  // then land on that cell and desynchronize Ink's incremental erase pass,
  // leaking stale border lines into the scrollback.
  const columns = Math.max(20, terminalColumns - 1);
  const toolDetailPageSize = Math.max(5, Math.min(40, rows - 5));
  const approvalPageSize = Math.max(4, Math.min(12, rows - 12));
  const layoutMode = responsiveMode(columns);
  const configuredMenuRows = Math.max(3, Math.floor(Number(process.env.TUI_COMMAND_MENU_ROWS ?? "8") || 8));
  const commandMenuRows = Math.max(3, Math.min(
    configuredMenuRows,
    Math.max(3, rows - (activityExpanded ? 15 : 10)),
    layoutMode === "compact" ? 5 : configuredMenuRows,
  ));
  const showWelcome = shouldShowWelcome({
    backendStatus,
    runtimeMode,
    historyLineCount: historyLines.length,
    openToolResultId,
    busy,
    activeTask: activeTask !== null,
    activeToolCount: activeTools.length,
    activeProcessCount: activeProcesses.length,
    approvalCount: approvalRequests.length,
    commandMenuVisible,
  });

  const compactQuestion = questionRequest !== null && approvalRequests.length === 0 && rows < 20;
  const interactionActive = approvalRequests.length > 0 || questionRequest !== null || connectionOpen;
  const pendingApproval = approvalRequests[0];
  const queuedApprovalCallIds = approvalRequests.map((request) => request.call_id);
  const pendingApprovalSummary: PendingApprovalSummary | undefined = pendingApproval
    ? {
        queueIndex: 1,
        queueTotal: approvalRequests.length,
        toolName: pendingApproval.tool_name,
        status: "awaiting",
      }
    : undefined;
  const dockTools = approvalRequests.length
    ? [
        ...filterQueuedApprovalTools(activeTools, queuedApprovalCallIds),
        ...filterQueuedApprovalTools(activeProcesses, queuedApprovalCallIds),
      ]
    : [...activeTools, ...activeProcesses];

  const procRef = useRef<ReturnType<typeof spawn> | null>(null);
  const eventDeliveryRef = useRef(new EventDeliveryState());
  const runtimeTimingRef = useRef<RuntimeTiming | null>(null);
  const eventScopeRef = useRef(randomUUID());
  const discardPendingTextRef = useRef<() => void>(() => {});
  const backendStartedOnceRef = useRef(false);
  const startBackendRef = useRef<() => void>(() => {});
  const busyRef = useRef(false);
  const msgIdRef = useRef(0);
  const streamCoordinatorRef = useRef(new StreamingMarkdownCoordinator());
  const assistantStartedRef = useRef(false);
  const showReasoningRef = useRef(false);
  const cancelRequestedRef = useRef(false);
  const toolResultIdRef = useRef(0);
  const toolResultsRef = useRef<ToolResultRecord[]>([]);
  const detailOpenRef = useRef(false);
  const timeRailCursorRef = useRef(new MessageTimeRailCursor());
  const timelineVisibleRef = useRef(timelineVisible);

  const railLayout = useCallback((role: Role): TimeRailLayout => ({
    width: MESSAGE_TIME_RAIL_WIDTH,
    prefixWidth: messageRolePrefixWidth(role, runtimeMode),
  }), [runtimeMode]);

  const nextTimeRail = useCallback((role: Role, timestamp: number): TimeRail | undefined => {
    return timeRailCursorRef.current.next(role, timestamp, railLayout(role));
  }, [railLayout]);

  const appendHistoryLines = useCallback((lines: RenderLine[]) => {
    if (!lines.length) return;
    dispatchDisplay({ type: "appendActivity", lines });
  }, []);

  useEffect(() => {
    showReasoningRef.current = showReasoning;
  }, [showReasoning]);

  useEffect(() => {
    if (!barNotice) return;
    const timer = setTimeout(() => setBarNotice(""), 4_000);
    return () => clearTimeout(timer);
  }, [barNotice]);

  useEffect(() => {
    const approvalCount = approvalRequests.length;
    const approvalTransition = previousApprovalCountRef.current !== approvalCount;
    previousApprovalCountRef.current = approvalCount;
    if (!activeTools.length && !activeProcesses.length) return;
    if (!shouldRefreshToolClock({
      activeToolCount: activeTools.length,
      activeProcessCount: activeProcesses.length,
      approvalCount,
    })) return;
    // The freeze check must run before setToolClock. Approval is a modal state:
    // repainting the full-width dock borders while it appears/disappears leaves
    // stale top-border rows in Windows Terminal. Normal tool/process progress
    // keeps the 200ms live clock, but approval transitions skip the immediate
    // forced tick and let the next interval refresh the elapsed display.
    if (!approvalTransition) {
      setToolClock(Date.now());
    }
    const timer = setInterval(() => { if (!lifecycle?.closing) setToolClock(Date.now()); }, 200);
    return () => clearInterval(timer);
  }, [activeProcesses.length, activeTools.length, approvalRequests.length]);

  useEffect(() => {
    if (!startupVisible || !startupInfo) return;
    const timer = setTimeout(() => setStartupVisible(false), 120);
    return () => clearTimeout(timer);
  }, [startupInfo, startupVisible]);

  useLayoutEffect(() => {
    if (openToolResultId === null) return;
    stdout.write(ENTER_ALTERNATE_SCREEN);
    return () => {
      stdout.write(LEAVE_ALTERNATE_SCREEN);
    };
  }, [openToolResultId, stdout]);

  const addMessage = useCallback((
    role: Role,
    content: string,
    options: AddMessageOptions = {},
  ) => {
    if (!content) return;
    const timestamp = options.timestamp ?? Date.now();
    const msg = { role, content, timestamp, id: ++msgIdRef.current } as ChatMessage;
    const useTimeline = shouldMessageUseTimeline(
      role,
      timelineVisibleRef.current,
      options.toolCompletion === true,
    );
    const rail = useTimeline ? nextTimeRail(role, timestamp) : undefined;
    appendHistoryLines(messageToLines(msg, columns, showReasoningRef.current, runtimeMode, rail));
  }, [appendHistoryLines, columns, nextTimeRail, runtimeMode]);

  const applyStreamingUpdate = useCallback((
    role: StreamingRole,
    update: StreamingUpdate,
    advanceState = true,
  ) => {
    const { committed, preview } = streamingUpdateToRenderLines(
      role,
      update,
      columns,
      runtimeMode,
      `stream-${role}-${++msgIdRef.current}`,
    );

    if (advanceState && committed.consumedLines > 0) {
      streamCoordinatorRef.current.advance(role, committed.consumedLines);
    }
    const action = {
      type: "applyStreamUpdate" as const,
      role,
      committed: committed.lines,
      preview: preview.lines,
    };
    if (advanceState) displayBatcher.enqueue(action);
    else dispatchDisplay(action);
  }, [columns, runtimeMode, displayBatcher, dispatchDisplay]);

  const appendStreamChunk = useCallback((role: StreamingRole, chunk: string) => {
    if (role === "reasoning" && !showReasoningRef.current) return;
    if (!streamCoordinatorRef.current.snapshot(role)) {
      const timestamp = Date.now();
      const rail = shouldMessageUseTimeline(role, timelineVisibleRef.current)
        ? nextTimeRail(role, timestamp)
        : undefined;
      streamCoordinatorRef.current.start(role, rail);
    }
    applyStreamingUpdate(role, streamCoordinatorRef.current.push(role, chunk));
  }, [applyStreamingUpdate, nextTimeRail]);

  const flushStreamRole = useCallback((role: StreamingRole) => {
    const update = streamCoordinatorRef.current.finish(role);
    applyStreamingUpdate(role, update, false);
  }, [applyStreamingUpdate]);

  const completeTurn = useCallback(() => {
    try {
      flushStreamRole("reasoning");
    } finally {
      try {
        flushStreamRole("assistant");
      } finally {
        assistantStartedRef.current = false;
      }
    }
  }, [flushStreamRole]);

  const finishTurn = useCallback(() => {
    try {
      completeTurn();
    } finally {
      // Text rendering cannot own task liveness. Always unlock the composer.
      busyRef.current = false;
      setBusy(false);
      setGenerationProgress(null);
      setCompacting(false);
      setActiveTools([]);
      setActiveProcesses([]);
      setApprovalRequests([]);
      setQuestionProtocol({ request: null, rejection: null });
      updateActiveTask(null);
      cancelRequestedRef.current = false;
    }
  }, [completeTurn, updateActiveTask]);

  const clearStreamingDisplay = useCallback(() => {
    discardPendingTextRef.current();
    streamCoordinatorRef.current.clear();
    dispatchDisplay({ type: "clearStreaming" });
    assistantStartedRef.current = false;
  }, []);

  const closeToolDetails = useCallback(() => {
    if (!detailOpenRef.current) return;
    detailOpenRef.current = false;
    dispatchDisplay({ type: "setToolDetailOpen", open: false });
    setOpenToolResultId(null);
    setToolDetailOffset(0);
  }, []);

  const handleEvent = useCallback((event: PyEvent) => {
    const envelope = event as PyEvent & {
      cursor?: number;
      replayed?: boolean;
    };
    switch (event.type) {
      case "message_accepted":
      case "message_rejected":
      case "submission_status": {
        const input = appshotInputRef.current;
        if (input?.snapshot().pending?.submissionId !== event.submission_id) break;
        clearTimeout(appshotStatusTimerRef.current);
        const result = event.type === "message_accepted" ? "accepted"
          : event.type === "message_rejected" ? "rejected" : event.status;
        if (result === "accepted") {
          input.acceptSubmission(event.submission_id);
          if (event.type === "message_accepted" && !envelope.replayed) {
            busyRef.current = true;
            setBusy(true);
          }
        } else if (result === "rejected") {
          input.rejectSubmission(event.submission_id);
          addMessage("system", appshotRejectionNotice("code" in event ? event.code : undefined));
        } else if (result === "unknown") {
          input.unknownSubmission(event.submission_id);
          addMessage("system", "Appshot submission status unknown. Content retained; use /appshot pending status or /appshot pending discard.");
        }
        break;
      }
      case "model_info":
        if (generationModelKeyRef.current !== (event.model_key ?? event.model)) setGenerationStats(null);
        generationModelKeyRef.current = event.model_key ?? event.model;
        setBackendStatus("ready");
        setInfo({
          model: event.model, total: event.total_tokens,
          modelKey: event.model_key ?? event.model,
          prompt: event.prompt_tokens, completion: event.completion_tokens,
          cacheHit: event.cache_hit_tokens ?? 0,
          cacheMiss: event.cache_miss_tokens ?? 0,
          ctxPct: event.context_pct,
          contextUsed: event.context_used ?? event.prompt_tokens,
          contextLimit: event.context_limit,
          reasoningEffort: event.reasoning_effort ?? undefined,
          codeMode: event.code_mode,
          personas: event.personas ?? [],
        });

        if (event.providers) setProviders(event.providers);
        if (event.connection_routes) setConnectionRoutes(event.connection_routes);
        if (event.recent_models) setRecentModelKeys(event.recent_models);
        if (event.models) {
          setModelList(event.models.map((model) => typeof model === "string"
            ? { name: model }
            : {
                name: model.name,
                key: model.key,
                provider: model.provider,
                provider_id: model.provider_id,
                source: model.source,
                metadata_known: model.metadata_known,
                endpoint: model.endpoint,
                current: model.current,
              }));
        }
        if (event.show_reasoning !== undefined) {
          setShowReasoning(event.show_reasoning);
          showReasoningRef.current = event.show_reasoning;
        }
        break;
      case "connection_result":
        if (event.request_id !== connectionRequestRef.current) break;
        setConnectionAuth(null);
        setConnectionPending(false);
        setConnectionError(event.error);
        if (!event.error) {
          setConnectionOpen(false);
          addMessage("system", event.notice ?? "Connection saved.");
          if (event.provider_id && connectionOpenRef.current) appshotInputRef.current?.openModelMenu(event.provider_id);
          connectionOpenRef.current = false;
        }
        break;
      case "connection_auth":
        if (event.request_id === connectionRequestRef.current) setConnectionAuth(event);
        break;
      case "cache_status":
        setInfo((prev) => ({
          ...prev,
          cacheHit: event.cache_hit_tokens ?? 0,
          cacheMiss: event.cache_miss_tokens ?? 0,
        }));
        break;
      case "computer_state": {
        const computerEvent: ComputerStateEvent = event;
        setComputer((current) => mergeComputerStateEvent(current, computerEvent));
        break;
      }
      case "session_info":
        if (generationSessionIdRef.current !== event.name) setGenerationStats(null);
        generationSessionIdRef.current = event.name;
        setSessionName(event.name);
        sessionNameRef.current = event.name;
        break;
      case "mode_info":
        setRuntimeMode(event.mode);
        setBarSessionName(event.mode === "bar" ? event.bar_session ?? "" : "");
        setMinimalSessionName(event.mode === "minimal" ? event.minimal_session ?? "" : "");
        setLocalSessionName(event.mode === "local" ? event.local_session ?? "" : "");
        if (event.mode !== "bar") {
          setBarNotice("");
        }
        if (event.mode !== "work") {
          setWorkingMemory({});
          setLearningReviewStatus(null);
          updateActiveTask(null);
        }
        break;
      case "startup_banner": {
        setStartupInfo(event);
        break;
      }
      case "bar_session_list":
        setBarSessionList(event.sessions);
        break;
      case "minimal_session_list":
        setMinimalSessionList(event.sessions);
        break;
      case "local_mode_info":
        setLocalMode(event.definition);
        setLocalSessionList(event.sessions);
        break;
      case "bar_state":
        setBarDrink(event.drink);
        setBarAmbiance(event.ambiance);
        setLyraGlass(event.lyra_glass);
        setBarShift(event.shift);
        setBarOutputMode(event.output_mode);
        setBarNotice(event.notice ?? "");
        break;
      case "session_list":
        setSessionList(event.sessions);
        break;
      case "history": {
        if (!event.session_id || generationSessionIdRef.current !== event.session_id) setGenerationStats(null);
        generationSessionIdRef.current = event.session_id;
        clearStreamingDisplay();
        closeToolDetails();
        const restoredTools: ToolResultRecord[] = (event.tool_results ?? []).slice(-MAX_TOOL_RESULTS).map((result) => ({
          id: ++toolResultIdRef.current,
          name: result.name,
          output: result.output,
          error: result.error,
          durationMs: result.duration_ms,
          artifactPath: result.artifact_path,
          outputTruncated: result.output_truncated,
        }));
        toolResultsRef.current = restoredTools;
        setToolResults(restoredTools);
        const visibleMessages = event.messages.filter(
          (m: any) =>
            m.role === "user" || m.role === "reasoning" || m.role === "assistant" || m.role === "system",
        );
        timeRailCursorRef.current.reset();
        const decoratedMessages = visibleMessages.map((m: any, index: number) => {
          const timestamp = typeof m.timestamp === "number"
            && Number.isFinite(m.timestamp)
            && m.timestamp > 0
            ? m.timestamp * 1000
            : undefined;
          const rail = timelineVisibleRef.current
            ? timeRailCursorRef.current.next(
                m.role,
                timestamp,
                railLayout(m.role as Role),
              )
            : undefined;
          return {
            message: {
              role: m.role,
              content: m.content,
              timestamp,
              id: index + 1,
            } as ChatMessage,
            rail,
          };
        });
        const restored = restoreRecentHistory(
          decoratedMessages,
          MAX_RESTORED_RENDER_LINES,
          ({ message, rail }) => messageToLines(
            message,
            columns,
            showReasoningRef.current,
            runtimeMode,
            rail,
          ),
        );
        dispatchDisplay({ type: "replaceHistory", lines: restored });
        msgIdRef.current = event.messages.length + 1;
        break;
      }
      case "generation_stats":
        if ([event.completion_tokens, event.elapsed_seconds, event.tokens_per_second].every((value) => Number.isFinite(value) && value > 0)) {
          setGenerationStats(event);
        }
        break;
      case "generation_progress":
        if (!["requesting", "streaming", "waiting", "finished"].includes(event.phase)
          || ![event.elapsed_seconds, event.idle_seconds].every((value) => Number.isFinite(value) && value >= 0)) break;
        if (event.phase === "requesting") setGenerationStats(null);
        setGenerationProgress(event.phase === "finished" ? null : event);
        break;
      case "context_compaction": {
        const notice = compactionNotice(event);
        if (!notice) break;
        setCompacting(event.status === "started");
        if (event.status === "started") setGenerationProgress(null);
        addMessage("system", notice);
        break;
      }
      case "working_memory":
        setWorkingMemory(event.memory);
        break;
      case "memory_retention": {
        flushStreamRole("assistant");
        assistantStartedRef.current = false;
        const changed = event.retained_ids.length + event.confirmed_ids.length;
        const replaced = event.superseded_ids.length;
        const detail = event.decision === "superseded"
          ? `${changed} updated · ${replaced} replaced`
          : `${changed} ${event.decision}`;
        addMessage("system", `MEMORY EVOLVED · ${detail} · ${event.mode}`);
        break;
      }
      case "vision_preprocess":
        {
          const message = formatVisionPreprocessMessage(event);
          if (message) addMessage("system", message);
        }
        break;
      case "learning_review":
        addMessage("system", event.message);
        break;
      case "steering":
        addMessage("system", `⟡ steering → ${event.text ?? ""}`);
        break;
      case "learning_review_status":
        setLearningReviewStatus(
          event.status === "queued" || event.status === "running" ? event.status : null,
        );
        break;
      case "yolo_status":
        setYolo(event.yolo);
        if (event.error) {
          addMessage("error", event.error);
        } else if (event.yolo) {
          addMessage("system", "YOLO mode ON — eligible approvals bypassed · Ctrl+Y to switch off");
        } else {
          addMessage("system", "YOLO mode OFF — approvals restored");
        }
        break;
      case "task_started":
        updateActiveTask(event.task);
        setGenerationStats(null);
        if (!envelope.replayed) {
          // Retries and goal continuations can start in the backend without
          // going through the normal composer message submission path.
          busyRef.current = true;
          setBusy(true);
          cancelRequestedRef.current = false;
        }
        break;
      case "task_status":
        if (event.task && !["completed", "failed", "cancelled", "done_verified", "interrupted"].includes(event.task.status)) {
          if (!activeTaskRef.current || activeTaskRef.current.id === event.task.id) updateActiveTask(event.task);
        } else if (event.task && activeTaskRef.current?.id === event.task.id) {
          finishTurn();
        }
        break;
      case "chunk":
        if (!assistantStartedRef.current) {
          flushStreamRole("reasoning");
          assistantStartedRef.current = true;
        }
        appendStreamChunk("assistant", event.content);
        break;
      case "reasoning":
        if (assistantStartedRef.current) return;
        appendStreamChunk("reasoning", event.content);
        break;
      case "tool_calls":
        flushStreamRole("reasoning");
        flushStreamRole("assistant");
        assistantStartedRef.current = false;
        if (event.calls?.length) {
          const startedAt = Date.now();
          setActiveTools((current) => {
            const known = new Set(current.map((tool) => tool.id));
            return [
              ...current,
              ...event.calls!
                .map((tool, index) => ({
                  ...tool,
                  id: tool.id || `${tool.name}-${startedAt}-${index}`,
                  startedAt,
                }))
                .filter((tool) => !known.has(tool.id)),
            ];
          });
        }
        break;
      case "tool_progress":
        // Backend emits an `awaiting_approval` progress event right before
        // the approval request itself. Without this guard the pair causes two
        // unbatched Ink repaints of the full-width dock borders; progress
        // events while the modal is visible are equally frozen. The approval
        // frame already shows the waiting stage.
        if (approvalRequests.length > 0 || event.stage === "awaiting_approval") break;
        setActiveTools((current) => {
          if (["idle", "completed", "partial", "failed", "cancelled", "timed_out"].includes(event.status ?? "")) {
            return current.filter((tool) => tool.id !== event.call_id);
          }
          const progress = {
            stage: event.stage,
            status: event.status,
            message: event.message,
            current: event.current,
            total: event.total,
            percent: event.percent,
            unit: event.unit,
          };
          const index = current.findIndex((tool) => tool.id === event.call_id);
          if (index < 0) {
            return [...current, {
              id: event.call_id || `${event.name}-${Date.now()}`,
              name: event.name,
              arguments: "",
              startedAt: Date.now(),
              progress,
            }];
          }
          return current.map((tool, itemIndex) => itemIndex === index ? { ...tool, progress } : tool);
        });
        break;
      case "process_status": {
        const processTool: ActiveTool = {
          id: `process:${event.process_id}`,
          name: `${event.kind} bg`,
          arguments: event.label,
          startedAt: Math.round(event.started_at * 1000),
          progress: {
            stage: event.status === "running" ? "background" : event.status,
            status: event.status,
            message: `${event.output_chars} chars · ${event.process_id.slice(0, 8)}`,
            current: event.output_chars,
            unit: "chars",
          },
        };
        if (event.status === "running") {
          setActiveProcesses((current) => {
            const exists = current.some((item) => item.id === processTool.id);
            return exists
              ? current.map((item) => item.id === processTool.id ? processTool : item)
              : [...current, processTool];
          });
        } else {
          setActiveProcesses((current) => current.filter((item) => item.id !== processTool.id));
          const marker = event.status === "completed" ? "\x1b[32m✓\x1b[0m" : "\x1b[31m✗\x1b[0m";
          addMessage(
            "tool",
            `${marker} background ${event.kind} · ${event.status} · ${event.duration_ms}ms\n` +
            `  process ${event.process_id.slice(0, 8)} · ${event.output_chars} chars`,
          );
        }
        break;
      }
      case "agent_team":
        setAgentTeams((current) => reduceAgentTeamEvent(current, event));
        if (event.event === "team_agent_idle" && event.process_id) {
          setActiveProcesses((current) => current.filter((item) => item.id !== `process:${event.process_id}`));
        }
        break;
      case "tool_approval_request":
        closeToolDetails();
        // Single state update on purpose: the waiting summary is derived
        // from `pendingApproval` during render. A second activeTools update
        // here would be a second unbatched Ink repaint of the full-width
        // dock borders for the same approval event.
        setApprovalRequests((current) => current.some((item) => item.request_id === event.request_id)
          ? current
          : [...current, event]);
        break;
      case "approval_inbox_snapshot":
        if (event.orphaned.length) {
          addMessage(
            "system",
            `${event.orphaned.length} approval request(s) were left by a previous backend process. ` +
            "They were marked orphaned and will not execute automatically; retry the original task if still needed.",
          );
        }
        break;
      case "approval_resolved":
        setApprovalRequests((current) => current.filter(
          (item) => item.request_id !== event.request_id
        ));
        break;
      case "approval_response_rejected":
        setApprovalRequests((current) => current.filter(
          (item) => item.request_id !== event.request_id
        ));
        addMessage("error", event.reason);
        break;
      case "user_question_request":
        closeToolDetails();
        setQuestionProtocol((current) => reduceQuestionProtocolState(current, event));
        break;
      case "user_question_resolved":
        setQuestionProtocol((current) => reduceQuestionProtocolState(current, event));
        break;
      case "user_question_response_rejected":
        setQuestionProtocol((current) => reduceQuestionProtocolState(current, event));
        if (!event.retryable) addMessage("error", event.reason);
        break;
      case "event_replay_complete":
        if (event.has_more) {
          procRef.current?.stdin?.write(JSON.stringify({
            type: "event_replay",
            after_cursor: event.next_cursor,
            limit: 500,
          }) + "\n");
        }
        break;
      case "tool_result":
        const completedAt = Date.now();
        flushStreamRole("assistant");
        assistantStartedRef.current = false;
        setActiveTools((current) => {
          if (event.call_id) return current.filter((tool) => tool.id !== event.call_id);
          const index = current.findIndex((tool) => tool.name === event.name);
          return index < 0 ? current : current.filter((_, itemIndex) => itemIndex !== index);
        });
        const ms = event.duration_ms ? ` \x1b[90m${event.duration_ms}ms\x1b[0m` : "";
        const status = event.error ? "\x1b[31m✗\x1b[0m" : "\x1b[32m✓\x1b[0m";
        const result: ToolResultRecord = {
          id: ++toolResultIdRef.current,
          name: event.name,
          output: event.output,
          error: event.error,
          durationMs: event.duration_ms,
          artifactPath: event.artifact_path,
          outputTruncated: event.output_truncated,
        };
        const nextResults = [...toolResultsRef.current, result].slice(-MAX_TOOL_RESULTS);
        toolResultsRef.current = nextResults;
        setToolResults(nextResults);
        const body = toolResultBody(result);
        if (shouldCollapseToolResult(body, TOOL_COLLAPSE_CHARS, TOOL_COLLAPSE_LINES)) {
          const lineCount = body.split(/\r?\n/).length;
          addMessage(
            "tool",
            `${status} ${event.name}${ms}  \x1b[90m${body.length} chars · ${lineCount} lines\x1b[0m\n` +
            `  ${summarizeToolResult(body)}\n` +
            `  \x1b[36m▸ collapsed · /tool ${result.id} or Ctrl+O for latest\x1b[0m`,
            { toolCompletion: true, timestamp: completedAt },
          );
        } else {
          const artifact = event.output_truncated && event.artifact_path
            ? `\n  \x1b[90mfull result → ${event.artifact_path}\x1b[0m`
            : "";
          addMessage(
            "tool",
            `${status} ${event.name}${ms}\n  ${body}${artifact}`,
            { toolCompletion: true, timestamp: completedAt },
          );
        }
        break;
      case "turn_changes": {
        const line = formatTurnChangesSummary(event);
        if (line && turnChangesGateRef.current?.shouldRender(event, sessionNameRef.current)) addMessage("system", line);
        break;
      }
      case "done":
        finishTurn();
        break;
      case "error":
        if (event.call_id) {
          setActiveTools((current) => current.filter((tool) => tool.id !== event.call_id));
        }
        if (event.recoverable) {
          flushStreamRole("assistant");
          assistantStartedRef.current = false;
          const label = event.code
            ? `${event.code.replaceAll("_", " ").toUpperCase()}${event.tool_name ? ` · ${event.tool_name}` : ""}`
            : event.message;
          const detail = event.code && event.message !== label ? ` — ${event.message}` : "";
          addMessage("tool", `\x1b[33m⚠\x1b[0m ${label}${detail}`);
          break;
        }
        try {
          finishTurn();
        } finally {
          addMessage("error", event.message);
        }
        break;
    }
  }, [addMessage, appendStreamChunk, clearStreamingDisplay, closeToolDetails, columns, finishTurn, flushStreamRole, railLayout, runtimeMode, approvalRequests.length, updateActiveTask]);

  // ── refs to decouple backend lifecycle from React deps ──
  useLayoutEffect(() => { runtimeTimingRef.current?.commit(); });

  const handleEventRef = useRef(handleEvent);
  const restartPendingRef = useRef(false);
  handleEventRef.current = handleEvent;
  const addMessageRef = useRef(addMessage);
  addMessageRef.current = addMessage;
  const clearStreamingDisplayRef = useRef(clearStreamingDisplay);
  clearStreamingDisplayRef.current = clearStreamingDisplay;

  useEffect(() => {
    let disposed = false;
    let discardPendingText = () => {};
    let closeHandshake = () => {};
    const restart = new ControlledBackendRestart();
    let restartSession: string | undefined;

    const startBackend = () => {
      if (procRef.current || disposed || lifecycle?.closing) return;
      setBackendStatus("connecting");
      const python = process.env.AGENT_PYTHON || "python";
      const backendCwd = process.env.AGENT_PROJECT_ROOT || process.cwd();
      const proc = spawn(python, ["-m", "agent.cli.backend"], {
        stdio: ["pipe", "pipe", "pipe"],
        cwd: backendCwd,
        env: { ...process.env, PYTHONUNBUFFERED: "1", ASTRA_EVENT_SCOPE: eventScopeRef.current,
          ASTRA_TUI_PID: String(process.pid), ASTRA_TUI_RESTART: "1",
          ...(restartSession ? { AGENT_SESSION: restartSession } : {}) },
      });
      // increase pipe buffer on Windows (default 64KB on Linux, ~4KB on Windows)
      proc.stdout?.setEncoding("utf-8");
      proc.stderr?.setEncoding("utf-8");
      procRef.current = proc;
      lifecycle?.attachBackend(proc);
      const runtimeTiming = new RuntimeTiming(samples => {
        const stdin = proc.stdin;
        if (lifecycle?.closing || !stdin || stdin.destroyed || stdin.writableEnded || stdin.writableLength > 65536) {
          throw new Error("Timing receipt backpressure");
        }
        stdin.write(JSON.stringify({ type: "performance_ack", samples }) + "\n");
      });
      runtimeTimingRef.current = runtimeTiming;
      if (backendStartedOnceRef.current) {
        proc.stdin?.write(JSON.stringify({
          type: "event_replay",
          after_cursor: eventDeliveryRef.current.cursor,
          limit: 500,
        }) + "\n");
      }
      backendStartedOnceRef.current = true;
      const pendingAppshot=appshotInputRef.current?.snapshot().pending;
      if(pendingAppshot)proc.stdin?.write(JSON.stringify({type:'submission_status',submission_id:pendingAppshot.submissionId})+'\n');

      const rl = createInterface({ input: proc.stdout! });
      let protocolNoticeShown = false;
      const handshake = createBackendHandshake(
        () => {
          if (lifecycle?.closing) return;
          try { addMessageRef.current("system", "Waiting for the backend to connect…"); } catch { /* Diagnostic only. */ }
        },
        () => {
          if (lifecycle?.closing) return;
          try { terminate("Backend sent no valid response within 15 seconds."); }
          finally { proc.kill(); }
        },
      );
      closeHandshake = handshake;
      let deliveryFailed = false;
      // Lifecycle events remain synchronous. Only volatile text is batched,
      // before Markdown parsing, so terminal state cannot wait behind a timer.
      const textBatcher = createTextEventBatcher(
        (event) => { if (!lifecycle?.closing) runtimeTiming.handle(event, () => handleEventRef.current(event)); },
        (event) => {
          deliveryFailed = true;
          void writeProtocolDiagnostic(backendCwd, {
            phase: "handle", event_type: event.type, error_type: "Error",
          }).catch(() => {});
        },
      );
      discardPendingText = textBatcher.discard;
      discardPendingTextRef.current = textBatcher.discard;
      const receiveEvent = createBackendEventReceiver(
        (event) => {
          handshake();
          if (lifecycle?.closing) { textBatcher.discard(); return; }
          textBatcher.accept(event);
          if (event.type === "wakeup_status") {
            if (event.message) addMessageRef.current("system", event.message);
          } else if (event.type === "peer_message") {
            // Another Astra session on this computer: a notice, never the user's words.
            addMessageRef.current("system", `PEER ${event.direction === "in" ? "←" : "→"} ${event.peer} · ${event.state} · ${event.task_id}\n${event.text}`);
          } else if (event.type === "restart_status") {
            restartPendingRef.current = ["draining", "awaiting_ack", "exiting"].includes(event.state);
            if (event.state === "cancelled") restart.cancel();
            addMessageRef.current("system", event.message);
          } else if (event.type === "restart_ready" && restart.acceptReady(event)) {
            if (deliveryFailed || eventDeliveryRef.current.hasPendingDelivery) {
              restart.cancel();
              proc.stdin?.write(JSON.stringify({ type: "command", cmd: "/restart cancel" }) + "\n");
              addMessageRef.current("system", "Restart cancelled because a response update could not be fully displayed. Check the protocol diagnostic before reconnecting.");
            } else {
              proc.stdin?.write(JSON.stringify({ type: "restart_ack", request_id: event.request_id }) + "\n");
            }
          } else if (event.type === "history" && restart.restored(event.session_id ?? "")) {
            addMessageRef.current("system", "Astra backend restarted. The session has been restored.");
          }
        },
        (diagnostic) => {
          if (lifecycle?.closing) return;
          deliveryFailed = true;
          void writeProtocolDiagnostic(backendCwd, diagnostic).catch(() => {});
          if (protocolNoticeShown) return;
          protocolNoticeShown = true;
          try {
            addMessageRef.current("error", "A response update could not be displayed. Later updates will continue; diagnostics: .logs/tui-protocol.jsonl");
          } catch {
            // Logging is independent of the renderer that just failed.
          }
        },
        {
          delivery: eventDeliveryRef.current,
          onAccepted: (event, parseMs) => { if (!lifecycle?.closing) runtimeTiming.received(event, parseMs); },
          onGap: (afterCursor) => {
            if (lifecycle?.closing) return;
            proc.stdin?.write(JSON.stringify({ type: "event_replay", after_cursor: afterCursor, limit: 500 }) + "\n");
          },
        },
      );
      rl.on("line", receiveEvent);

      const errRl = createInterface({ input: proc.stderr! });
      errRl.on("line", (line: string) => {
        if (lifecycle?.closing) return;
        // Model download bars must not add history and dismiss the welcome screen.
        if (!line.trim() || isBenignMacOSAllocatorDiagnostic(line) || isBackendModelProgress(line)) return;
        addMessageRef.current("error", `[backend] ${line}`);
      });

      let terminated = false;
      const terminate = (message: string, code: number | null = null) => {
        if (terminated) return;
        terminated = true;
        handshake();
        rl.close();
        errRl.close();
        if (disposed) textBatcher.discard();
        if (procRef.current !== proc) return;
        runtimeTiming.clear();
        runtimeTimingRef.current = null;
        procRef.current = null;
        if (disposed || lifecycle?.closing) return;
        receiveEvent('{"type":"done"}');
        clearStreamingDisplayRef.current();
        const pendingAppshot=appshotInputRef.current?.snapshot().pending;
        if(pendingAppshot)appshotInputRef.current?.unknownSubmission(pendingAppshot.submissionId);
        clearTimeout(appshotStatusTimerRef.current);
        setBackendStatus("disconnected");
        setYolo(false);
        setStartupVisible(false);
        restartPendingRef.current = false;
        const session = restart.onExit(code);
        if (session) {
          restartSession = session;
          startBackend();
          return;
        }
        addMessageRef.current("error", `${message} Use /reconnect to restart it.`);
      };
      proc.on("exit", (code) => terminate(`Backend exited (code ${code ?? "unknown"}).`, code));
      proc.on("error", (error) => terminate(`Backend failed: ${error.message}.`));
      proc.stdin?.on("error", (error) => {
        if (lifecycle?.closing) return;
        terminate(`Backend input failed: ${error.message}.`);
        proc.kill();
      });
    };

    startBackendRef.current = startBackend;
    startBackend();

    return () => {
      disposed = true;
      discardPendingText();
      closeHandshake();
      runtimeTimingRef.current?.clear();
      runtimeTimingRef.current = null;
      clearTimeout(appshotStatusTimerRef.current);
      if (lifecycle) void lifecycle.requestExit("ui_exit");
      else procRef.current?.kill();
      procRef.current = null;
      clearStreamingDisplayRef.current();
    };
  }, []);

  const send = useCallback((cmd: TuiCommand) => {
    if (lifecycle?.closing) return false;
    const proc = procRef.current;
    const stdin = proc?.stdin;
    if (!stdin || stdin.destroyed || stdin.writableEnded || !stdin.writable) {
      if (proc) {
        proc.emit("error", new Error("backend input is closed"));
        proc.kill();
      } else {
        addMessage("error", "Backend is not running. Use /reconnect to restart it.");
      }
      return false;
    }
    try {
      // A false write return means accepted with backpressure, not failure.
      stdin.write(JSON.stringify(cmd) + "\n");
      return true;
    } catch (error) {
      proc.emit("error", error instanceof Error ? error : new Error(String(error)));
      proc.kill();
      return false;
    }
  }, [addMessage]);

  useEffect(() => {
    // Fake Ink streams and noninteractive hosts must not execute installed helpers.
    if (
      !appshotClientFactory &&
      !(
        (process.platform === "darwin" || process.platform === "win32") &&
        process.stdin.isTTY &&
        process.stdout.isTTY
      )
    )
      return;
    const consumer: AppshotConsumer = {
      stage: (offer, binding) =>
        appshotInputRef.current?.stage(offer, binding) ?? false,
      stageWindows: (offer) => appshotInputRef.current?.stageWindows?.(offer) ?? false,
      commit: (event) => {
        if (!appshotInputRef.current) throw new Error("attachment_rejected");
        return appshotInputRef.current.commit(event);
      },
      revoke: (event) => appshotInputRef.current?.revoke(event),
      disconnect: (event) => appshotInputRef.current?.disconnect(event),
    };
    const client = appshotClientFactory
      ? appshotClientFactory(consumer)
      : new AppshotClient({ consumer,
          ...(process.platform === "win32" ? { windowsDeps: productionWindowsAppshotDependencies() } : {}) });
    appshotClientRef.current = client;
    const notice = (code: string) => addMessageRef.current("system", code === "broker_unavailable"
      ? "Appshot：连接未恢复，已暂停自动重试；在本窗口按键可重试。"
      : `Appshot: ${code}`);
    let recipientHintShown = false;
    const changed = () => {
      if (client.state.connection !== "connected") return;
      if (!recipientHintShown && client.activityNS === 0n) {
        recipientHintShown = true;
        setBarNotice("Appshot：聚焦此标签后切到目标应用截图；若焦点检测不可用，按一下方向键选择此对话。");
      }
      const state = appshotInputRef.current?.capacity();
      if (state) client.updateDraft(client.activityNS, state.appshotCount, state.canAccept);
    };
    client.on("notice", notice);
    client.on("change", changed);
    void client.start();
    const stopFocus = appshotClientFactory ? () => {} : startAppshotFocusMonitor(() => client.recordInput());
    return () => {
      stopFocus();
      client.off("change", changed);
      client.off("notice", notice);
      appshotClientRef.current = undefined;
      void client.close();
    };
  }, [appshotClientFactory]);

  const updateAppshotDraft = useCallback((state: AppshotDraftState) => {
    const client = appshotClientRef.current;
    client?.updateDraft(client.activityNS, state.appshotCount, state.canAccept);
  }, []);
  const releaseAppshot = useCallback(
    (id: string) => appshotClientRef.current?.release(id),
    [],
  );
  const refreshModels = useCallback((providerId?: string) => send({ type: "refresh_models", provider_id: providerId }), [send]);

  const submitQuestionAnswer = useCallback((requestId: string, answers: UserQuestionAnswer[]) => {
    if (!questionRequest || questionRequest.request_id !== requestId) return;
    addMessage("user", formatQuestionAnswerDisplaySummary(questionRequest, answers));
    send({ type: "user_question_response", request_id: requestId, answers });
  }, [addMessage, questionRequest, send]);

  const cancelQuestion = useCallback((requestId: string) => {
    if (!questionRequest || questionRequest.request_id !== requestId) return;
    send({ type: "user_question_cancel", request_id: requestId });
  }, [questionRequest, send]);

  const openToolDetails = useCallback((requestedId?: string) => {
    const available = toolResultsRef.current;
    if (!available.length) {
      addMessage("system", "No tool results are available yet.");
      return;
    }
    const normalized = (requestedId ?? "").trim().toLowerCase();
    const selected = !normalized || normalized === "latest"
      ? available[available.length - 1]
      : available.find((item) => item.id === Number(normalized));
    if (!selected) {
      addMessage("error", `Unknown tool result #${requestedId}. Available: ${available.map((item) => item.id).join(", ")}`);
      return;
    }
    detailOpenRef.current = true;
    dispatchDisplay({ type: "setToolDetailOpen", open: true });
    setOpenToolResultId(selected.id);
    setToolDetailOffset(0);
  }, [addMessage]);

  const submit = useCallback((submission: InputSubmission) => {
    const text = submission.text;
    if (restartPendingRef.current && !/^\/(?:restart(?: cancel)?|wakeup(?: cancel)?|cancel|yolo(?:\s.*)?|exit|quit)$/.test(text.trim())) {
      if (submission.submissionId) appshotInputRef.current?.rejectSubmission(submission.submissionId);
      addMessage("system", "Restart is pending. Use /restart cancel before starting more work.");
      return;
    }
    if (submission.appshots.length && /^\/appshot(?:\s|$)/i.test(text.trimStart())) {
      // A pasted-text expansion can reveal a command only after InputBar's local dispatch.
      appshotInputRef.current?.rejectSubmission(submission.submissionId!);
      addMessage("system", "Run the Appshot command directly in the composer. Attached content is retained.");
      return;
    }
    if (submission.appshots.length) {
      const id = submission.submissionId!;
      // Typed Appshot messages never enter ordinary busy steering or slash dispatch.
      const ok = send({
        type: "message",
        text,
        appshots: submission.appshots,
        submission_id: id,
        appshot_session_id: submission.appshotSessionId!,
        appshot_broker_id: submission.appshotBrokerId!,
      });
      if (!ok) {
        appshotInputRef.current?.unknownSubmission(id);
        return;
      }
      addMessage("user", text);
      clearTimeout(appshotStatusTimerRef.current);
      appshotStatusTimerRef.current = setTimeout(() => {
        if (appshotInputRef.current?.snapshot().pending?.submissionId === id)
          send({ type: "submission_status", submission_id: id });
      }, 5000);
      return;
    }
    if (!text.trim()) return;
    if (text.trim() === "/connect") {
      connectionRequestRef.current = "";
      setConnectionError(""); setConnectionPending(false);
      setConnectionOpen(true); connectionOpenRef.current = true;
      return;
    }
    if (text.trim().startsWith("/model-refresh ")) {
      const providerId = text.trim().slice("/model-refresh ".length);
      send({ type: "refresh_models", provider_id: providerId, force: true });
      appshotInputRef.current?.openModelMenu(providerId);
      return;
    }
    if (/^\/appshot\s+pending(?:\s|$)/i.test(text.trim())) {
      const action = text.trim().toLowerCase();
      const pending = appshotInputRef.current?.snapshot().pending;
      if (!pending) {
        addMessage("system", "No pending Appshot submission.");
        return;
      }
      if (action === "/appshot pending discard") {
        clearTimeout(appshotStatusTimerRef.current);
        appshotInputRef.current?.discardSubmission(pending.submissionId);
        addMessage(
          "system",
          "Pending Appshot draft discarded. Newer draft preserved; any backend work already admitted is unaffected.",
        );
      } else if (action === "/appshot pending status")
        send({ type: "submission_status", submission_id: pending.submissionId });
      else
        addMessage(
          "system",
          "Use /appshot pending status or /appshot pending discard.",
        );
      return;
    }
    const trimmedText = text.trim();
    if (/^\/appshot(?:\s|$)/i.test(trimmedText)) {
      void runAppshotCommand(trimmedText, appshotClientRef.current).then((message) => {
        if (message) addMessage("system", message);
      });
      return;
    }
    const toolDetailsMatch = trimmedText.match(/^\/tool(?:\s+(\S+))?$/i);
    if (toolDetailsMatch) {
      openToolDetails(toolDetailsMatch[1]);
      return;
    }
    const galleryMatch = trimmedText.match(/^\/gallery(?:\s+(.*))?$/i);
    if (galleryMatch) {
      void openImageGallery(toolResultsRef.current, galleryMatch[1]).then(
        (message) => addMessage("system", message),
        () => addMessage("error", "无法打开图库。请检查浏览器和图库文件是否可用。"),
      );
      return;
    }
    const timelineCommand = resolveTimelineCommand(text, timelineVisibleRef.current);
    const controlWhileBusy = /^\/(?:cancel|theme|timeline|reconnect|restart|wakeup|yolo|help)(?:\s|$)/i.test(trimmedText);
    const streamLifecycle = submitStreamLifecyclePolicy(text, timelineCommand);
    if (busyRef.current && !controlWhileBusy) {
      // Auto-steering: send as a normal message; the backend routes it
      // into the running agent's steering queue instead of rejecting.
      if (!trimmedText.startsWith("/")) {
        addMessage("user", `⟡ ${trimmedText}`);
        send({ type: "message", text: trimmedText });
      } else {
        addMessage("system", `Finish or cancel the current reply before using ${trimmedText.split(/\s+/)[0]}. /yolo and Ctrl+Y are available while it runs.`);
      }
      return;
    }
    const imageDisplay = formatImageInputDisplay(text);
    const quietBarAction = isQuietBarAction(trimmedText, runtimeMode);
    if (!quietBarAction) addMessage("user", imageDisplay ?? text);
    if (streamLifecycle === "clear") clearStreamingDisplay();
    const image = parseImageInput(text);

    // POSIX image paths also start with "/"; send attachments before slash
    // command dispatch so a valid image is not treated as an unknown command.
    if (image) {
      if (send({ type: "message", text })) {
        busyRef.current = true;
        setBusy(true);
      }
      return;
    }

    if (text.startsWith("/")) {
      const themeMatch = text.match(/^\/theme(?:\s+(\S+))?$/i);
      if (themeMatch) {
        const selected = (themeMatch[1] ?? "").toLowerCase();
        if (!selected) {
          addMessage("system", `Current theme: ${themeName}\nAvailable: ${THEME_NAMES.join(", ")}`);
        } else if (isThemeName(selected)) {
          try {
            saveThemeName(selected);
            setThemeName(selected);
            addMessage("system", `Theme switched to ${selected}: ${THEMES[selected].description}`);
          } catch (error) {
            addMessage("error", `Could not save theme: ${String(error)}`);
          }
        } else {
          addMessage("error", `Unknown theme: ${selected}. Available: ${THEME_NAMES.join(", ")}`);
        }
      } else {
        if (timelineCommand) {
          if (!timelineCommand.ok) {
            addMessage("system", timelineCommand.error);
          } else {
            try {
              const resetCursor = shouldResetTimelineCursor(
                timelineVisibleRef.current,
                timelineCommand.enabled,
              );
              saveTimelineDisplay(timelineCommand.enabled);
              if (!timelineCommand.enabled) {
                streamCoordinatorRef.current.clearTimeRails();
                dispatchDisplay({ type: "hideDynamicTimeRails" });
              }
              timelineVisibleRef.current = timelineCommand.enabled;
              setTimelineVisible(timelineCommand.enabled);
              if (resetCursor) timeRailCursorRef.current.reset();
              addMessage(
                "system",
                `Timeline display: ${timelineCommand.enabled ? "ON" : "OFF"}`,
              );
            } catch (error) {
              addMessage("error", `Could not save timeline preference: ${String(error)}`);
            }
          }
        } else if (text === "/reset") {
          timeRailCursorRef.current.reset();
          dispatchDisplay({ type: "clearHistory" });
          toolResultsRef.current = [];
          detailOpenRef.current = false;
          setToolResults([]);
          setOpenToolResultId(null);
          addMessage("system", "Context cleared.");
          send({ type: "command", cmd: "/reset" });
        } else if (text === "/think") {
          const next = !showReasoningRef.current;
          showReasoningRef.current = next;
          setShowReasoning(next);
          send({ type: "command", cmd: `/think ${next ? "on" : "off"}` });
        } else if (text === "/compress") {
          if (send({ type: "command", cmd: "/compress" })) {
            addMessage("system", "Compressing context...");
            busyRef.current = true;
            setBusy(true);
          }
        } else if (text === "/reload") {
          addMessage("system", "Reloading agent runtime...");
          send({ type: "command", cmd: "/reload" });
        } else if (startsConversationCommand(text)) {
          if (send({ type: "command", cmd: text })) {
            busyRef.current = true;
            setBusy(true);
            setActiveTools([]);
            setActiveProcesses([]);
          }
        } else if (/^\/learn\s+migrate(?:\s|$)/i.test(text)) {
          addMessage("system", "Migrating historical automatic summaries…");
          if (send({ type: "command", cmd: text })) {
            busyRef.current = true;
            setBusy(true);
          }
        } else if (text === "/help") {
          addMessage("system", [
            "Shortcuts: Ctrl+L activity dock · Ctrl+O tool details · Ctrl+Y YOLO · Ctrl+C cancel/exit",
            "YOLO: /yolo [on|off|status] works during replies. Ctrl+Y also works in approval panels.",
            `CHAT     /image  /bar  /minimal${localMode ? `  ${localMode.command}` : ""}  /reset  /compress  /undo  /retry  /changes  /think`,
            "MODEL    /model  /connect  /persona",
            "TOOLS    /search  /tool  /memory  /skills  /learn  /tools  /browser  /computer  /conclave",
            "SESSION  /tasks  /resume  /cancel  /session  /handoff",
            "CAPTURE  /appshot status · shortcut · enable · disable",
            "DISPLAY  /theme  /timeline",
            "SYSTEM   /health  /doctor  /diagnostics  /maintenance  /sandbox  /vision-tiles  /mcp  /yolo  /permissions  /reload  /reconnect  /restart  /wakeup  /help",
          ].join("\n"));
        } else if (text === "/reconnect") {
          if (procRef.current) {
            addMessage("system", "Backend is already running.");
          } else {
            addMessage("system", "Restarting backend...");
            startBackendRef.current();
          }
        } else if (text.startsWith("/image")) {
          if (!image && !imageDisplay) {
            addMessage("error", "Usage: /image <path> [prompt]");
            return;
          }
          if (send({ type: "message", text })) {
            busyRef.current = true;
            setBusy(true);
          }
        } else {
          const startsRetry = trimmedText === "/retry"
            || (localMode && trimmedText.startsWith(localMode.command + " ")
              && trimmedText.slice(localMode.command.length).trim().toLowerCase() === "retry");
          if (send({ type: "command", cmd: text }) && startsRetry) {
            // Cover admission latency too. A rejected retry sends done;
            // a successful retry keeps busy until the model turn finishes.
            busyRef.current = true;
            setBusy(true);
          }
        }
      }
      return;
    }

    if (send({ type: "message", text })) {
      busyRef.current = true;
      setBusy(true);
      setActiveTools([]);
      setActiveProcesses([]);
    }
  }, [addMessage, clearStreamingDisplay, openToolDetails, runtimeMode, send, themeName, localMode]);

  const openToolResult = openToolResultId === null
    ? undefined
    : toolResults.find((item) => item.id === openToolResultId);
  const openToolBody = openToolResult ? toolResultBody(openToolResult) : undefined;
  const toolWrapWidth = Math.max(20, columns - 8);
  const openToolLines = useMemo(
    () => openToolBody === undefined ? [] : wrapToolResult(openToolBody, toolWrapWidth),
    [openToolBody, toolWrapWidth],
  );
  const openToolLineCount = openToolLines.length;
  const approvalLineCount = approvalRequests[0]
    ? approvalRequestLines(approvalRequests[0], Math.max(20, columns - 4)).length
    : 0;

  useInput((input, key) => {
    if (lifecycle?.closing) return;
    appshotClientRef.current?.recordInput();
    if (connectionOpenRef.current) return;
    if (key.ctrl && input === "y") {
      // Handle before modal navigation; backend owns state and pending approvals.
      send({ type: "command", cmd: "/yolo" });
      return;
    }
    if (key.ctrl && input === "c") {
      if (busyRef.current && !cancelRequestedRef.current) {
        cancelRequestedRef.current = true;
        addMessage("system", "Cancelling active task… Press Ctrl+C again to force exit.");
        send({ type: "command", cmd: "/cancel" });
        return;
      }
      if (lifecycle) void lifecycle.requestExit("user_exit");
      else {
        procRef.current?.kill();
        if (detailOpenRef.current) stdout.write(LEAVE_ALTERNATE_SCREEN);
        process.exit(0);
      }
    }
    if (approvalRequests.length > 0) {
      const transition = transitionApprovalInput(
        {
          requests: approvalRequests,
          detailOpen: approvalDetailOpen,
          detailOffset: approvalDetailOffset,
        },
        input,
        key,
        { lineCount: approvalLineCount, pageSize: approvalPageSize },
      );
      if (transition.response) {
        if (!send({
          type: "tool_approval_response",
          request_id: transition.response.requestId,
          decision: transition.response.decision,
        })) return;
        setApprovalRequests(transition.requests);
      }
      setApprovalDetailOpen(transition.detailOpen);
      setApprovalDetailOffset(transition.detailOffset);
      return;
    }
    if (questionRequest) return;
    if (openToolResult) {
      if (key.escape || input === "q" || (key.ctrl && input === "o")) {
        closeToolDetails();
        return;
      }
      if (key.upArrow) {
        setToolDetailOffset((current) => clampToolDetailOffset(current - 1, openToolLineCount, toolDetailPageSize));
        return;
      }
      if (key.downArrow) {
        setToolDetailOffset((current) => clampToolDetailOffset(current + 1, openToolLineCount, toolDetailPageSize));
        return;
      }
      if (key.pageUp) {
        setToolDetailOffset((current) => clampToolDetailOffset(current - toolDetailPageSize, openToolLineCount, toolDetailPageSize));
        return;
      }
      if (key.pageDown) {
        setToolDetailOffset((current) => clampToolDetailOffset(current + toolDetailPageSize, openToolLineCount, toolDetailPageSize));
        return;
      }
      return;
    }
    if (key.ctrl && input === "o") {
      openToolDetails();
      return;
    }
    if (key.ctrl && input === "l") {
      setActivityExpanded((current) => !current);
    }
  });

  return (
    <ThemeProvider theme={theme}>
    {startupVisible ? (
      <StartupScreenMemo columns={columns} rows={rows} info={startupInfo} animate={startupAnimationEnabled} />
    ) : (
    <>
    <Box
      flexDirection="column"
      width={columns}
      display={openToolResult ? "none" : "flex"}
      height={showWelcome ? Math.max(16, rows - 1) : undefined}
    >
      {showWelcome && <WelcomeScreenMemo
        columns={columns}
        rows={rows}
        info={startupInfo}
        model={info.model}
        sessionName={sessionName}
      />}

      <HistoryOutput generation={`${themeName}:${historyGeneration}`} lines={historyLines} runtimeMode={runtimeMode} />

      <StreamingControlLayout
        rows={rows}
        interactionActive={interactionActive}
        dynamic={compactQuestion ? null : visibleDynamicDisplayLines(displayState, Math.max(1, rows - 2)).map((line) => (
          <MessageLine key={line.key} line={line} runtimeMode={runtimeMode} />
        ))}
        header={compactQuestion ? null : runtimeMode === "bar"
          ? <BarHeaderMemo columns={columns} />
          : <AppHeaderMemo columns={columns} busy={busy} mode={runtimeMode} localMode={localMode} />}
        status={compactQuestion ? null : runtimeMode === "work" ? <ActivityDockMemo
          expanded={activityExpanded}
          tools={dockTools}
          now={toolClock}
          memory={workingMemory}
          lastTool={toolResults[toolResults.length - 1]}
          model={info.model}
          totalTokens={info.total}
          promptTokens={info.prompt}
          cacheHitTokens={info.cacheHit}
          cacheMissTokens={info.cacheMiss}
          contextUsed={info.contextUsed}
          contextPct={info.ctxPct}
          contextLimit={info.contextLimit}
          status={backendStatus === "disconnected"
            ? "disconnected"
            : compacting ? COMPACTION_LABEL : activeTools.length || activeProcesses.length
              ? `tool ${(activeTools[0] ?? activeProcesses[0]).name} · ${formatToolElapsed((activeTools[0] ?? activeProcesses[0]).startedAt, toolClock)}`
              : generationProgressLabel(generationProgress) ?? (activeTask
                ? `task ${activeTask.id.slice(0, 6)} · ${activeTask.status}`
                : busy
                  ? "thinking"
                  : learningReviewStatus
                    ? `review ${learningReviewStatus}`
                    : backendStatus)}
          showReasoning={showReasoning}
          reasoningEffort={info.reasoningEffort}
          codeMode={info.codeMode}
          generationStats={compacting || generationProgress ? null : generationStats}
          sessionName={sessionName}
          columns={columns}
          teams={agentTeams}
          pendingApproval={pendingApprovalSummary}
          queuedApprovalCallIds={queuedApprovalCallIds}
          computer={computer}
        /> : runtimeMode === "bar" ? <BarStatusDockMemo
          session={barSessionName}
          busy={busy}
          disconnected={backendStatus === "disconnected"}
          notice={compacting ? COMPACTION_LABEL : generationProgressLabel(generationProgress) ?? barNotice}
          ambiance={barAmbiance}
          shift={barShift}
          lyraGlass={lyraGlass}
          outputMode={barOutputMode}
          columns={columns}
        /> : runtimeMode === "local" ? <LocalStatusDock
          session={localSessionName}
          definition={localMode}
          busy={busy}
          disconnected={backendStatus === "disconnected"}
          columns={columns}
          generationLabel={compacting ? COMPACTION_LABEL : generationProgressLabel(generationProgress)}
          cacheHitTokens={info.cacheHit}
          cacheMissTokens={info.cacheMiss}
          contextUsed={info.contextUsed}
          contextLimit={info.contextLimit}
          contextPct={info.ctxPct}
        /> : <MinimalStatusDock
          session={minimalSessionName}
          busy={busy}
          disconnected={backendStatus === "disconnected"}
          tools={dockTools}
          now={toolClock}
          columns={columns}
          generationLabel={compacting ? COMPACTION_LABEL : generationProgressLabel(generationProgress)}
          cacheHitTokens={info.cacheHit}
          cacheMissTokens={info.cacheMiss}
          contextUsed={info.contextUsed}
          contextLimit={info.contextLimit}
          contextPct={info.ctxPct}
        />}
        auxiliary={<>
          {connectionOpen && <Box flexDirection="column" display={approvalRequests.length || questionRequest ? "none" : "flex"}><ConnectionPanel routes={connectionRoutes} pending={connectionPending} error={connectionError} authorization={connectionAuth}
            onCancel={() => {
              if (connectionPending && connectionRequestRef.current) send({ type: "cancel_connection", request_id: connectionRequestRef.current });
              connectionRequestRef.current = "";
              setConnectionPending(false); setConnectionAuth(null);
              setConnectionOpen(false); connectionOpenRef.current = false;
            }}
            onSave={(request) => {
              connectionRequestRef.current = request.request_id;
              setConnectionError("");
              setConnectionAuth(null);
              if (send(request)) setConnectionPending(true);
              else setConnectionError("Backend disconnected. Reconnect before saving.");
            }} /></Box>}
          {!interactionActive && runtimeMode === "bar" && <BarShelf drink={barDrink} columns={columns} />}

          {approvalRequests[0] && (
            <ToolApprovalPanel
              request={approvalRequests[0]}
              width={columns}
              queueIndex={1}
              queueTotal={approvalRequests.length}
              expanded={approvalDetailOpen}
              offset={approvalDetailOffset}
              pageSize={approvalPageSize}
              maxHeight={rows < 20 ? Math.max(6, rows - 2) : undefined}
            />
          )}

          {questionRequest && (
            <Box flexDirection="column" display={approvalRequests.length ? "none" : "flex"}><QuestionCard
              request={questionRequest}
              active={!openToolResult && approvalRequests.length === 0}
              width={columns}
              maxHeight={Math.max(6, rows - 2)}
              onAnswer={submitQuestionAnswer}
              onCancel={cancelQuestion}
              submissionRejection={questionProtocol.rejection}
            /></Box>
          )}
        </>}
        input={<Box flexDirection="column" display={compactQuestion ? "none" : "flex"}><InputBarMemo
          ref={appshotInputRef}
          appshotManifestReader={appshotManifestReader}
          onAppshotStateChange={updateAppshotDraft}
          onAppshotRelease={releaseAppshot}
          onSubmit={submit}
          disabled={connectionOpen || Boolean(openToolResult) || approvalRequests.length > 0 || questionRequest !== null}
          yolo={yolo}
          sessionList={sessionList}
          barSessionList={barSessionList}
          minimalSessionList={minimalSessionList}
          localSessionList={localSessionList}
          localMode={localMode}
          modelList={modelList.map((model) => ({
            ...model,
            current: model.current ?? (model.key ? model.key === info.modelKey : model.name === info.model),
          }))}
          providers={providers}
          recentModels={recentModelKeys}
          onModelMenuOpen={refreshModels}
          onMenuVisibilityChange={setCommandMenuVisible}
          menuFocusActive={commandMenuVisible}
          menuRows={commandMenuRows}
          currentTheme={themeName}
          runtimeMode={runtimeMode}
          columns={columns}
          personas={info.personas}
        /></Box>}
      />
    </Box>
    {openToolResult && (
      <Box flexDirection="column" height={Math.max(8, rows - 1)}>
        <ToolDetailsPanel
          result={openToolResult}
          lines={openToolLines}
          pageSize={toolDetailPageSize}
          offset={toolDetailOffset}
        />
      </Box>
    )}
    </>
    )}
    </ThemeProvider>
  );
}
