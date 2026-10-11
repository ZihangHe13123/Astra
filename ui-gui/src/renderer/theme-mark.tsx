import React from "react";
import { THEMES, isThemeName, markLineSegments, type UiTheme } from "@astra/ui-core/themes";
import { useTheme } from "./theme-context.js";

/** The terminal names a mark's colours by role; here each role is the property the theme module set. */
const ROLE_COLOURS = {
  text: "var(--text)", muted: "var(--muted)", subtle: "var(--line)", accent: "var(--accent)", accentAlt: "var(--accent-2)",
  emphasis: "var(--value)", header: "var(--value)", code: "var(--code)", success: "var(--ok)", warning: "var(--warn)",
  danger: "var(--error)", border: "var(--frame)",
} as unknown as UiTheme;

/** A theme's mark as the terminal draws it: its first three lines in the accent, the rest in the second accent.
 * Every line is its own element, so no line break has to survive between them. */
export function ThemeMark({ className }: { className: string }) {
  const theme = useTheme();
  if (!isThemeName(theme.name)) return null;
  const { mark, markPalette } = THEMES[theme.name].console;
  const indent = Math.min(...mark.map(line => line.length - line.trimStart().length));
  return <span className={className} aria-hidden="true">{mark.map((line, index) =>
    <span key={index}>{markLineSegments(line.slice(indent), markPalette, ROLE_COLOURS, index < 3 ? "var(--accent)" : "var(--accent-2)").map((segment, at) =>
      // A drawn mark uses "." for the cells it leaves empty.
      <span key={at} style={{ color: segment.color }}>{markPalette && /^\.+$/.test(segment.text) ? " ".repeat(segment.text.length) : segment.text}</span>)}</span>)}</span>;
}
