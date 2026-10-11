import React from "react";
import { Box, Text } from "ink";
import stringWidth from "string-width";
import type { BarDrinkState } from "../types.js";
import { useTheme } from "../theme-context.js";

export function drinkFillMeter(fill: number): string {
  const level = Math.max(0, Math.min(3, Math.round(fill)));
  return `[${"#".repeat(level)}${".".repeat(3 - level)}]`;
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
    : "[---] GLASS WAITING";
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

export function BarShelf({ drink, columns }: { drink: BarDrinkState; columns: number }) {
  const theme = useTheme();
  const toneColor = {
    amber: theme.warning,
    cyan: theme.accentAlt,
    pink: theme.accent,
    clear: theme.text,
  }[drink.tone];
  const layout = barShelfLayout(drink, columns);

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
