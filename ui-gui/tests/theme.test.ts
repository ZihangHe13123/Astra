import assert from "node:assert/strict";
import test from "node:test";
import { THEME_NAMES } from "@astra/ui-core/themes";
import { DESKTOP_THEME_NAMES, applyTheme, contrast, desktopTheme, isDesktopThemeName, mix, readable } from "../src/renderer/theme.js";

const TEXT_ON = ["--bg", "--sidebar", "--panel", "--soft"];
const TEXT_COLOURS = ["--text", "--muted", "--accent", "--accent-2", "--value", "--ok", "--warn", "--error", "--code"];

test("every theme keeps its text readable on every surface the desktop puts it on", () => {
  for (const name of [...DESKTOP_THEME_NAMES, "system"]) for (const systemDark of [false, true]) {
    const theme = desktopTheme(name, systemDark);
    for (const colour of TEXT_COLOURS) for (const ground of TEXT_ON) {
      const ratio = contrast(theme.colors[colour], theme.colors[ground]);
      assert.ok(ratio >= 4.5, `${name}: ${colour} on ${ground} is ${ratio.toFixed(2)}`);
    }
    assert.ok(contrast(theme.colors["--on-accent"], theme.colors["--accent"]) >= 4.5, `${name}: text on the accent`);
    assert.ok(contrast(theme.colors["--frame"], theme.colors["--bg"]) >= 2, `${name}: frame against the page`);
    assert.ok(contrast(theme.colors["--line"], theme.colors["--bg"]) > 1.1, `${name}: lines show`);
  }
});

test("each shared theme reaches the desktop as itself", () => {
  const seen = new Map<string, string>();
  for (const name of THEME_NAMES) {
    const theme = desktopTheme(name);
    assert.equal(theme.name, name);
    assert.equal(theme.console, true);
    const look = ["--bg", "--text", "--accent", "--accent-2"].map(key => theme.colors[key]).join(" ");
    assert.equal(seen.get(look), undefined, `${name} looks like ${seen.get(look)}`);
    seen.set(look, name);
    for (const role of Object.values(theme.wording.roles)) assert.equal(role, role.trim());
  }
  // The daylight theme is light; before, the desktop painted it with the dark palette.
  assert.equal(desktopTheme("moonlit").dark, false);
  assert.equal(desktopTheme("glitchcity").dark, true);
  assert.equal(desktopTheme("glitchcity").wording.promptLabel, "order");
  assert.equal(desktopTheme("glitchcity").inputFrame, "double");
});

test("system follows the system, and an unknown name falls back to it", () => {
  assert.equal(desktopTheme("system", false).dark, false);
  assert.equal(desktopTheme("system", true).dark, true);
  assert.equal(desktopTheme("system", true).name, "system");
  assert.deepEqual(desktopTheme("no-such-theme", true).colors, desktopTheme("dark").colors);
  assert.equal(isDesktopThemeName("glitchcity"), true);
  assert.equal(isDesktopThemeName("no-such-theme"), false);
  assert.equal(desktopTheme("light").console, false);
});

test("a colour is moved only as far as readability needs", () => {
  assert.equal(readable("#ffffff", "#000000", 4.5, "#ffffff"), "#ffffff");
  const lifted = readable("#4b526d", "#181d30", 4.5, "#ffffff");
  assert.ok(contrast(lifted, "#181d30") >= 4.5);
  assert.ok(contrast(mix("#4b526d", lifted, 0.5), "#181d30") < contrast(lifted, "#181d30"));
});

test("applying a theme sets what stylesheets and cards read", () => {
  const properties = new Map<string, string>();
  const root = { dataset: {} as Record<string, string>, style: { colorScheme: "", setProperty: (key: string, value: string) => { properties.set(key, value); } } };
  const theme = desktopTheme("phosphor");
  applyTheme(theme, root as unknown as HTMLElement);
  assert.deepEqual(root.dataset, { theme: "phosphor", frame: "double", inputFrame: "double", console: "true" });
  assert.equal(root.style.colorScheme, "dark");
  for (const name of ["--bg", "--panel", "--soft", "--hover", "--line", "--text", "--muted", "--accent", "--on-accent", "--error"])
    assert.equal(properties.get(name), theme.colors[name]);
});
