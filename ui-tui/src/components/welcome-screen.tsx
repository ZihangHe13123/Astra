import React from "react";
import { Box, Text } from "ink";
import type { StartupInfo } from "../types.js";
import { useTheme } from "../theme-context.js";
import { markLineSegments } from "../theme.js";
import { selectLyraSprite } from "../lyra-sprite.js";
import { LyraCharacter } from "./lyra-character.js";
import { responsiveMode } from "./app-header.js";
import { startupMcpSummary } from "./startup-screen.js";
import { sessionLabel } from "../workspace-label.js";

function shorten(value: string, max: number): string {
  if (value.length <= max) return value;
  if (max <= 1) return "…";
  return `${value.slice(0, max - 1)}…`;
}

function RuntimeRow({
  label,
  value,
  state,
  warning = false,
  quiet = false,
}: {
  label: string;
  value: string;
  state: string;
  warning?: boolean;
  quiet?: boolean;
}) {
  const theme = useTheme();
  return (
    <Text>
      <Text color={theme.accentAlt}>{label.padEnd(7)}</Text>
      <Text color={theme.emphasis}>{value.padStart(5)}</Text>
      <Text color={theme.subtle}>  ───  </Text>
      <Text color={warning ? theme.warning : quiet ? theme.muted : theme.success}>{state}</Text>
    </Text>
  );
}

export function WelcomeScreen({
  columns,
  rows,
  info,
  model,
  sessionName,
  location,
}: {
  columns: number;
  rows: number;
  info: StartupInfo | null;
  model: string;
  sessionName: string;
  /** The working directory and branch, shown where the theme's tag would be. */
  location?: string;
}) {
  const theme = useTheme();
  const consoleTheme = theme.console;
  const mode = responsiveMode(columns);
  const mcp = info ? startupMcpSummary(info) : { ready: 0, total: 0, errors: 0 };
  const learnedCount = info?.learning.error ? "?" : info?.learning.learned ?? 0;
  const learningState = info?.learning.error ? "CHECK /learn" : info?.learning.mode === "off" ? "OFF" : "MANUAL REVIEW";
  const wide = mode === "wide";
  // The console spans the terminal like the status line and the prompt under it. Lyra's card keeps
  // the width her portrait was drawn for.
  const panelWidth = theme.name === "lyra" ? Math.max(36, Math.min(columns - 6, wide ? 108 : 76)) : Math.max(36, columns);
  const session = sessionLabel(sessionName) || "new session";
  // Compact cards spend their height on the native portrait and runtime info,
  // omitting title/footer decorations so 30-row terminals retain the art.
  const compactPortrait = theme.name === "lyra" && rows < 36;
  const lyra = theme.name === "lyra" && columns >= 79
    ? selectLyraSprite(panelWidth - 40, rows - (compactPortrait ? 14 : 20)) : undefined;
  const showArt = theme.name === "lyra" ? Boolean(lyra) : wide;
  const modelName = shorten(info?.model || model, wide ? 28 : Math.max(18, panelWidth - 18));
  const compactHeight = rows < 22;

  return (
    <Box flexGrow={1} alignItems="center" justifyContent="center">
        <Box
          width={panelWidth}
          borderStyle={theme.chrome?.headerFrameStyle ?? "single"}
          borderColor={theme.border}
          paddingX={wide ? 3 : 2}
          paddingY={compactHeight || (compactPortrait && lyra) ? 0 : 1}
          flexDirection="column"
        >
          {!(compactPortrait && lyra) && <><Box justifyContent="space-between" overflow="hidden">
            <Text bold color={theme.accent}>{consoleTheme.consoleTitle}</Text>
            {mode !== "compact" && <Text color={theme.muted} wrap="truncate-start">{location || consoleTheme.consoleTag}</Text>}
          </Box>
          <Text color={theme.subtle}>{"─".repeat(Math.max(20, panelWidth - (wide ? 8 : 6)))}</Text>
          </>}

          <Box flexDirection={showArt ? "row" : "column"}>
            {showArt && (
              <Box width={lyra?.columns ?? 34} flexShrink={lyra ? 0 : undefined} flexDirection="column" marginRight={3}>
                {lyra ? <LyraCharacter columns={lyra.columns} rows={lyra.rows} /> : consoleTheme.mark.map((line, index) => {
                  const segments = markLineSegments(line, consoleTheme.markPalette, theme, index < 3 ? theme.accent : theme.accentAlt);
                  return (
                    <Text key={`${index}:${line}`} bold={index === 3}>
                      {segments.map((seg, si) => (
                        <Text key={si} color={seg.color}>{seg.text}</Text>
                      ))}
                    </Text>
                  );
                })}
                {!lyra && <Text color={theme.emphasis}>MODEL   // {modelName}</Text>}
                {!lyra && <Text color={theme.muted} wrap="truncate-end">SESSION // {session}</Text>}
              </Box>
            )}

            <Box flexDirection="column" flexGrow={1} flexBasis={lyra ? 0 : undefined} minWidth={lyra ? 0 : undefined}>
              {(!showArt || lyra) && <Text color={theme.emphasis} wrap={lyra ? "truncate-end" : undefined}>MODEL // {modelName}</Text>}
              {lyra && <Text color={theme.muted} wrap="truncate-end">SESSION // {session}</Text>}
              <Text bold color={theme.accentAlt}>{consoleTheme.runtimeTitle}</Text>
              <RuntimeRow label="TOOLS" value={String(info?.tools ?? 0)} state={consoleTheme.toolState} />
              <RuntimeRow label="SKILLS" value={String(info?.skills ?? 0)} state={consoleTheme.skillState} />
              {/* No server configured is not a link: say so instead of "0/0 linked". */}
              <RuntimeRow
                label="MCP"
                value={mcp.total ? `${mcp.ready}/${mcp.total}` : "0"}
                state={!mcp.total ? "NONE SET" : mcp.errors > 0 ? consoleTheme.mcpWarningState : consoleTheme.mcpState}
                warning={mcp.errors > 0}
                quiet={!mcp.total}
              />
              <RuntimeRow
                label="LEARN"
                value={String(learnedCount)}
                state={learningState}
                warning={Boolean(info?.learning.error)}
              />
              {!compactHeight && <Text> </Text>}
              <Text bold color={theme.accentAlt}>{consoleTheme.commandTitle}</Text>
              {wide ? (
                <>
                  <Text color={theme.text}><Text color={theme.emphasis}>/model</Text>   switch runtime</Text>
                  <Text color={theme.text}><Text color={theme.emphasis}>/skills</Text>  browse capabilities</Text>
                  <Text color={theme.text}><Text color={theme.emphasis}>/help</Text>    command directory</Text>
                </>
              ) : (
                <Text color={theme.text}>/help commands · /model switch</Text>
              )}
            </Box>
          </Box>

        </Box>
    </Box>
  );
}
