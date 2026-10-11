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
      if (data.type === "ping") tell({ type: "pong", id: data.id });
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

export function cardFrameResponse(pathname: string): Response {
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
