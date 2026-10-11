import React from "react";
import { Box, Text } from "ink";
import { useTheme } from "../theme-context.js";
import type { LocalModeDefinition, RuntimeMode } from "../types.js";

export type ResponsiveMode = "compact" | "standard" | "wide";

export function responsiveMode(columns: number): ResponsiveMode {
  if (columns < 70) return "compact";
  if (columns <= 110) return "standard";
  return "wide";
}

function shorten(value: string, max: number): string {
  if (value.length <= max) return value;
  if (max <= 1) return "…";
  return `${value.slice(0, max - 1)}…`;
}

export function AppHeader({ columns, busy, mode: runtimeMode = "work", localMode }: { columns: number; busy: boolean; mode?: RuntimeMode; localMode?: LocalModeDefinition | null }) {
  const theme = useTheme();
  const chrome = theme.chrome;
  const mode = responsiveMode(columns);
  const minimal = runtimeMode === "minimal";
  const local = runtimeMode === "local";
  const brand = shorten(local ? localMode?.ui.brand ?? "LOCAL MODE" : minimal ? "MINIMAL MODE" : chrome?.brand ?? "Agent System", mode === "compact" ? 22 : 30);
  const action = busy
    ? "cancel"
    : runtimeMode === "bar"
      ? "leave bar"
      : minimal
        ? "leave minimal"
        : local
          ? localMode?.ui.exitLabel ?? "leave local mode"
          : chrome?.exitLabel ?? "exit";
  const headerTag = runtimeMode === "bar"
    ? "PRIVATE SHIFT · MEMORY OFF"
    : minimal
      ? "ZERO INJECTION · TWO TOOLS"
      : local
        ? localMode?.ui.headerTag ?? "PRIVATE · NO TOOLS · NO MEMORY"
        : chrome?.headerTag;
  const hints = mode === "compact"
    ? `  ^L activity  ^O tools  ^C ${action}`
    : mode === "standard"
      ? `  ^L activity  ^O tools  /help  ^C ${action}`
      : `${chrome ? "  //  " : "  "}Ctrl+L activity  Ctrl+O tools  Ctrl+C ${action}`;

  return (
    <Box borderStyle={chrome?.headerFrameStyle ?? chrome?.frameStyle ?? "single"} borderColor={theme.border} paddingX={1} overflow="hidden">
      <Text wrap="truncate-end">
        <Text bold={theme.prefixBold} color={theme.name === "classic" ? theme.header : theme.accent}>{brand}</Text>
        {headerTag && mode === "wide" && (
          <Text bold color={theme.accentAlt}>{`  ${headerTag}`}</Text>
        )}
        <Text color={theme.muted}>{hints}</Text>
      </Text>
    </Box>
  );
}
