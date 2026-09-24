import React from "react";
import stringWidth from "string-width";
import { Box, Text } from "ink";
import type { GenerationStats, ToolCallInfo, ToolProgressInfo, WorkingMemory } from "../types.js";
import type { ToolResultRecord } from "../tool-results.js";
import { useTheme } from "../theme-context.js";
import { WorkingMemoryPanel, workingProgress } from "./working-memory.js";
import { buildStatusParts, computerIndicator, truncateDisplayText, type ComputerUiState } from "./status-bar.js";
import { responsiveMode } from "./app-header.js";
import type { AgentTeamView } from "../agent-team-state.js";
import { COMPACTION_LABEL } from "../context-compaction.js";

export type ActiveTool = ToolCallInfo & { startedAt: number };
export type ActivityVisualState = "idle" | "busy" | "error";
export type PendingApprovalSummary = {
  queueIndex: number;
  queueTotal: number;
  toolName: string;
  status: string;
};

export function filterQueuedApprovalTools(
  tools: ActiveTool[],
  queuedApprovalCallIds: readonly (string | undefined)[] = [],
): ActiveTool[] {
  if (!queuedApprovalCallIds.length) return tools;
  if (queuedApprovalCallIds.some((callId) => !callId)) return [];
  const queuedCallIds = new Set(queuedApprovalCallIds);
  return tools.filter((tool) => !queuedCallIds.has(tool.id));
}

export type ActivitySummaryInput = {
  status: string;
  tools: ActiveTool[];
  now: number;
  memory: WorkingMemory;
  contextPct: number;
  lastTool?: ToolResultRecord;
  columns: number;
  model?: string;
  showReasoning?: boolean;
  reasoningEffort?: "low" | "high" | "xhigh" | "max";
  codeMode?: "native" | "code" | "both";
  expanded?: boolean;
  tokensPerSecond?: number;
  teams?: AgentTeamView[];
  pendingApproval?: PendingApprovalSummary;
  computer?: ComputerUiState;
};

const CODE_MODE_LABEL = { native: "NATIVE", code: "PTC", both: "BOTH" } as const;

export function formatToolElapsed(startedAt: number, now: number): string {
  return `${Math.max(0, now - startedAt) / 1000}`.replace(/(\.\d).*$/, "$1") + "s";
}

export function formatToolName(name: string): string {
  const semanticNames: Record<string, string> = {
    bash: "BASH",
    read_file: "READ",
    stat_file: "STAT",
    write_file: "WRITE",
    edit_file: "EDIT",
    str_replace_editor: "EDIT",
    apply_patch: "PATCH",
    run_code: "PTC",
    search_web: "SEARCH",
    web_extract: "FETCH",
  };
  return semanticNames[name] ?? name;
}

export function formatToolProgress(progress?: ToolProgressInfo): string {
  if (!progress) return "";
  const parts = [progress.stage.replace(/_/g, " ").toUpperCase()];
  if (typeof progress.percent === "number" && Number.isFinite(progress.percent)) {
    parts.push(`${Math.max(0, Math.min(100, Math.round(progress.percent)))}%`);
  } else if (typeof progress.current === "number" && typeof progress.total === "number") {
    parts.push(`${progress.current}/${progress.total}${progress.unit ? ` ${progress.unit}` : ""}`);
  } else if (typeof progress.current === "number") {
    parts.push(`${progress.current}${progress.unit ? ` ${progress.unit}` : ""}`);
  }
  if (progress.message) parts.push(progress.message);
  return parts.join(" · ");
}

export function activityVisualState(status: string, toolCount: number, hasPendingApproval = false): ActivityVisualState {
  const normalized = status.trim().toLowerCase();
  if (normalized === "disconnected" || /\b(?:failed|error)\b/.test(normalized)) return "error";
  if (hasPendingApproval || toolCount > 0 || normalized === COMPACTION_LABEL || normalized === "thinking" || normalized.startsWith("task ")
    || /^(?:model wait|generating|no output)\b/.test(normalized) || normalized === "bar · talking") return "busy";
  return "idle";
}

function shorten(value: string, max: number): string {
  return truncateDisplayText(value, max);
}

export function buildActivitySummary(input: ActivitySummaryInput): string[] {
  const { completed, total } = workingProgress(input.memory);
  const mode = responsiveMode(input.columns);
  const segments: string[] = [];
  const active = input.tools[0];
  const pendingApproval = input.pendingApproval;
  const reasoningEffort = ["low", "high", "xhigh", "max"].includes(input.reasoningEffort ?? "")
    ? input.reasoningEffort
    : undefined;
  const codeMode = input.codeMode && CODE_MODE_LABEL[input.codeMode];
  const rawComputerLabel = computerIndicator(input.computer).label;
  const computerLabel = rawComputerLabel
    ? truncateDisplayText(
      rawComputerLabel,
      mode === "compact" ? Math.max(15, Math.floor(input.columns * 0.55)) : 48,
    )
    : "";

  if (mode === "compact") {
    if (computerLabel) segments.push(computerLabel);
    if (pendingApproval) {
      segments.push(`APPROVAL ${pendingApproval.queueIndex}/${pendingApproval.queueTotal} · ${pendingApproval.toolName} · ${pendingApproval.status.toUpperCase()}`);
    } else if (active && input.status !== COMPACTION_LABEL) {
      segments.push(`RUN ${shorten(formatToolName(active.name), 10)} ${formatToolElapsed(active.startedAt, input.now)}`);
      const stage = active.progress?.stage.replace(/_/g, " ").toUpperCase();
      if (stage) segments.push(shorten(stage, 7));
    } else {
      segments.push(shorten(input.status.toUpperCase(), 18));
    }
    if (reasoningEffort) segments.push(reasoningEffort.toUpperCase());
    if (codeMode) segments.push(codeMode);
    if (input.model) segments.push(shorten(input.model, 16));
    if (total > 0) segments.push(`P${completed}/${total}`);
    if (input.teams?.length) segments.push(`T${input.teams.length}`);
    segments.push(`C${input.contextPct.toFixed(0)}%`);
    if (input.showReasoning) segments.push("R");
    if (input.tokensPerSecond !== undefined) segments.push(`${input.tokensPerSecond.toFixed(0)}t/s`);
    return segments;
  }

  if (computerLabel) segments.push(computerLabel);
  if (pendingApproval) {
    segments.push(`APPROVAL ${pendingApproval.queueIndex}/${pendingApproval.queueTotal} · ${pendingApproval.toolName} · ${pendingApproval.status.toUpperCase()}`);
  } else if (active && input.status !== COMPACTION_LABEL) {
    const extra = input.tools.length > 1 ? ` +${input.tools.length - 1}` : "";
    const stage = active.progress?.stage.replace(/_/g, " ").toUpperCase() ?? "";
    const measured = typeof active.progress?.percent === "number"
      ? ` ${Math.round(active.progress.percent)}%`
      : typeof active.progress?.current === "number" && typeof active.progress?.total === "number" && active.progress.unit !== "s"
        ? ` ${active.progress.current}/${active.progress.total}`
        : "";
    const progressText = stage ? ` · ${stage}${measured}` : "";
    segments.push(`RUN ${shorten(formatToolName(active.name), 24)}${progressText} · ${formatToolElapsed(active.startedAt, input.now)}${extra}`);
  } else {
    segments.push(input.status.toUpperCase());
  }
  if (reasoningEffort) {
    segments.push(mode === "wide" ? `EFFORT ${reasoningEffort.toUpperCase()}` : reasoningEffort.toUpperCase());
  }
  if (codeMode) segments.push(mode === "wide" ? `TOOLS ${codeMode}` : codeMode);
  if (input.model) segments.push(shorten(input.model, mode === "wide" ? 28 : 20));
  if (total > 0) segments.push(`PLAN ${completed}/${total}`);
  if (input.teams?.length) {
    const running = input.teams.flatMap((team) => team.agents).filter((agent) =>
      ["starting", "running", "idle", "waiting"].includes(agent.status),
    ).length;
    segments.push(`TEAM ${input.teams.length}/${running}`);
  }
  segments.push(`CTX ${input.contextPct.toFixed(0)}%`);
  segments.push(input.showReasoning ? "R" : "r");
  if (input.tokensPerSecond !== undefined) segments.push(`${input.tokensPerSecond.toFixed(1)} tok/s`);
  if (mode === "wide" && input.lastTool && !input.expanded) {
    const icon = input.lastTool.error ? "✕" : "✓";
    const duration = input.lastTool.durationMs ? ` ${input.lastTool.durationMs}ms` : "";
    segments.push(`LAST ${icon} ${shorten(formatToolName(input.lastTool.name), 18)}${duration}`);
  }
  return segments;
}

// Budget the shortcut and important fields before Ink truncates the row.
export function fitActivitySummary(segments: string[], columns: number, model: string, separator: string, hint: string): string[] {
  const available = Math.max(1, columns - 6 - stringWidth(hint));
  let parts = segments.map((text) => ({ text, kind:
    text === model || (text.endsWith("…") && model.startsWith(text.slice(0, -1))) ? "model"
      : /^(?:CTX |C)\d+%$/.test(text) ? "context"
      : /(?:tok\/s|t\/s)$/.test(text) ? "speed"
      : /^(?:TOOLS )?(?:NATIVE|PTC|BOTH)$/.test(text) ? "tools"
      : /^(?:EFFORT )?(?:LOW|HIGH|MAX)$/.test(text) ? "effort"
      : /^(?:USER CONTROL|COMPUTER |COOP|TAKEOVER)/.test(text) ? "computer"
      : /^(?:MODEL WAIT|GENERATING|NO OUTPUT)\b/.test(text) ? "progress"
      : /^[Rr]$/.test(text) ? "reasoning"
      : /^(?:LAST |PLAN |P\d|TEAM |T\d)/.test(text) ? "secondary" : "status",
  }));
  const width = () => stringWidth(parts.map((part) => part.text).join(separator));
  const trim = (kind: string, max: number) => {
    for (const part of parts) if (width() > available && part.kind === kind) part.text = shorten(part.text, max);
  };
  const drop = (kind: string) => {
    if (width() > available) parts = parts.filter((part) => part.kind !== kind);
  };
  if (width() <= available) return parts.map((part) => part.text);
  for (const part of parts) {
    if (part.kind === "effort") part.text = part.text.replace("EFFORT ", "");
    if (part.kind === "tools") part.text = part.text.replace("TOOLS ", "");
    if (part.kind === "speed") part.text = part.text.replace(" tok/s", "t/s");
  }
  drop("secondary");
  if (width() > available) {
    for (const part of parts) if (part.kind === "computer") {
      part.text = part.text.match(/^(?:USER CONTROL|COMPUTER [^ ·]+|COOP|TAKEOVER)/)?.[0] ?? part.text;
    }
  }
  trim("status", 16);
  drop("tools");
  drop("reasoning");
  trim("status", 8);
  trim("model", 12);
  if (width() > available) {
    for (const part of parts) if (part.kind === "context") part.text = part.text.replace("CTX ", "C");
  }
  trim("model", 8);
  drop("effort");
  drop("status");
  drop("speed");
  if (width() > available) {
    for (const part of parts) if (part.kind === "progress") {
      part.text = part.text.replace(/^(?:MODEL WAIT|NO OUTPUT)/, "WAIT").replace(/^GENERATING/, "GEN");
    }
  }
  trim("model", Math.max(1, available - 8));
  if (parts.some((part) => part.kind === "computer")) drop("model");
  if (width() > available) {
    for (const part of parts) if (part.kind === "computer") {
      part.text = part.text.replace("COMPUTER ", "");
      part.text = shorten(part.text, Math.max(1, available - (width() - stringWidth(part.text))));
    }
  }
  return parts.map((part) => part.text);
}

function argumentsPreview(raw = "", maxChars = 90): string {
  const compact = raw.replace(/\s+/g, " ").trim();
  if (!compact || compact === "{}") return "";
  return compact.length <= maxChars ? compact : `${compact.slice(0, maxChars - 1)}…`;
}

function requestDuration(seconds: number): string {
  if (seconds < 0.001) return "<1ms";
  return seconds < 1 ? `${Math.round(seconds * 1000)}ms` : `${seconds.toFixed(1)}s`;
}

export function ActivityDock({
  expanded,
  tools,
  now,
  memory,
  lastTool,
  model,
  totalTokens,
  promptTokens,
  cacheHitTokens,
  cacheMissTokens,
  contextUsed,
  contextPct,
  contextLimit,
  status,
  showReasoning,
  reasoningEffort,
  codeMode,
  generationStats,
  sessionName,
  columns,
  teams = [],
  pendingApproval,
  queuedApprovalCallIds = [],
  computer,
}: {
  expanded: boolean;
  tools: ActiveTool[];
  now: number;
  memory: WorkingMemory;
  lastTool?: ToolResultRecord;
  model: string;
  totalTokens: number;
  promptTokens: number;
  cacheHitTokens?: number;
  cacheMissTokens?: number;
  contextUsed?: number;
  contextPct: number;
  contextLimit?: number;
  status: string;
  showReasoning: boolean;
  reasoningEffort?: "low" | "high" | "xhigh" | "max";
  codeMode?: "native" | "code" | "both";
  generationStats?: GenerationStats | null;
  sessionName: string;
  columns: number;
  teams?: AgentTeamView[];
  pendingApproval?: PendingApprovalSummary;
  queuedApprovalCallIds?: readonly (string | undefined)[];
  computer?: ComputerUiState;
}) {
  const theme = useTheme();
  const chrome = theme.chrome;
  const mode = responsiveMode(columns);
  const visibleTools = filterQueuedApprovalTools(tools, queuedApprovalCallIds);
  const separator = chrome?.separator ?? " · ";
  const hint = mode === "compact" ? " ^L" : expanded ? "  ^L collapse" : "  ^L expand";
  const summary = fitActivitySummary(buildActivitySummary({
    status, tools: visibleTools, now, memory, contextPct, lastTool, columns, model, showReasoning, reasoningEffort, codeMode, teams, pendingApproval, computer,
    expanded, tokensPerSecond: generationStats?.tokens_per_second,
  }), columns, model, separator, hint);
  const statusParts = buildStatusParts({
    columns: Math.max(30, columns - 10),
    model,
    variant: "details",
    sessionName,
    contextUsed,
    promptTokens,
    totalTokens,
    cacheHitTokens,
    cacheMissTokens,
    contextPct,
    contextLimit,
    showReasoning,
    theme,
  });
  const hasMemory = Boolean(memory.goal || memory.progress || memory.steps?.length);
  const visualState = activityVisualState(status, visibleTools.length, Boolean(pendingApproval));
  const indicatorColor = visualState === "error"
    ? theme.danger
    : visualState === "busy"
      ? theme.warning
      : theme.success;
  const indicator = visibleTools.length > 0
    ? chrome?.activeToolIcon ?? "◆"
    : visualState === "error"
      ? "✕"
      : visualState === "busy"
        ? "◉"
        : "●";
  const borderColor = visualState === "error"
    ? theme.danger
    : visualState === "busy"
      ? theme.warning
      : theme.border;

  return (
    <Box flexDirection="column" borderStyle={chrome?.activityFrameStyle ?? chrome?.frameStyle ?? "single"} borderColor={borderColor} paddingX={1}>
      {/* Stretch the text to the row so asynchronously inserted fields do not
          retain Ink's previous, shorter text measurement. */}
      <Box flexDirection="column" overflow="hidden">
        <Text wrap="truncate-end">
          <Text bold color={indicatorColor}>{indicator} </Text>
          {summary.map((item, index) => (
            <React.Fragment key={item}>
              {index > 0 && <Text color={theme.subtle}>{chrome?.separator ?? " · "}</Text>}
              <Text color={index === 0 ? theme.accent : index === 1 ? theme.accentAlt : theme.muted}>{item}</Text>
            </React.Fragment>
          ))}
          <Text dimColor color={theme.subtle}>{hint}</Text>
        </Text>
      </Box>

      {expanded && (
        <>
          {visibleTools.length > 0 && (
            <Text bold={theme.prefixBold} color={theme.warning}>{chrome?.activeToolsTitle ?? "LIVE TOOLS"}</Text>
          )}
          {visibleTools.map((tool) => {
            const preview = argumentsPreview(tool.arguments, Math.max(20, Math.min(90, columns - 30)));
            const progress = formatToolProgress(tool.progress);
            const progressColor = tool.progress?.status === "failed" ? theme.danger : theme.warning;
            return (
              <Text key={tool.id} color={theme.text}>
                <Text color={theme.warning}>{chrome?.activeToolIcon ?? "◌"} </Text>
                <Text bold={theme.prefixBold}>{formatToolName(tool.name)}</Text>
                {progress && <Text color={progressColor}>  {progress}</Text>}
                <Text color={theme.muted}>  {formatToolElapsed(tool.startedAt, now)}{preview ? `  ${preview}` : ""}</Text>
              </Text>
            );
          })}
          {lastTool && (
            <Text color={lastTool.error ? theme.danger : theme.success}>
              LAST {lastTool.error ? "✕" : "✓"} {formatToolName(lastTool.name)}
              <Text color={theme.muted}>{lastTool.durationMs ? `  ${lastTool.durationMs}ms` : ""}</Text>
            </Text>
          )}
          {teams.length > 0 && (
            <>
              <Text bold={theme.prefixBold} color={theme.accentAlt}>AGENT TEAMS</Text>
              {teams.map((team) => {
                const doneTasks = team.tasks.filter((task) => task.status === "completed").length;
                return (
                  <React.Fragment key={team.id}>
                    <Text color={theme.text}>
                      <Text color={theme.accentAlt}>◆ </Text>
                      <Text bold={theme.prefixBold}>{shorten(team.name, 24)}</Text>
                      <Text color={theme.muted}>  {team.status} · agents {team.agents.length} · tasks {doneTasks}/{team.tasks.length} · msg {team.messageCount}</Text>
                    </Text>
                    {team.agents.slice(0, 8).map((agent, index) => (
                      <Text key={agent.id} color={agent.status === "failed" ? theme.danger : agent.status === "running" ? theme.warning : theme.muted}>
                        {index === team.agents.length - 1 ? "└─" : "├─"} {shorten(agent.name, Math.max(8, Math.min(24, columns - 50)))} · {agent.role || "agent"} · {agent.status}{agent.unread ? ` · unread ${agent.unread}` : ""}
                      </Text>
                    ))}
                    {team.tasks.slice(0, 6).map((task) => (
                      <Text key={task.id} color={task.status === "failed" ? theme.danger : theme.subtle}>
                        <Text color={theme.accent}>  TASK </Text>{shorten(task.title, Math.max(12, columns - 38))} · {task.status}{task.ownerId ? ` · ${task.ownerId.slice(0, 8)}` : ""}
                      </Text>
                    ))}
                  </React.Fragment>
                );
              })}
            </>
          )}
          {hasMemory && <WorkingMemoryPanel memory={memory} columns={columns} embedded />}
          <Box overflow="hidden">
            <Text color={theme.subtle}>STATUS{chrome?.separator ?? " · "}</Text>
            {statusParts.map((segment, index) => (
              <Text key={`${segment.text}-${index}`} color={segment.color}>{segment.text}</Text>
            ))}
          </Box>
          {generationStats && <Text color={theme.muted} wrap="truncate-end">
            SPEED{separator}last request: {generationStats.completion_tokens} tokens / {requestDuration(generationStats.elapsed_seconds)} incl. wait
          </Text>}
        </>
      )}
    </Box>
  );
}
