import { randomUUID } from "node:crypto";
import type { WebContents } from "electron";

/**
 * The one document every interactive card runs in.
 *
 * A card is HTML and script the model wrote, so it gets nothing: the frame is sandboxed into an
 * origin of its own, the policy below lets it run inline script and style and load nothing, and
 * the window's policy and the guard below keep it from navigating away. It never sees the preload
 * bridge, the conversation or a file. It receives its own source and the theme colours, and reports only
 * its height and its errors.
 *
 * The policy has no say over WebRTC, which opens its own connections. The preload script removes
 * those interfaces from every frame below the window (see src/preload), before any script runs.
 */
export const CARD_FRAME_URL = "astra://card/frame.html";
export const CARD_FRAME_POLICY = [
  "default-src 'none'", "script-src 'unsafe-inline'", "style-src 'unsafe-inline'", "img-src data:", "font-src data:", "media-src data:",
  "form-action 'none'", "base-uri 'none'", "frame-src 'none'",
].join("; ");

const frameDocument = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
:root { color-scheme: light; --bg:#fff; --panel:#fcfcfb; --soft:#f5f5f3; --hover:#eeefec; --line:#eaeae7; --text:#282a2b; --muted:#858580; --accent:#222725; --on-accent:#fff; --error:#b13a35; --radius:10px; }
* { box-sizing: border-box; }
html { background: transparent; }
body { display: flow-root; margin: 0; padding: 2px; color: var(--text); font: 14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; overflow-wrap: anywhere; }
h1, h2, h3, h4 { margin: 0 0 8px; font-weight: 600; line-height: 1.35; } h1 { font-size: 18px; } h2 { font-size: 16px; } h3, h4 { font-size: 14px; }
p { margin: 0 0 8px; } small { color: var(--muted); } a { color: inherit; }
button, input, select, textarea { font: inherit; color: inherit; }
button { border: 1px solid var(--line); background: var(--bg); border-radius: 8px; padding: 6px 12px; cursor: pointer; }
button:hover { background: var(--hover); } button:disabled { opacity: .5; cursor: default; }
input[type=text], input[type=number], select, textarea { border: 1px solid var(--line); background: var(--bg); border-radius: 8px; padding: 6px 9px; }
input[type=range] { accent-color: var(--accent); }
table { border-collapse: collapse; width: 100%; } th, td { border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; } th { color: var(--muted); font-weight: 500; }
svg { max-width: 100%; }
:focus-visible { outline: 2px solid var(--muted); outline-offset: 1px; }
</style><script>if (location.search === "?dark") document.documentElement.style.colorScheme = "dark";</script></head><body><div id="card"></div><script>
(() => {
  const host = window.parent, root = document.getElementById("card");
  const tell = message => host.postMessage({ astraCard: true, ...message }, "*");
  let rendered = false;
  const paint = theme => {
    if (!theme || typeof theme !== "object") return;
    for (const [name, value] of Object.entries(theme.colors || {})) if (/^--[a-z-]+$/.test(name) && typeof value === "string") document.documentElement.style.setProperty(name, value);
    document.documentElement.style.colorScheme = theme.dark ? "dark" : "light";
  };
  const report = () => tell({ type: "height", height: Math.ceil(document.body.getBoundingClientRect().height) });
  addEventListener("message", event => {
    const data = event.data;
    if (event.source !== host || !data || data.astraCard !== true) return;
    paint(data.theme);
    if (data.type !== "render" || rendered || typeof data.html !== "string") return;
    rendered = true;
    const template = document.createElement("template");
    template.innerHTML = data.html;
    // Markup inserted this way does not run its scripts; each one is created again so that it does.
    const scripts = [...template.content.querySelectorAll("script")];
    for (const script of scripts) script.remove();
    root.append(template.content);
    for (const source of scripts) {
      const script = document.createElement("script");
      if (source.type) script.type = source.type;
      script.textContent = source.textContent;
      document.body.append(script);
    }
    report();
  });
  // A followed link would replace the card with a refused page; nothing a card links to can load here.
  addEventListener("click", event => { if (event.target instanceof Element && event.target.closest("a[href], area[href]")) event.preventDefault(); }, true);
  addEventListener("submit", event => event.preventDefault(), true);
  addEventListener("error", event => tell({ type: "error", message: String(event.message || "脚本出错").slice(0, 300) }));
  addEventListener("unhandledrejection", event => tell({ type: "error", message: String(event.reason && event.reason.message || event.reason || "脚本出错").slice(0, 300) }));
  new ResizeObserver(report).observe(document.body);
  tell({ type: "ready" });
})();
</script></body></html>`;

/**
 * The document a whole page runs in: a file the model wrote to be looked at, shown beside the
 * conversation. It is under the same policy as a card and gets as little. Unlike a card it is a
 * document of its own, so this one only waits for the source and then lets it replace everything:
 * the page's doctype, head and body are its own, and no style of ours is laid over it. What the
 * page starts with is written in front of it: the app's colours as `--astra-*` variables, which a
 * page may use or ignore, a readable ground for a page that sets none, and the same refusals a
 * card has (a link that leaves, a form that submits).
 */
export const PAGE_FRAME_URL = "astra://card/page.html";
const pageDocument = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><script>
(() => {
  const host = window.parent;
  if (location.search === "?dark") document.documentElement.style.colorScheme = "dark";
  // Runs first in the page's own document. Listeners added before the page is written do not
  // outlive document.open, so everything the page needs from us is set up again here.
  const prelude = function (theme) {
    const host = window.parent, root = document.documentElement;
    const tell = message => host.postMessage({ astraPage: true, ...message }, "*");
    const paint = theme => {
      if (!theme || typeof theme !== "object") return;
      for (const [name, value] of Object.entries(theme.colors || {})) if (/^--[a-z-]+$/.test(name) && typeof value === "string") root.style.setProperty("--astra-" + name.slice(2), value);
      root.style.colorScheme = theme.dark ? "dark" : "light";
    };
    paint(theme);
    addEventListener("message", event => {
      const data = event.data;
      if (event.source !== host || !data || data.astraPage !== true) return;
      if (data.type === "theme") paint(data.theme);
      // The host asks after every load whether this is still the page; a document that replaced it cannot answer.
      // The answer says how tall the page is and whether it has finished loading, for a host that takes its picture.
      if (data.type === "ping") tell({ type: "pong", id: data.id, height: Math.max(root.scrollHeight, document.body ? document.body.scrollHeight : 0), settled: document.readyState === "complete" });
    });
    // A link inside the page scrolls to its place. Any other would replace the page with a refused one.
    addEventListener("click", event => {
      const link = event.target instanceof Element ? event.target.closest("a[href], area[href]") : null;
      if (link && !(link.getAttribute("href") || "").startsWith("#")) event.preventDefault();
    }, true);
    addEventListener("submit", event => event.preventDefault(), true);
    addEventListener("error", event => tell({ type: "error", message: String(event.message || "脚本出错").slice(0, 300) }));
    addEventListener("unhandledrejection", event => tell({ type: "error", message: String(event.reason && event.reason.message || event.reason || "脚本出错").slice(0, 300) }));
  };
  const ground = "<style>html{background:var(--astra-bg,#fff);color:var(--astra-text,#282a2b);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}</style>";
  addEventListener("message", function receive(event) {
    const data = event.data;
    if (event.source !== host || !data || data.astraPage !== true) return;
    if (data.type === "ping") host.postMessage({ astraPage: true, type: "pong", id: data.id }, "*");
    if (data.type !== "render" || typeof data.html !== "string") return;
    removeEventListener("message", receive);
    const theme = JSON.stringify(data.theme || null).replace(/</g, "\\\\u003c");
    // The doctype is written once, ahead of the script that must run first; the page's own would come too late to count.
    const page = data.html.replace(/^\\uFEFF?\\s*<!doctype[^>]*>/i, "");
    document.open();
    document.write("<!doctype html><script>(" + prelude + ")(" + theme + ")</" + "script>" + ground + page);
    document.close();
  });
  host.postMessage({ astraPage: true, type: "ready" }, "*");
})();
</script></head><body></body></html>`;

/**
 * The document that holds a page while its picture is taken, in a window nobody sees. It is the
 * page's host there as the app's window is its host on screen: the page runs in the same frame,
 * under the same policy, and this document only hands it its source, waits until it has settled,
 * and says how tall it is. It is ours alone; nothing but the page frame loads in it.
 */
export const CAPTURE_HOST_URL = "astra://capture/host.html";
export const CAPTURE_HOST_POLICY = [
  "default-src 'none'", "script-src 'unsafe-inline'", "style-src 'unsafe-inline'", "frame-src astra://card",
  "form-action 'none'", "base-uri 'none'",
].join("; ");
const captureDocument = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>html, body { margin: 0; overflow: hidden; background: #fff; } iframe { display: block; border: 0; }</style><script>
(() => {
  const loadTimeout = 5000, answerTimeout = 3000, settleTime = 400, drawTimeout = 8000;
  let frame, asks = 0, problem = "", ready;
  const waiting = new Map();
  let becameReady = new Promise(resolve => { ready = resolve; });
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  // Two frames, so that what was laid out has also been drawn; a window that draws nothing must not hold the picture up.
  const painted = () => Promise.race([new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))), pause(300)]);
  addEventListener("message", event => {
    const data = event.data;
    if (!frame || event.source !== frame.contentWindow || !data || data.astraPage !== true) return;
    if (data.type === "ready") ready();
    else if (data.type === "pong" && waiting.has(data.id)) { const answer = waiting.get(data.id); waiting.delete(data.id); answer(data); }
    else if (data.type === "error" && typeof data.message === "string" && !problem) problem = data.message.slice(0, 300);
  });
  // The page answers only while it is still the page and its script is not stuck.
  const ask = () => new Promise((resolve, reject) => {
    const id = ++asks;
    const timer = setTimeout(() => { waiting.delete(id); reject(new Error("页面没有应答：它离开了自己的框，或脚本一直占着不放。")); }, answerTimeout);
    waiting.set(id, data => { clearTimeout(timer); resolve(data); });
    frame.contentWindow.postMessage({ astraPage: true, type: "ping", id }, "*");
  });
  // The page says how tall it is; the host believes it only as a number within bounds.
  const tall = data => { const value = Number(data.height); return Number.isFinite(value) ? Math.min(100000, Math.max(0, Math.ceil(value))) : 0; };
  window.astraCapture = {
    async open({ html, theme, address, width, height }) {
      // One page at a time: drawing the next one (a deck's next slide) replaces the frame before it.
      if (frame) { frame.remove(); becameReady = new Promise(resolve => { ready = resolve; }); problem = ""; }
      frame = document.createElement("iframe");
      frame.setAttribute("sandbox", "allow-scripts"); frame.referrerPolicy = "no-referrer";
      frame.style.width = width + "px"; frame.style.height = height + "px";
      frame.src = address; document.body.append(frame);
      await Promise.race([becameReady, pause(loadTimeout).then(() => { throw new Error("页面没有载入。"); })]);
      frame.contentWindow.postMessage({ astraPage: true, type: "render", html, theme }, "*");
      // Loaded, and the same height twice running: what the page lays out as it loads has been laid out.
      const started = Date.now(); let last = 0, settledAt = 0;
      for (;;) {
        await pause(100);
        const data = await ask(), now = tall(data);
        if (data.settled === true && now > 0) {
          settledAt = settledAt || Date.now();
          if (now === last && Date.now() - settledAt >= settleTime) break;
          last = now;
        }
        if (Date.now() - started > drawTimeout) { if (last > 0) break; throw new Error("页面在限定时间内没有画完。"); }
      }
      await painted();
      return last;
    },
    async fit(height) {
      frame.style.height = height + "px";
      await painted(); await pause(120);
      const now = tall(await ask());
      await painted();
      return now;
    },
    problem: () => problem,
  };
})();
</script></head><body></body></html>`;

export function captureHostResponse(pathname: string): Response {
  if (pathname !== "/host.html") return new Response("Not found", { status: 404 });
  return new Response(captureDocument, { headers: {
    "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": CAPTURE_HOST_POLICY,
    "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
  } });
}

/**
 * A print of a canvas (canvas-deck.ts). Chromium prints only a window's own document, so for this
 * the deck cannot stay in a frame: it is served as the document of a window nobody sees, under the
 * frame's policy plus `sandbox allow-scripts`, which gives it the opaque origin a frame has; the
 * preload strips from it what it strips from a frame. Each is served only while its print runs.
 */
export const PRINT_POLICY = `${CARD_FRAME_POLICY}; sandbox allow-scripts`;
const prints = new Map<string, string>();
export function printAddress(html: string): { address: string; forget: () => void } {
  const token = randomUUID();
  prints.set(token, html);
  return { address: `astra://card/print/${token}.html`, forget: () => { prints.delete(token); } };
}

export function cardFrameResponse(pathname: string): Response {
  const print = /^\/print\/([0-9a-f-]{36})\.html$/.exec(pathname);
  if (print) {
    const html = prints.get(print[1]);
    if (html === undefined) return new Response("Not found", { status: 404 });
    return new Response(html, { headers: {
      "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": PRINT_POLICY,
      "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
    } });
  }
  const body = pathname === "/frame.html" ? frameDocument : pathname === "/page.html" ? pageDocument : undefined;
  if (body === undefined) return new Response("Not found", { status: 404 });
  return new Response(body, { headers: {
    "Content-Type": "text/html; charset=utf-8", "Content-Security-Policy": CARD_FRAME_POLICY,
    "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
  } });
}

/** The card document or the page document, with or without the hint that tells it to start in the dark theme. */
export function isCardFrameAddress(url: string): boolean {
  return [CARD_FRAME_URL, PAGE_FRAME_URL].some(address => url === address || url === `${address}?dark`);
}

/**
 * A second barrier behind two policies. The card's own policy refuses everything it could load,
 * and the window's policy (frame-src in index.html) refuses a card that navigates its frame away,
 * which the card's own policy has no say over. Should the window's policy be loosened by mistake,
 * no frame in this window navigates to anything but the card document or the page document.
 *
 * Requests are deliberately not filtered here as well: a request filter on the window's session
 * stalled document previews, and every request a card can make is already refused by its policy.
 */
export function guardCardFrames(contents: WebContents): void {
  contents.on("will-frame-navigate", event => {
    if (!event.isMainFrame && !isCardFrameAddress(event.url)) event.preventDefault();
  });
}
