import React, { useEffect, useState } from "react";
import { Box, Text } from "ink";
import stringWidth from "string-width";
import { useTheme } from "../theme-context.js";
import type { BarAmbianceState, BarShiftState, LyraGlassState } from "../types.js";
import type { RuntimeMode } from "../types.js";
import { responsiveMode } from "./app-header.js";

export function formatBarClock(now: number): string {
  return new Date(now).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
}

export function isQuietBarAction(input: string, mode: RuntimeMode): boolean {
  return mode === "bar" && /^\/(?:sip|bar\s+(?:sip|drink))$/i.test(input.trim());
}

const PHASE_LABELS: Record<BarShiftState["phase"], string> = {
  early: "EARLY SHIFT",
  deep: "DEEP NIGHT",
  late: "LATE NIGHT",
  last_call: "LAST CALL",
};

export function formatBarAmbiance(ambiance: BarAmbianceState, shift: BarShiftState): string {
  return [PHASE_LABELS[shift.phase], formatBarEnvironment(ambiance)]
    .join(" // ");
}

export function formatBarEnvironment(ambiance: BarAmbianceState): string {
  return [ambiance.weather, ambiance.power, ambiance.music, ambiance.radio]
    .map((part) => part.replaceAll("_", " ").toUpperCase())
    .join(" · ");
}

export function formatLyraGlass(glass: LyraGlassState): string {
  if (!glass.active) return "LYRA [---] WATER WAITING";
  const fill = Math.max(0, Math.min(3, Math.round(glass.fill)));
  return `LYRA [${"#".repeat(fill)}${".".repeat(3 - fill)}] ${glass.name}`;
}

function shorten(value: string, width: number): string {
  if (stringWidth(value) <= width) return value;
  if (width <= 1) return "…";
  let result = "";
  for (const char of value) {
    if (stringWidth(result + char) >= width) break;
    result += char;
  }
  return `${result}…`;
}

function useBarClock(): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

export function BarHeader({ columns }: { columns: number }) {
  const theme = useTheme();
  const mode = responsiveMode(columns);
  const now = useBarClock();
  const clock = formatBarClock(now);

  if (mode === "compact") {
    return (
      <Box borderStyle="double" borderColor={theme.accent} paddingX={1} height={3} overflow="hidden">
        <Text bold color={theme.accent}>LYRA'S BAR</Text>
        <Text color={theme.subtle}> // </Text>
        <Text color={theme.accentAlt}>PRIVATE NIGHT</Text>
        <Text color={theme.muted}> // {clock}</Text>
      </Box>
    );
  }

  return (
    <Box borderStyle="double" borderColor={theme.accent} paddingX={1} height={5} overflow="hidden">
      <Text bold color={theme.accentAlt}>{" /\\_/\\ \n( -.- )\n /|_|\\ "}</Text>
      <Box flexDirection="column" marginLeft={2}>
        <Text>
          <Text bold color={theme.accent}>GLITCH CITY // LYRA'S BAR</Text>
          <Text color={theme.subtle}>  ::  </Text>
          <Text bold color={theme.warning}>{clock}</Text>
        </Text>
        <Text color={theme.accentAlt}>LYRA // ON DUTY</Text>
        <Text color={theme.muted}>CHANNEL 07 LINK  //  PRIVATE SHIFT  //  MEMORY SEALED</Text>
      </Box>
    </Box>
  );
}

export function BarStatusDock({
  session,
  busy,
  disconnected,
  notice,
  ambiance,
  shift,
  lyraGlass,
  outputMode,
  columns,
}: {
  session: string;
  busy: boolean;
  disconnected: boolean;
  notice?: string;
  ambiance: BarAmbianceState;
  shift: BarShiftState;
  lyraGlass: LyraGlassState;
  outputMode: "atomic" | "stream";
  columns: number;
}) {
  const theme = useTheme();
  const mode = responsiveMode(columns);
  const state = disconnected ? "SIGNAL LOST" : busy ? "LYRA IS MIXING" : "BAR OPEN";
  const stateColor = disconnected ? theme.danger : busy ? theme.warning : theme.success;
  const phase = PHASE_LABELS[shift.phase];
  const environment = notice || formatBarEnvironment(ambiance);
  const lyraCup = formatLyraGlass(lyraGlass);
  const outputLabel = outputMode.toUpperCase();
  const outputColor = outputMode === "atomic" ? theme.warning : theme.accentAlt;
  const contentWidth = Math.max(20, columns - 5);

  if (mode === "compact") {
    return (
      <Box borderStyle="single" borderColor={stateColor} paddingX={1} height={3} overflow="hidden">
        <Text bold color={stateColor}>{disconnected ? "X" : busy ? "*" : "o"} {state}</Text>
        <Text color={theme.subtle}> | </Text>
        <Text color={theme.accent}>{shorten(session || "UNNAMED", 15)}</Text>
        <Text color={theme.subtle}> | </Text>
        <Text bold color={outputColor}>{outputLabel}</Text>
        <Text color={theme.subtle}> | </Text><Text color={theme.muted}>/bar leave</Text>
      </Box>
    );
  }

  if (mode === "standard") {
    const cupBudget = Math.min(21, Math.max(14, Math.floor(contentWidth * 0.32)));
    const environmentBudget = Math.max(10, contentWidth - stringWidth("ENV // ") - stringWidth("  |  ") - cupBudget);
    return (
      <Box borderStyle="single" borderColor={stateColor} paddingX={1} height={4} overflow="hidden" flexDirection="column">
        <Text>
          <Text bold color={stateColor}>{disconnected ? "X" : busy ? "*" : "o"} {state}</Text>
          <Text color={theme.subtle}>  |  </Text>
          <Text bold color={theme.warning}>{phase}</Text>
          <Text color={theme.subtle}>  |  </Text>
          <Text bold color={outputColor}>{outputLabel}</Text>
          <Text color={theme.subtle}>  |  </Text><Text color={theme.muted}>/bar leave</Text>
        </Text>
        <Text>
          <Text bold color={theme.muted}>ENV // </Text>
          <Text color={notice ? theme.warning : theme.muted}>{shorten(environment, environmentBudget)}</Text>
          <Text color={theme.subtle}>  |  </Text>
          <Text color={lyraGlass.active ? theme.accentAlt : theme.muted}>{shorten(lyraCup, cupBudget)}</Text>
        </Text>
      </Box>
    );
  }

  const sessionBudget = Math.min(38, Math.max(14, contentWidth - stringWidth(state) - stringWidth(phase) - 35));
  const lyraBudget = Math.min(32, Math.max(20, Math.floor(contentWidth * 0.26)));
  const environmentBudget = Math.max(
    18,
    contentWidth - stringWidth("AMBIENCE // ") - stringWidth("  |  ") - lyraBudget,
  );

  return (
    <Box borderStyle="single" borderColor={stateColor} paddingX={1} height={4} overflow="hidden" flexDirection="column">
      <Text>
        <Text bold color={stateColor}>{disconnected ? "X" : busy ? "*" : "o"} {state}</Text>
        <Text color={theme.subtle}>  |  </Text><Text color={theme.muted}>SHIFT </Text>
        <Text color={theme.accent}>{shorten(session || "UNNAMED", sessionBudget)}</Text>
        <Text color={theme.subtle}>  |  </Text>
        <Text bold color={theme.warning}>{phase}</Text>
        <Text color={theme.subtle}>  |  </Text>
        <Text bold color={outputColor}>{outputLabel}</Text>
        <Text color={theme.subtle}>  |  </Text><Text color={theme.muted}>/bar leave</Text>
      </Text>
      <Text>
        <Text bold color={theme.muted}>AMBIENCE // </Text>
        <Text color={notice ? theme.warning : theme.muted}>{shorten(environment, environmentBudget)}</Text>
        <Text color={theme.subtle}>  |  </Text>
        <Text color={lyraGlass.active ? theme.accentAlt : theme.muted}>{shorten(lyraCup, lyraBudget)}</Text>
      </Text>
    </Box>
  );
}
