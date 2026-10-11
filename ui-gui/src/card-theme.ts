/** The app's colours as a card or a page is handed them. The window reads them; the host process checks what it is told. */
export const CARD_THEME_COLORS = ["--bg", "--panel", "--soft", "--hover", "--line", "--text", "--muted", "--accent", "--on-accent", "--error"] as const;
export type CardTheme = { dark: boolean; colors: Record<string, string> };

/** The colours a card may use so that it reads as part of the reply, in whichever theme is active. */
export function cardTheme(style: { getPropertyValue(name: string): string; colorScheme?: string }): CardTheme {
  const colors: Record<string, string> = {};
  for (const name of CARD_THEME_COLORS) { const value = style.getPropertyValue(name).trim(); if (value) colors[name] = value; }
  return { dark: String(style.colorScheme || "").includes("dark"), colors };
}

/** A theme that crossed a process boundary: only the known names, each a short plain value. */
export function checkedCardTheme(value: unknown): CardTheme | undefined {
  if (!value || typeof value !== "object") return undefined;
  const { dark, colors } = value as { dark?: unknown; colors?: unknown };
  const checked: Record<string, string> = {};
  if (colors && typeof colors === "object") for (const name of CARD_THEME_COLORS) {
    const color = (colors as Record<string, unknown>)[name];
    if (typeof color === "string" && color.length <= 80 && /^[#(),.%\s\w-]+$/.test(color)) checked[name] = color;
  }
  return { dark: dark === true, colors: checked };
}
