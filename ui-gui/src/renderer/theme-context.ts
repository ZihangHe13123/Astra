/** The theme in force, for components that word or draw themselves by it. */
import { useSyncExternalStore } from "react";
import { desktopTheme, type DesktopTheme } from "./theme.js";

let current: DesktopTheme = desktopTheme("system");
const listeners = new Set<() => void>();

export function setCurrentTheme(theme: DesktopTheme): void {
  current = theme;
  for (const listener of listeners) listener();
}
export function useTheme(): DesktopTheme {
  return useSyncExternalStore(listener => { listeners.add(listener); return () => { listeners.delete(listener); }; }, () => current, () => current);
}
