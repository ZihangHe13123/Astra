import { DEFAULT_THEME, isThemeName, type ThemeName } from "@astra/ui-core/themes";
import { readTuiSettings, writeTuiSettings } from "./tui-settings.js";

export * from "@astra/ui-core/themes";

export function loadThemeName(): ThemeName {
  const value = readTuiSettings().theme;
  return typeof value === "string" && isThemeName(value) ? value : DEFAULT_THEME;
}

export function saveThemeName(theme: ThemeName): void {
  writeTuiSettings({ theme });
}
