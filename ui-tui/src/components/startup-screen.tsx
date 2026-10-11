import React, { useEffect, useState } from "react";
import { Box, Text } from "ink";
import type { StartupInfo } from "../types.js";
import { useTheme } from "../theme-context.js";
import { markLineSegments } from "../theme.js";
import { selectLyraSprite } from "../lyra-sprite.js";
import { LyraCharacter } from "./lyra-character.js";
import { responsiveMode } from "./app-header.js";

const SPINNER = ["◒", "◐", "◓", "◑"] as const;
const DEFAULT_BOOT_PHASES = [
  "MODEL BUS HANDSHAKE",
  "MEMORY CORE MOUNT",
  "TOOL REGISTRY SCAN",
  "MCP LINK NEGOTIATION",
] as const;

export function startupPhase(frame: number, phases: readonly string[] = DEFAULT_BOOT_PHASES): string {
  return phases[Math.floor(Math.max(0, frame) / 7) % phases.length] ?? phases[0] ?? "";
}

export function startupProgress(frame: number, width: number): string {
  const safeWidth = Math.max(6, width);
  const head = frame % safeWidth;
  return Array.from({ length: safeWidth }, (_, index) => {
    const distance = (index - head + safeWidth) % safeWidth;
    if (distance === 0) return "━";
    if (distance === 1 || distance === safeWidth - 1) return "╺";
    return "─";
  }).join("");
}

export function startupMcpSummary(info: StartupInfo): { ready: number; total: number; errors: number } {
  const enabled = info.mcp.filter((server) => server.state !== "disabled");
  return {
    ready: enabled.filter((server) => server.state === "ready").length,
    total: enabled.length,
    errors: enabled.filter((server) => server.state === "error").length,
  };
}

function shorten(value: string, max: number): string {
  if (value.length <= max) return value;
  if (max <= 1) return "…";
  return `${value.slice(0, max - 1)}…`;
}

export function StartupScreen({
  columns,
  rows,
  info,
  animate = true,
}: {
  columns: number;
  rows?: number;
  info: StartupInfo | null;
  animate?: boolean;
}) {
  const theme = useTheme();
  const consoleTheme = theme.console;
  const mode = responsiveMode(columns);
  const [frame, setFrame] = useState(0);

  useEffect(() => {
    // Large half-block artwork is expensive to repaint in Apple Terminal.
    // Keep the Lyra scene still; actual loading-state changes still render.
    if (!animate || theme.name === "lyra") return;
    const timer = setInterval(() => setFrame((current) => current + 1), 90);
    return () => clearInterval(timer);
  }, [animate, theme.name]);

  const ready = Boolean(info);
  const mcp = info ? startupMcpSummary(info) : null;
  const learnedCount = info?.learning.error ? "?" : info?.learning.learned ?? 0;
  const learningState = info?.learning.error ? "CHECK" : info?.learning.mode === "off" ? "OFF" : "MANUAL";
  const panelWidth = Math.max(36, Math.min(columns - 6, mode === "wide" ? 92 : theme.name === "lyra" ? 76 : 70));
  const lyra = theme.name === "lyra" && columns >= 79
    ? selectLyraSprite(panelWidth - 40, (rows ?? 30) - 10) : undefined;
  const showArt = theme.name === "lyra" ? Boolean(lyra) : mode === "wide";
  const progressWidth = Math.max(12, Math.min(mode === "compact" ? 22 : 32, panelWidth - 30));
  const phase = startupPhase(frame, consoleTheme.phases);
  const spinner = ready ? "✓" : SPINNER[frame % SPINNER.length];
  const statusColor = ready ? (mcp?.errors ? theme.warning : theme.success) : theme.accentAlt;

  return (
    <Box
      height={rows ? Math.max(8, rows - 1) : undefined}
      alignItems="center"
      justifyContent="center"
    >
      <Box
        flexDirection="column"
        width={panelWidth}
        borderStyle={theme.chrome?.headerFrameStyle ?? "single"}
        borderColor={theme.border}
        paddingX={mode === "compact" ? 2 : 3}
        paddingY={1}
      >
        <Box justifyContent="space-between" overflow="hidden">
          <Text bold color={theme.accent}>{consoleTheme.bootTitle}</Text>
          {mode !== "compact" && <Text color={theme.muted}>{consoleTheme.bootTag}</Text>}
        </Box>
        <Text color={theme.subtle}>{"─".repeat(Math.max(20, panelWidth - (mode === "compact" ? 6 : 8)))}</Text>

        <Box flexDirection={showArt ? "row" : "column"}>
          {showArt && (
            <Box width={lyra?.columns ?? 28} flexShrink={lyra ? 0 : undefined} flexDirection="column" marginRight={3}>
              {lyra ? <LyraCharacter columns={lyra.columns} rows={lyra.rows} /> : consoleTheme.mark.map((line, index) => {
                const segments = markLineSegments(line, consoleTheme.markPalette, theme, index < 3 ? theme.accent : theme.accentAlt);
                return (
                  <Text key={`${index}:${line}`}>
                    {segments.map((seg, si) => (
                      <Text key={si} color={seg.color}>{seg.text}</Text>
                    ))}
                  </Text>
                );
              })}
              {!lyra && <Text color={theme.muted}>{consoleTheme.nodeLabel}</Text>}
            </Box>
          )}

          <Box flexDirection="column" flexGrow={1} flexBasis={lyra ? 0 : undefined} minWidth={lyra ? 0 : undefined}>
            <Text bold color={ready ? statusColor : theme.accentAlt}>
              {ready ? consoleTheme.readyTitle : consoleTheme.bootActive}
            </Text>
            <Text>
              <Text bold color={statusColor}>{spinner} </Text>
              <Text color={theme.text}>{ready ? consoleTheme.readyStatus : phase}</Text>
            </Text>
            <Text>
              <Text color={theme.muted}>SCAN </Text>
              <Text color={theme.accentAlt}>{startupProgress(frame, progressWidth)}</Text>
            </Text>
            {info ? (
              <>
                {mode === "compact" && (
                  <Text color={theme.accentAlt}>TOOLS {info.tools}</Text>
                )}
                <Text color={theme.text}>
                  {mode !== "compact" && (
                    <>
                      <Text color={theme.accentAlt}>TOOLS {info.tools}</Text>
                      <Text color={theme.subtle}>  ·  </Text>
                    </>
                  )}
                  <Text color={theme.accentAlt}>SKILLS {info.skills}</Text>
                  <Text color={theme.subtle}>  ·  </Text>
                  <Text color={mcp?.errors ? theme.warning : theme.success}>MCP {mcp?.ready}/{mcp?.total}</Text>
                  <Text color={theme.subtle}>  ·  </Text>
                  <Text color={info.learning.error ? theme.warning : theme.success}>LEARN {learningState}</Text>
                </Text>
                <Text color={theme.muted}>{consoleTheme.handoff}</Text>
              </>
            ) : (
              <Text color={theme.muted}>SIGNAL // {consoleTheme.waiting}</Text>
            )}
          </Box>
        </Box>

        <Text color={theme.subtle}>{"─".repeat(Math.max(20, panelWidth - (mode === "compact" ? 6 : 8)))}</Text>
        <Box justifyContent="space-between" overflow="hidden">
          <Text color={theme.muted}>{consoleTheme.systemsLabel}</Text>
          <Text color={theme.muted}>{info?.model ? shorten(info.model, 28) : "NEGOTIATING LINK"}</Text>
        </Box>
      </Box>
    </Box>
  );
}
