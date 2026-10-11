/** What a theme looks like in the desktop: every colour, the frame shape and the wording, worked out
 * from the definitions both clients share. The terminal draws on the user's terminal background; the
 * desktop has to supply its own surfaces, and it keeps text readable on them whatever a theme says. */
import { THEMES, THEME_NAMES, isThemeName, type ThemeName, type UiTheme } from "@astra/ui-core/themes";

export const PLAIN_THEMES = ["system", "light", "dark"] as const;
export type PlainTheme = typeof PLAIN_THEMES[number];
export type DesktopThemeName = PlainTheme | ThemeName;
export const DESKTOP_THEME_NAMES: readonly DesktopThemeName[] = [...PLAIN_THEMES, ...THEME_NAMES];
export function isDesktopThemeName(value: string): value is DesktopThemeName {
  return (DESKTOP_THEME_NAMES as readonly string[]).includes(value);
}

export type FrameStyle = "single" | "round" | "double";
export type DesktopWording = {
  /** Shown beside the mark in the sidebar. */
  brand: string;
  tag: string;
  promptLabel: string;
  promptPlaceholder: string;
  marker: string;
  /** Headings of the work shown in the conversation: tools running, tools finished, files changed. */
  liveTools: string;
  archive: string;
  changes: string;
  roles: { user: string; assistant: string; reasoning: string; tool: string; error: string; system: string };
};
export type DesktopTheme = {
  name: DesktopThemeName;
  label: string;
  dark: boolean;
  /** A theme with its own voice: console lettering, its own names for the roles. */
  console: boolean;
  frame: FrameStyle;
  inputFrame: FrameStyle;
  wording: DesktopWording;
  /** CSS custom properties, by name. */
  colors: Record<string, string>;
};

type Rgb = [number, number, number];
/** The terminal's classic theme names its colours the way a terminal does. */
const ANSI: Record<string, string> = {
  black: "#0c0c0c", white: "#f2f2f2", gray: "#8c8c8c", grey: "#8c8c8c", yellow: "#c9a227",
  cyanBright: "#61d6d6", blueBright: "#6f9bff", magentaBright: "#e07be0", yellowBright: "#f4e36b",
  greenBright: "#5fd77a", redBright: "#f2666c",
};

export function rgb(color: string): Rgb {
  const hex = (ANSI[color] ?? color).replace("#", "");
  const full = hex.length === 3 ? [...hex].map(c => c + c).join("") : hex;
  if (!/^[0-9a-f]{6}$/i.test(full)) throw new Error(`Not a colour: ${color}`);
  return [0, 2, 4].map(at => parseInt(full.slice(at, at + 2), 16)) as Rgb;
}
function hex([r, g, b]: Rgb): string {
  return "#" + [r, g, b].map(v => Math.round(Math.min(255, Math.max(0, v))).toString(16).padStart(2, "0")).join("");
}
/** `amount` of `b` in `a`: 0 gives `a`, 1 gives `b`. */
export function mix(a: string, b: string, amount: number): string {
  const [x, y] = [rgb(a), rgb(b)];
  return hex([0, 1, 2].map(i => x[i] + (y[i] - x[i]) * amount) as Rgb);
}
function luminance(color: string): number {
  const [r, g, b] = rgb(color).map(v => { const c = v / 255; return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
/** WCAG contrast ratio, 1 to 21. */
export function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
/** `color`, moved toward `toward` only as far as it takes to reach `ratio` against `ground`. */
export function readable(color: string, ground: string, ratio: number, toward: string): string {
  let result = hex(rgb(color));
  for (let step = 1; step <= 20 && contrast(result, ground) < ratio; step++) result = mix(color, toward, step / 20);
  return result;
}

/** Page grounds for themes whose terminal definition has no panel colour of its own. */
const GROUNDS: Partial<Record<ThemeName, { bg: string; rail: string }>> = {
  classic: { bg: "#101010", rail: "#000000" },
  nord: { bg: "#2e3440", rail: "#272c36" },
  dracula: { bg: "#282a36", rail: "#21222c" },
  solarized: { bg: "#002b36", rail: "#00212b" },
  gruvbox: { bg: "#282828", rail: "#1d2021" },
  obsidian: { bg: "#000000", rail: "#0a0a0a" },
  moonlit: { bg: "#fbf8f3", rail: "#f1ece4" },
};

function build(name: DesktopThemeName, label: string, source: {
  bg: string; rail: string; text: string; muted: string; accent: string; accentAlt: string; emphasis: string;
  success: string; warning: string; danger: string; border: string; selected?: string; code: string;
  /** Code colours, when a theme has its own; otherwise they come from its accents. */
  syntax?: { keyword: string; string: string; number: string; title: string; builtin: string };
}, shape: Pick<DesktopTheme, "console" | "frame" | "inputFrame" | "wording">): DesktopTheme {
  const bg = hex(rgb(source.bg)), rail = hex(rgb(source.rail)), text = hex(rgb(source.text));
  const dark = luminance(bg) < 0.4;
  const far = dark ? "#ffffff" : "#000000";
  const panel = dark ? mix(bg, text, 0.045) : mix(bg, "#ffffff", 0.7);
  const soft = mix(bg, text, dark ? 0.07 : 0.045);
  const hover = mix(bg, text, dark ? 0.11 : 0.075);
  // Text has to read on the page and on the two surfaces it most often sits on.
  const onAll = (color: string, ratio: number) => [panel, soft, rail].reduce(
    (value, ground) => readable(value, ground, ratio, far), readable(color, bg, ratio, far));
  const accent = onAll(source.accent, 4.5);
  const colors: Record<string, string> = {
    "--bg": bg, "--sidebar": rail, "--panel": panel, "--soft": soft, "--hover": hover,
    "--line": mix(bg, text, dark ? 0.17 : 0.14),
    "--frame": readable(source.border, bg, 2, text),
    "--text": onAll(text, 4.5),
    "--muted": onAll(source.muted, 4.5),
    "--accent": accent,
    "--on-accent": contrast("#ffffff", accent) >= contrast("#14161a", accent) ? "#ffffff" : "#14161a",
    "--accent-2": onAll(source.accentAlt, 4.5),
    "--value": onAll(source.emphasis, 4.5),
    "--ok": onAll(source.success, 4.5),
    "--warn": onAll(source.warning, 4.5),
    "--error": onAll(source.danger, 4.5),
    "--selected": source.selected ? hex(rgb(source.selected)) : mix(bg, accent, 0.2),
    "--code": onAll(source.code, 4.5),
  };
  const syntax = source.syntax ?? { keyword: source.accent, string: source.success, number: source.emphasis, title: source.accentAlt, builtin: source.warning };
  for (const [part, value] of Object.entries(syntax)) colors[`--syn-${part}`] = readable(value, soft, 4.5, far);
  colors["--added"] = mix(bg, colors["--ok"], 0.16);
  colors["--removed"] = mix(bg, colors["--error"], 0.16);
  return { name, label, dark, colors, ...shape };
}

const PLAIN_WORDING: DesktopWording = {
  brand: "Astra", tag: "", promptLabel: "", promptPlaceholder: "", marker: "",
  liveTools: "正在执行", archive: "操作记录", changes: "本回合改动",
  roles: { user: "你", assistant: "Astra", reasoning: "推理 / 摘要", tool: "工具", error: "请求未完成", system: "提示" },
};
const PLAIN_SHAPE = { console: false, frame: "round", inputFrame: "round", wording: PLAIN_WORDING } as const;
const LIGHT = () => build("light", "浅色", {
  bg: "#ffffff", rail: "#f7f7f6", text: "#282a2b", muted: "#858580", accent: "#222725", accentAlt: "#377c66",
  emphasis: "#96621a", success: "#2f7d4f", warning: "#9a6b15", danger: "#b13a35", border: "#cfcfca", code: "#377c66",
  syntax: { keyword: "#773b9b", string: "#256c47", number: "#96521a", title: "#275e9a", builtin: "#86622b" },
}, PLAIN_SHAPE);
const DARK = () => build("dark", "深色", {
  bg: "#1d1f20", rail: "#181a1b", text: "#e4e5e2", muted: "#969c98", accent: "#e4e7e3", accentAlt: "#7fc5ad",
  emphasis: "#d9b36c", success: "#7fc58f", warning: "#d9b36c", danger: "#ef9290", border: "#4a4f4d", code: "#7fc5ad",
  syntax: { keyword: "#d6adf4", string: "#a7d7af", number: "#ebbf89", title: "#a5c9f0", builtin: "#dbcd9f" },
}, PLAIN_SHAPE);

function fromShared(theme: UiTheme): DesktopTheme {
  const declared = theme.statusBackground ?? theme.codeBlockBackground;
  const ground = GROUNDS[theme.name] ?? (declared ? { rail: declared, bg: mix(declared, theme.text, 0.04) } : undefined);
  if (!ground) throw new Error(`Theme ${theme.name} has no ground for the desktop`);
  const chrome = theme.chrome ?? {};
  const role = (key: keyof DesktopWording["roles"]) => (chrome.rolePrefixes?.[key] ?? PLAIN_WORDING.roles[key]).trim();
  return build(theme.name, theme.label, {
    ...ground, text: theme.text, muted: theme.muted, accent: theme.accent, accentAlt: theme.accentAlt,
    emphasis: theme.emphasis, success: theme.success, warning: theme.warning, danger: theme.danger,
    border: theme.border, selected: theme.menuSelectedBackground, code: theme.code,
  }, {
    console: true,
    frame: chrome.frameStyle ?? "single",
    inputFrame: chrome.inputFrameStyle ?? chrome.frameStyle ?? "single",
    wording: {
      brand: chrome.brand ?? "ASTRA", tag: chrome.headerTag ?? "", promptLabel: chrome.promptLabel ?? "",
      promptPlaceholder: chrome.promptPlaceholder ?? "", marker: chrome.menuMarker ?? "",
      liveTools: chrome.activeToolsTitle ?? "LIVE TOOLS", archive: chrome.detailTitle ?? "TOOL RESULT", changes: "CHANGES THIS TURN",
      roles: { user: role("user"), assistant: role("assistant"), reasoning: role("reasoning"), tool: role("tool"), error: role("error"), system: role("system") },
    },
  });
}

/** `systemDark` decides what "system" means; every other name ignores it. */
export function desktopTheme(name: string, systemDark = false): DesktopTheme {
  if (name === "light") return LIGHT();
  if (name === "dark") return DARK();
  if (isThemeName(name)) return fromShared(THEMES[name]);
  return { ...(systemDark ? DARK() : LIGHT()), name: "system", label: "跟随系统" };
}

/** Puts a theme on the page. The names of the properties are what cards and stylesheets read. */
export function applyTheme(theme: DesktopTheme, root: HTMLElement = document.documentElement): void {
  root.dataset.theme = theme.name;
  root.dataset.frame = theme.frame;
  root.dataset.inputFrame = theme.inputFrame;
  root.dataset.console = String(theme.console);
  root.style.colorScheme = theme.dark ? "dark" : "light";
  for (const [property, value] of Object.entries(theme.colors)) root.style.setProperty(property, value);
}
