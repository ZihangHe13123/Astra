import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { CARD_THEME_COLORS, checkedCardTheme } from "../src/card-theme.js";
import { CAPTURE_HOST_POLICY, CAPTURE_HOST_URL, PAGE_FRAME_URL, captureHostResponse, cardFrameResponse, isCardFrameAddress } from "../src/main/card-frame.js";
import { SNAPSHOT_MAX_HEIGHT, SNAPSHOT_SCREEN, SNAPSHOT_WIDTH, growsWithWindow, pngSize, shownHeight, slideFit, snapshotName, staleSnapshots } from "../src/main/page-snapshot-state.js";

test("a picture shows a page whole up to three screens, and never less than one", () => {
  assert.equal(shownHeight(120), SNAPSHOT_SCREEN);
  assert.equal(shownHeight(SNAPSHOT_SCREEN + 0.2), SNAPSHOT_SCREEN + 1);
  assert.equal(shownHeight(1480), 1480);
  assert.equal(shownHeight(90000), SNAPSHOT_MAX_HEIGHT);
  for (const height of [Number.NaN, Number.POSITIVE_INFINITY]) assert.equal(shownHeight(height), SNAPSHOT_SCREEN);
});

test("a page that got taller only because its window did is one laid out against the window", () => {
  assert.equal(growsWithWindow(1600, 2500), true);
  assert.equal(growsWithWindow(1600, 1600), false);
  // Rounding in the page's own report is not growth.
  assert.equal(growsWithWindow(1600, 1601), false);
  assert.equal(growsWithWindow(5000, 5000), false);
});

test("a picture is named after its page, and changes name only when the page or the theme does", () => {
  const name = snapshotName("/work/报告 v2.page.html", "<h1>a</h1>", false);
  assert.match(name, /^报告-v2\.page-[0-9a-f]{10}\.png$/);
  assert.equal(snapshotName("/elsewhere/报告 v2.page.html", "<h1>a</h1>", false), name);
  assert.notEqual(snapshotName("/work/报告 v2.page.html", "<h1>b</h1>", false), name);
  assert.notEqual(snapshotName("/work/报告 v2.page.html", "<h1>a</h1>", true), name);
  // Nothing in a file name can step out of the folder the pictures are kept in.
  for (const path of ["/work/..html", "/work/a b/../../x.htm", "C:\\work\\..\\evil.html", `/work/${"长".repeat(300)}.html`]) {
    const made = snapshotName(path, "", false);
    assert.doesNotMatch(made, /[\\/]|^\.\.?-/);
    assert.ok(made.length <= 80, made);
  }
  assert.match(snapshotName("/work/.html", "", false), /^page-[0-9a-f]{10}\.png$/);
});

test("only the oldest pictures beyond the number kept are removed", () => {
  const files = [5, 1, 4, 2, 3].map(modified => ({ name: `p${modified}.png`, modified }));
  assert.deepEqual(staleSnapshots(files, 3), ["p2.png", "p1.png"]);
  assert.deepEqual(staleSnapshots(files, 5), []);
  assert.deepEqual(staleSnapshots([], 3), []);
});

test("a picture's size is read from the file itself", () => {
  const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=", "base64");
  assert.deepEqual(pngSize(png), { width: 1, height: 1 });
  const header = Buffer.alloc(24); header.writeUInt32BE(1000, 16); header.writeUInt32BE(2100, 20);
  assert.deepEqual(pngSize(header), { width: 1000, height: 2100 });
  assert.deepEqual(pngSize(Buffer.alloc(8)), { width: 0, height: 0 });
});

test("a theme taken from the window is believed only as known names with plain colour values", () => {
  const theme = checkedCardTheme({ dark: true, colors: { "--bg": "#101418", "--accent": "rgb(34, 39, 37)", "--text": "x".repeat(200), "--line": "red;} body{display:none", "--made-up": "#fff", "--muted": 7 } });
  assert.deepEqual(theme, { dark: true, colors: { "--bg": "#101418", "--accent": "rgb(34, 39, 37)" } });
  assert.deepEqual(checkedCardTheme({ dark: "yes" }), { dark: false, colors: {} });
  for (const value of [null, undefined, "dark", 3]) assert.equal(checkedCardTheme(value), undefined);
  assert.ok(Object.keys(theme!.colors).every(name => (CARD_THEME_COLORS as readonly string[]).includes(name)));
});

test("the document that holds a page for its picture loads nothing but the page frame, and is no frame's address", async () => {
  const response = captureHostResponse("/host.html");
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Content-Security-Policy"), CAPTURE_HOST_POLICY);
  assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
  assert.match(CAPTURE_HOST_POLICY, /default-src 'none'/);
  assert.match(CAPTURE_HOST_POLICY, /frame-src astra:\/\/card(;|$)/);
  assert.doesNotMatch(CAPTURE_HOST_POLICY, /https?:|\*|connect-src|img-src|'self'/);
  for (const path of ["/", "/page.html", "/host.html/x", "/frame.html"]) assert.equal(captureHostResponse(path).status, 404, path);
  // It is a host, not a page: no frame may be sent to it, in the app's window or in its own.
  assert.equal(isCardFrameAddress(CAPTURE_HOST_URL), false);
  assert.equal(new URL(CAPTURE_HOST_URL).hostname === new URL(PAGE_FRAME_URL).hostname, false);
  assert.doesNotMatch(readFileSync(new URL("../index.html", import.meta.url), "utf8"), /astra:\/\/capture/);
  const text = await response.text();
  const script = text.slice(text.indexOf("<script>") + 8, text.lastIndexOf("</script>"));
  assert.doesNotThrow(() => new Function(script), "the served script parses");
  // The frame it makes is the one the app's window makes: sandboxed to script alone.
  assert.match(script, /setAttribute\("sandbox", "allow-scripts"\)/);
});

test("the picture the skill and the guides describe is the one the desktop draws", () => {
  const screens = SNAPSHOT_MAX_HEIGHT / SNAPSHOT_SCREEN;
  assert.equal(screens, 3);
  const skill = readFileSync(new URL("../../agent/runtime/interactive_cards.md", import.meta.url), "utf8");
  const look = skill.slice(skill.indexOf("## 写完看一眼"), skill.indexOf("## 例子", skill.indexOf("## 写完看一眼")));
  assert.match(look, /`read_image`/);
  assert.match(look, new RegExp(`${SNAPSHOT_WIDTH}px`));
  assert.match(look, /最多三屏/);
  for (const [guide, said] of [["gui.md", "up to three screens"], ["zh-CN/gui.md", "最多三屏"]] as const) {
    const text = readFileSync(new URL(`../../docs/${guide}`, import.meta.url), "utf8");
    assert.match(text, new RegExp(`${SNAPSHOT_WIDTH} px`), guide);
    assert.equal(text.includes(said), true, guide);
    assert.match(text, /gui\/page-snapshots/, guide);
  }
});

test("the page tells whoever holds it how tall it is and whether it has loaded", async () => {
  const text = await cardFrameResponse("/page.html").text();
  const script = text.slice(text.indexOf("<script>") + 8, text.lastIndexOf("</script>"));
  const prelude = script.slice(script.indexOf("const prelude = function (theme) {") + "const prelude = ".length, script.indexOf("const ground ="));
  const told: any[] = [];
  const listeners: Record<string, (event: any) => void> = {};
  const host = { postMessage: (message: unknown) => told.push(message) };
  const root = { style: { setProperty() {}, colorScheme: "" }, scrollHeight: 1480 };
  const run = new Function("window", "document", "addEventListener", `(${prelude.trim().replace(/;$/, "")})(null)`);
  const document = { documentElement: root, body: { scrollHeight: 1620 }, readyState: "loading" };
  run({ parent: host }, document, (type: string, listener: (event: any) => void) => { listeners[type] = listener; });
  listeners.message({ source: host, data: { astraPage: true, type: "ping", id: 4 } });
  document.readyState = "complete";
  listeners.message({ source: host, data: { astraPage: true, type: "ping", id: 5 } });
  // Anyone else asking is not answered.
  listeners.message({ source: {}, data: { astraPage: true, type: "ping", id: 6 } });
  assert.deepEqual(told, [
    { astraPage: true, type: "pong", id: 4, height: 1620, settled: false },
    { astraPage: true, type: "pong", id: 5, height: 1620, settled: true },
  ]);
});

test("a slide window the system made smaller than a slide is zoomed out to fit and drawn denser, still 1920x1080", () => {
  assert.deepEqual(slideFit(1920, 1080), { zoom: 1, scale: 1, width: 1920, height: 1080 });
  assert.deepEqual(slideFit(2560, 1440), { zoom: 1, scale: 1, width: 1920, height: 1080 });
  // A Windows runner's 1024x768 screen, and a 1080p screen with a taskbar: whole points both ways, 1920x1080 pixels.
  for (const [width, height] of [[1024, 720], [1920, 1040], [1536, 824], [1366, 728]]) {
    const fit = slideFit(width, height);
    assert.ok(fit.width <= width && fit.height <= height, `${width}x${height} holds the slide`);
    assert.ok(Number.isInteger(fit.width) && Number.isInteger(fit.height) && fit.width * 9 === fit.height * 16);
    assert.ok(Math.abs(fit.width * fit.scale - 1920) < 1e-9 && Math.abs(fit.height * fit.scale - 1080) < 1e-9);
    assert.ok(Math.abs(fit.zoom * fit.scale - 1) < 1e-9, "one CSS pixel is one device pixel");
  }
  assert.deepEqual(slideFit(1024, 720), { zoom: 64 / 120, scale: 120 / 64, width: 1024, height: 576 });
  assert.throws(() => slideFit(0, 720), /0/);
});
