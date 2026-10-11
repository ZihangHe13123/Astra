import React from "react";
import { Box, Text } from "ink";
import stringWidth from "string-width";
import type { BarDrinkState } from "../types.js";
import { useTheme } from "../theme-context.js";

/** Where a glass would be when there is none. */
export const NO_GLASS = "···";

export function drinkFillMeter(fill: number): string {
  const level = Math.max(0, Math.min(3, Math.round(fill)));
  return "█".repeat(level) + "░".repeat(3 - level);
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

export type BarShelfLayout = {
  compact: boolean;
  prefix: string;
  hint: string;
  spacer: string;
  note: string;
};

export function drinkTemperatureLabel(temperature: BarDrinkState["temperature"]): string {
  return temperature === "room" ? "ROOM TEMP" : temperature.toUpperCase();
}

export function barShelfLayout(drink: BarDrinkState, columns: number): BarShelfLayout {
  const compact = columns < 70;
  const contentWidth = Math.max(20, columns - 5);
  const temperature = drinkTemperatureLabel(drink.temperature);
  const hint = drink.active && drink.fill > 0
    ? compact ? "/sip" : "/sip to drink"
    : drink.fill === 0 && drink.active ? "EMPTY" : "LYRA WILL MIX";
  const label = compact ? "SHELF // " : "BAR SHELF // ";
  const rawPrefix = drink.active
    ? `${drinkFillMeter(drink.fill)} ${drink.name} · ${temperature}`
    : `${NO_GLASS} GLASS WAITING`;
  const prefixBudget = Math.max(8, contentWidth - stringWidth(label) - stringWidth(hint) - 2);
  const prefix = shorten(rawPrefix, prefixBudget);
  const spacerWidth = Math.max(1, contentWidth - stringWidth(label) - stringWidth(prefix) - stringWidth(hint));
  const noteLabel = "TASTING // ";
  const noteBudget = Math.max(0, contentWidth - stringWidth(noteLabel));
  const note = !compact && drink.active && drink.note && noteBudget >= 8
    ? shorten(drink.note, noteBudget)
    : "";
  return { compact, prefix, hint, spacer: " ".repeat(spacerWidth), note };
}

const SIPS = ["empty", "one sip left", "two sips left", "full glass"];
const GLASS_WIDTH = 9;

export type GlassPiece = { text: string; part: "glass" | "liquid" | "ice" | "steam" };

/**
 * The glass as it stands, four rows of nine cells: a tumbler, with ice when the drink is cold,
 * or a mug that steams while there is something hot in it. Each sip lowers the drink a third.
 */
export function drinkGlass(drink: Pick<BarDrinkState, "temperature" | "fill">): GlassPiece[][] {
  const level = Math.max(0, Math.min(3, Math.round(drink.fill)));
  const hot = drink.temperature === "hot";
  const upper = level === 3 ? "█████" : level === 2 ? "▃▃▃▃▃" : "     ";
  const lower = level >= 2 ? "█████" : level === 1 ? "▅▅▅▅▅" : "     ";
  // Ice floats on the drink, and is what is left at the bottom of an empty glass.
  const iceRow = drink.temperature === "cold" ? (level >= 2 ? "upper" : "lower") : "";
  const inside = (fill: string, ice: boolean): GlassPiece[] => ice
    ? [...fill].map((cell, index): GlassPiece => index % 2 ? { text: "□", part: "ice" } : { text: cell, part: "liquid" })
    : [{ text: fill, part: "liquid" }];
  const glass = (text: string): GlassPiece => ({ text, part: "glass" });
  return [
    hot ? [{ text: level ? "  ~ ~    " : " ".repeat(GLASS_WIDTH), part: "steam" }] : [glass("╷     ╷  ")],
    [glass("│"), ...inside(upper, iceRow === "upper"), glass(hot ? "├╮ " : "│  ")],
    [glass("│"), ...inside(lower, iceRow === "lower"), glass(hot ? "├╯ " : "│  ")],
    [glass("╰─────╯  ")],
  ];
}

/** `text` on at most `lines` rows of `width` cells, breaking between words where there are words. */
function wrapCells(text: string, width: number, lines: number): string[] {
  const rows: string[] = [];
  let rest = text.replace(/\s+/g, " ").trim();
  while (rest && rows.length < lines) {
    if (stringWidth(rest) <= width) { rows.push(rest); break; }
    if (rows.length === lines - 1) { rows.push(shorten(rest, width)); break; }
    let line = "";
    for (const char of rest) {
      if (stringWidth(line + char) > width) break;
      line += char;
    }
    const space = line.lastIndexOf(" ");
    const next = rest.slice(line.length);
    const midWord = /[\x21-\x7e]$/.test(line) && /^[\x21-\x7e]/.test(next);
    if (midWord && space > line.length / 2) line = line.slice(0, space);
    // A row does not open with closing punctuation: the character before it goes down with it.
    else if (/^[，。、；：！？）》」』”’,.;:!?)]/.test(next) && [...line].length > 1) line = [...line].slice(0, -1).join("");
    rows.push(line.trimEnd());
    rest = rest.slice(line.length).trimStart();
  }
  return rows;
}

export type BarDrinkCard = {
  glass: GlassPiece[][];
  name: string;
  hint: string;
  spacer: string;
  temperature: string;
  sips: string;
  note: string[];
};

/** A poured drink drawn as a card: the glass, and beside it its name, how much is left and how it tastes. */
export function barDrinkCard(drink: BarDrinkState, columns: number): BarDrinkCard {
  const width = Math.max(12, columns - 4 - GLASS_WIDTH - 1);
  const level = Math.max(0, Math.min(3, Math.round(drink.fill)));
  const hint = level > 0 ? "/sip to drink" : "";
  const name = shorten(drink.name, Math.max(4, width - (hint ? stringWidth(hint) + 2 : 0)));
  return {
    glass: drinkGlass(drink),
    name,
    hint,
    spacer: " ".repeat(Math.max(1, width - stringWidth(name) - stringWidth(hint))),
    temperature: drinkTemperatureLabel(drink.temperature),
    sips: SIPS[level],
    note: wrapCells(drink.note, width, 2),
  };
}

/** Below this many terminal rows a poured drink stays on one line, leaving the room to the talk. */
export const DRINK_CARD_MIN_ROWS = 24;

export function BarShelf({ drink, columns, rows }: { drink: BarDrinkState; columns: number; rows?: number }) {
  const theme = useTheme();
  const toneColor = {
    amber: theme.warning,
    cyan: theme.accentAlt,
    pink: theme.accent,
    clear: theme.text,
  }[drink.tone];
  const layout = barShelfLayout(drink, columns);

  if (drink.active && !layout.compact && (rows === undefined || rows >= DRINK_CARD_MIN_ROWS)) {
    const card = barDrinkCard(drink, columns);
    const partColor = { glass: theme.muted, liquid: toneColor, ice: theme.text, steam: theme.muted };
    const temperatureColor = { hot: theme.warning, cold: theme.accentAlt, room: theme.muted }[drink.temperature];
    return (
      <Box borderStyle="single" borderColor={toneColor} paddingX={1} height={6} overflow="hidden">
        <Box flexDirection="column" flexShrink={0} marginRight={1}>
          {card.glass.map((row, index) => (
            <Text key={index}>
              {row.map((piece, at) => <Text key={at} color={partColor[piece.part]}>{piece.text}</Text>)}
            </Text>
          ))}
        </Box>
        <Box flexDirection="column">
          <Text wrap="truncate-end">
            <Text bold color={toneColor}>{card.name}</Text>
            <Text color={theme.muted}>{card.spacer}{card.hint}</Text>
          </Text>
          <Text wrap="truncate-end">
            <Text color={temperatureColor}>{card.temperature}</Text>
            <Text color={theme.muted}> · {card.sips}</Text>
          </Text>
          {card.note.map((line, index) => <Text key={index} color={theme.text} wrap="truncate-end">{line}</Text>)}
        </Box>
      </Box>
    );
  }

  if (layout.compact) {
    return (
      <Box borderStyle="single" borderColor={drink.active ? toneColor : theme.border} paddingX={1} height={3} overflow="hidden">
        <Text bold color={theme.accent}>SHELF // </Text>
        <Text color={toneColor}>{layout.prefix}</Text>
        <Text color={theme.muted}>{layout.spacer}{layout.hint}</Text>
      </Box>
    );
  }

  return (
    <Box
      borderStyle="single"
      borderColor={drink.active ? toneColor : theme.border}
      paddingX={1}
      height={layout.note ? 4 : 3}
      overflow="hidden"
      flexDirection="column"
    >
      <Box>
        <Text bold color={theme.accent}>BAR SHELF // </Text>
        <Text color={toneColor}>{layout.prefix}</Text>
        <Text color={theme.muted}>{layout.spacer}{layout.hint}</Text>
      </Box>
      {layout.note && (
        <Text>
          <Text bold color={theme.muted}>TASTING // </Text>
          <Text color={theme.muted}>{layout.note}</Text>
        </Text>
      )}
    </Box>
  );
}
