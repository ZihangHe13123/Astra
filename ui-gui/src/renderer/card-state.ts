/** What the desktop decides about an interactive card before and while its frame runs. */
export const CARD_LANGUAGE = "card";
export const CARD_FRAME_URL = "astra://card/frame.html";
/** Beyond this the block is shown as source: nothing that size is an explanation aid. */
export const MAX_CARD_SOURCE = 200_000;
export const MIN_CARD_HEIGHT = 40;
export const MAX_CARD_HEIGHT = 2400;

export function isCardLanguage(language: string): boolean { return language.trim().toLowerCase() === CARD_LANGUAGE; }

/** The frame says how tall its content is; the host believes it only within these bounds. */
export function cardHeight(reported: unknown): number | undefined {
  if (typeof reported !== "number" || !Number.isFinite(reported)) return undefined;
  return Math.min(MAX_CARD_HEIGHT, Math.max(MIN_CARD_HEIGHT, Math.ceil(reported)));
}

export type CardMessage = { type: "ready" } | { type: "height"; height: number } | { type: "error"; message: string };
/** A card's script can post anything to the host. Only these three reports mean something, and none grants anything. */
export function cardMessage(data: unknown): CardMessage | undefined {
  if (!data || typeof data !== "object" || (data as { astraCard?: unknown }).astraCard !== true) return undefined;
  const { type, height, message } = data as { type?: unknown; height?: unknown; message?: unknown };
  if (type === "ready") return { type };
  if (type === "height") { const value = cardHeight(height); return value === undefined ? undefined : { type, height: value }; }
  if (type === "error" && typeof message === "string") return { type, message: message.slice(0, 300) };
  return undefined;
}

const THEME_COLORS = ["--bg", "--panel", "--soft", "--hover", "--line", "--text", "--muted", "--accent", "--on-accent", "--error"] as const;
export type CardTheme = { dark: boolean; colors: Record<string, string> };
/** The colours a card may use so that it reads as part of the reply, in whichever theme is active. */
export function cardTheme(style: { getPropertyValue(name: string): string; colorScheme?: string }): CardTheme {
  const colors: Record<string, string> = {};
  for (const name of THEME_COLORS) { const value = style.getPropertyValue(name).trim(); if (value) colors[name] = value; }
  return { dark: String(style.colorScheme || "").includes("dark"), colors };
}

/** Rows leave and re-enter the virtual list; a card that comes back starts at the height it had. */
const remembered = new Map<string, number>();
export function cardSourceKey(source: string): string {
  let hash = 5381;
  for (let i = 0; i < source.length; i++) hash = (hash * 33 ^ source.charCodeAt(i)) >>> 0;
  return `${source.length}:${hash}`;
}
export function rememberedCardHeight(source: string): number | undefined { return remembered.get(cardSourceKey(source)); }
export function rememberCardHeight(source: string, height: number): void {
  const key = cardSourceKey(source);
  remembered.delete(key); remembered.set(key, height);
  if (remembered.size > 200) remembered.delete(remembered.keys().next().value!);
}
