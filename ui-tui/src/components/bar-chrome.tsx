import React, { useEffect, useState } from "react";
import { Box, Text } from "ink";
import stringWidth from "string-width";
import { useTheme } from "../theme-context.js";
import type { BarAmbianceState, BarShiftState, LyraGlassState } from "../types.js";
import type { RuntimeMode } from "../types.js";
import { responsiveMode } from "./app-header.js";
import { drinkFillMeter, NO_GLASS } from "./bar-shelf.js";

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

const ROOM: Record<keyof BarAmbianceState, Record<string, string>> = {
  weather: { drizzle: "drizzle outside", rain: "rain outside", downpour: "downpour outside", clearing: "sky clearing" },
  power: { stable: "lights steady", flicker: "lights flickering", brownout: "brownout" },
  music: { silent: "no music", low_synth: "♪ low synth", old_radio: "♪ old radio", jukebox: "♪ jukebox" },
  radio: { static: "radio static", local_news: "local news on air", weather: "weather report on air", emergency: "emergency broadcast" },
};
const ROOM_ORDER = ["weather", "power", "music", "radio"] as const;

/** The room in a few words: as phrases when they fit `width`, else the bare words, else fewer of them. */
export function formatBarRoom(ambiance: BarAmbianceState, width = Number.POSITIVE_INFINITY): string {
  const words = ROOM_ORDER.map((key) => String(ambiance[key]).replaceAll("_", " "));
  const phrases = ROOM_ORDER.map((key, index) => ROOM[key][ambiance[key]] ?? words[index]);
  if (stringWidth(phrases.join(" · ")) <= width) return phrases.join(" · ");
  for (let count = words.length; count > 0; count--) {
    const line = words.slice(0, count).join(" · ");
    if (stringWidth(line) <= width) return line;
  }
  return shorten(words[0], width);
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

type Piece = { text: string; color?: string; bold?: boolean };

const piecesWidth = (pieces: Piece[]) => pieces.reduce((sum, piece) => sum + stringWidth(piece.text), 0);

function Row({ left, right, width }: { left: Piece[]; right: Piece[]; width: number }) {
  const gap = Math.max(right.length ? 1 : 0, width - piecesWidth(left) - piecesWidth(right));
  return (
    <Text wrap="truncate-end">
      {left.map((piece, index) => <Text key={`l${index}`} bold={piece.bold} color={piece.color}>{piece.text}</Text>)}
      {" ".repeat(gap)}
      {right.map((piece, index) => <Text key={`r${index}`} bold={piece.bold} color={piece.color}>{piece.text}</Text>)}
    </Text>
  );
}

export type BarSign = {
  compact: boolean;
  face: string;
  title: string;
  phase: string;
  clock: string;
  mark: string;
  state: string;
  session: string;
  notice: string;
  /** The reply mode with or without its label, and the way out: as much of the two as fits. */
  output: string;
  leave: string;
  room: string;
  /** Lyra's own glass; both empty when the sign has no room for it. */
  lyraMeter: string;
  lyraName: string;
};

/** What the bar's sign says at this width; every row fits in `columns` without wrapping. */
export function barSign(input: {
  session: string;
  busy: boolean;
  disconnected: boolean;
  notice?: string;
  ambiance: BarAmbianceState;
  shift: BarShiftState;
  lyraGlass: LyraGlassState;
  outputMode: "atomic" | "stream";
  columns: number;
  now: number;
}): BarSign {
  const compact = responsiveMode(input.columns) === "compact";
  // Two cells of frame and two of padding; beside the cat, seven more and a gap of two.
  const width = Math.max(8, input.columns - 4 - (compact ? 0 : 9));
  const state = input.disconnected ? "SIGNAL LOST" : input.busy ? "LYRA IS MIXING" : "BAR OPEN";
  const mark = input.disconnected ? "✕" : input.busy ? "◉" : "●";
  const face = input.disconnected ? "( x.x )" : input.busy ? "( o.o )" : "( -.- )";

  const title = compact ? "LYRA'S BAR" : "GLITCH CITY // LYRA'S BAR";
  const fullClock = formatBarClock(input.now);
  const fullPhase = PHASE_LABELS[input.shift.phase];
  const titleRoom = width - stringWidth(title) - 2;
  const clock = titleRoom >= stringWidth(fullClock) ? fullClock : "";
  const phase = clock && titleRoom >= stringWidth(fullPhase) + 2 + stringWidth(fullClock) ? fullPhase : "";

  const notice = input.notice ?? "";
  // On a narrow sign a notice takes the session's place; a wide one has a line for it.
  const beside = compact && notice ? notice : input.session || "unnamed";
  const stateWidth = stringWidth(mark) + 1 + stringWidth(state);
  const asides = [
    { output: `output ${input.outputMode}`, leave: "/bar leave" },
    { output: input.outputMode, leave: "/bar leave" },
    { output: "", leave: "/bar leave" },
    { output: "", leave: "" },
  ];
  const asideWidth = (aside: { output: string; leave: string }) =>
    stringWidth(aside.output) + (aside.output && aside.leave ? 3 : 0) + stringWidth(aside.leave);
  const left = (aside: { output: string; leave: string }) =>
    width - stateWidth - (asideWidth(aside) ? asideWidth(aside) + 2 : 0) - 3;
  const wanted = Math.min(stringWidth(beside), 16);
  // The reply mode gives way to the name beside the state; the way out gives way to nothing
  // but the state itself.
  const aside = asides.slice(0, 2).find((candidate) => left(candidate) >= wanted)
    ?? (left(asides[2]) >= -3 ? asides[2] : asides[3]);
  const besideText = left(aside) >= 2 ? shorten(beside, left(aside)) : "";

  const glass = input.lyraGlass;
  const showLyra = !compact && width >= 34;
  const lyraMeter = showLyra ? (glass.active ? drinkFillMeter(glass.fill) : NO_GLASS) : "";
  const lyraLabel = "LYRA ";
  const lyraName = showLyra
    ? shorten(glass.active ? glass.name : "water waiting", Math.max(13, Math.floor(width * 0.34) - stringWidth(lyraLabel) - 4))
    : "";
  const lyraWidth = showLyra ? stringWidth(lyraLabel) + stringWidth(lyraMeter) + 1 + stringWidth(lyraName) : 0;
  const roomWidth = width - (lyraWidth ? lyraWidth + 2 : 0);
  const room = compact ? "" : notice ? shorten(notice, roomWidth) : formatBarRoom(input.ambiance, roomWidth);

  return {
    compact, face, title, phase, clock, mark, state,
    session: compact && notice ? "" : besideText,
    notice: compact ? (notice ? besideText : "") : notice ? room : "",
    output: aside.output,
    leave: aside.leave,
    room: notice ? "" : room,
    lyraMeter,
    lyraName,
  };
}

/** The bar's sign and its status in one frame: who is in, how late it is, what the room is like. */
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
  const now = useBarClock();
  const sign = barSign({ session, busy, disconnected, notice, ambiance, shift, lyraGlass, outputMode, columns, now });
  const width = Math.max(8, columns - 4 - (sign.compact ? 0 : 9));
  const stateColor = disconnected ? theme.danger : busy ? theme.warning : theme.success;
  const frameColor = disconnected ? theme.danger : theme.accent;
  const dot: Piece = { text: " · ", color: theme.muted };

  const titleRow = {
    left: [{ text: sign.title, color: theme.accent, bold: true }],
    right: [
      ...(sign.phase ? [{ text: `${sign.phase}  `, color: theme.accentAlt }] : []),
      ...(sign.clock ? [{ text: sign.clock, color: theme.warning, bold: true }] : []),
    ],
  };
  const [outputLabel, outputValue] = sign.output.includes(" ") ? sign.output.split(" ") : ["", sign.output];
  const stateRow = {
    left: [
      { text: `${sign.mark} ${sign.state}`, color: stateColor, bold: true },
      ...(sign.session ? [dot, { text: sign.session, color: theme.text }] : []),
      ...(sign.compact && sign.notice ? [dot, { text: sign.notice, color: theme.warning }] : []),
    ],
    right: [
      ...(outputLabel ? [{ text: `${outputLabel} `, color: theme.muted }] : []),
      ...(outputValue ? [{ text: outputValue, color: outputMode === "stream" ? theme.accentAlt : theme.text }, dot] : []),
      ...(sign.leave ? [{ text: sign.leave, color: theme.muted }] : []),
    ],
  };

  if (sign.compact) {
    return (
      <Box borderStyle="double" borderColor={frameColor} paddingX={1} height={4} overflow="hidden" flexDirection="column">
        <Row {...titleRow} width={width} />
        <Row {...stateRow} width={width} />
      </Box>
    );
  }

  const catColor = disconnected ? theme.danger : theme.accentAlt;
  return (
    <Box borderStyle="double" borderColor={frameColor} paddingX={1} height={5} overflow="hidden">
      <Box flexDirection="column" flexShrink={0}>
        <Text bold color={catColor}>{" /\\_/\\ "}</Text>
        <Text bold color={catColor}>{sign.face}</Text>
        <Text bold color={catColor}>{" /|_|\\ "}</Text>
      </Box>
      <Box flexDirection="column" marginLeft={2}>
        <Row {...titleRow} width={width} />
        <Row {...stateRow} width={width} />
        <Row
          left={[sign.notice ? { text: sign.notice, color: theme.warning } : { text: sign.room, color: theme.muted }]}
          right={sign.lyraMeter ? [
            { text: "LYRA ", color: theme.muted, bold: true },
            { text: `${sign.lyraMeter} `, color: lyraGlass.active ? theme.accentAlt : theme.muted },
            { text: sign.lyraName, color: lyraGlass.active ? theme.text : theme.muted },
          ] : []}
          width={width}
        />
      </Box>
    </Box>
  );
}
