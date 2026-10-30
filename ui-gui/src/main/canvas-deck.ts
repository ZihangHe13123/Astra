/**
 * A slide canvas as one document (specs/canvas.md).
 *
 * The model writes only the slides (`slides.html`: one `<section class="slide">` each); the look,
 * the list, presenting and printing are this template's own code. The composed document runs in the
 * same isolated page frame as a page (card-frame.ts), so the model's markup gets no more than a
 * page does. Nothing here touches the file system: canvas-files.ts reads the canvas and its images.
 */
export type DeckMode = "view" | "capture" | "print";
export const SLIDE_WIDTH = 1920;
export const SLIDE_HEIGHT = 1080;

const font = `-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif`;

export const DECK_STYLE = `
:root { --deck-bg:#ffffff; --deck-text:#1d2329; --deck-muted:#6b7280; --deck-accent:#2563eb; --deck-accent-text:#ffffff;
  --deck-font:${font}; --deck-heading-font:var(--deck-font); }
* { box-sizing: border-box; }
html, body { margin: 0; }
body { background: var(--astra-soft, #f1f1ef); }
.slide { position: relative; display: flex; flex-direction: column; width: ${SLIDE_WIDTH}px; height: ${SLIDE_HEIGHT}px; overflow: hidden;
  padding: 110px 140px; background: var(--deck-bg); color: var(--deck-text); font: 400 40px/1.45 var(--deck-font); text-align: left; }
.slide h1, .slide h2, .slide h3 { margin: 0 0 40px; font-family: var(--deck-heading-font); font-weight: 700; line-height: 1.15; letter-spacing: -.01em; }
.slide h1 { font-size: 76px; } .slide h2 { font-size: 56px; } .slide h3 { font-size: 44px; }
.slide p { margin: 0 0 28px; } .slide ul, .slide ol { margin: 0 0 28px; padding-left: 1.2em; } .slide li { margin: 0 0 18px; }
.slide li::marker { color: var(--deck-accent); }
.slide .lead { font-size: 52px; line-height: 1.35; } .slide .muted { color: var(--deck-muted); } .slide .small { font-size: 30px; }
.slide .accent { color: var(--deck-accent); } .slide strong { font-weight: 700; }
.slide table { width: 100%; border-collapse: collapse; font-size: 34px; }
.slide th, .slide td { padding: 18px 24px; border-bottom: 2px solid color-mix(in srgb, var(--deck-text) 14%, transparent); text-align: left; }
.slide th { color: var(--deck-muted); font-weight: 600; }
.slide figure { display: flex; flex: 1; min-height: 0; flex-direction: column; align-items: center; justify-content: center; margin: 0; }
.slide img { max-width: 100%; max-height: 100%; object-fit: contain; }
.slide figcaption, .slide .caption { margin-top: 16px; font-size: 28px; color: var(--deck-muted); }
.slide svg { max-width: 100%; }
.slide .notes { display: none; }
.slide.title { justify-content: center; }
.slide.title h1 { max-width: 85%; margin-bottom: 36px; font-size: 104px; }
.slide.title p { font-size: 44px; color: var(--deck-muted); }
.slide.section { justify-content: center; background: var(--deck-accent); color: var(--deck-accent-text); }
.slide.section h1 { font-size: 96px; } .slide.section .muted { color: inherit; opacity: .8; }
.slide .columns { display: grid; flex: 1; min-height: 0; grid-auto-flow: column; grid-auto-columns: 1fr; gap: 80px; }
.slide.image { padding: 0; justify-content: flex-end; }
.slide.image > img { position: absolute; inset: 0; width: 100%; height: 100%; max-width: none; max-height: none; object-fit: cover; }
.slide.image h1 { position: relative; margin: 0 140px 24px; color: #fff; text-shadow: 0 4px 24px rgba(0,0,0,.45); }
.slide.image .caption { position: relative; margin: 0 140px 90px; color: #f3f4f6; }
.slide.quote { justify-content: center; }
.slide blockquote { margin: 0; padding-left: 56px; border-left: 12px solid var(--deck-accent); font-family: var(--deck-heading-font); font-size: 64px; line-height: 1.3; }
.slide .by { margin-top: 40px; color: var(--deck-muted); }
.slide .stats { display: grid; grid-auto-flow: column; grid-auto-columns: 1fr; gap: 60px; margin: 20px 0 40px; }
.slide .stats b { display: block; font-size: 110px; font-weight: 800; line-height: 1; color: var(--deck-accent); }
.slide .stats span { display: block; margin-top: 18px; font-size: 32px; color: var(--deck-muted); }

/* The list beside the conversation: every slide, as wide as the panel. */
body[data-mode=view] { padding: 14px 14px 28px; font: 12px/1.4 ${font}; }
.astra-deck-empty { color: var(--astra-muted, #858580); }
.astra-slide-number { margin: 14px 0 6px 2px; color: var(--astra-muted, #858580); }
.astra-slide-number:first-child { margin-top: 0; }
.astra-slide-frame { position: relative; width: 100%; aspect-ratio: 16 / 9; overflow: hidden; border-radius: 6px; cursor: zoom-in;
  background: var(--deck-bg); box-shadow: 0 1px 3px rgba(0,0,0,.12), 0 6px 18px rgba(0,0,0,.08); }
.astra-slide-frame > .slide { position: absolute; top: 0; left: 0; transform-origin: 0 0; transform: scale(var(--astra-scale, .2)); }
/* Presenting: one slide, as large as the frame allows. */
body.astra-present { padding: 0; overflow: hidden; background: #000; }
body.astra-present .astra-slide-number, body.astra-present .astra-slide-frame { display: none; }
body.astra-present .astra-slide-frame.astra-current { display: block; position: fixed; left: 50%; top: 50%; width: var(--astra-w); height: var(--astra-h);
  aspect-ratio: auto; border-radius: 0; box-shadow: none; cursor: default; transform: translate(-50%, -50%); }
.astra-present-hint { display: none; }
body.astra-present .astra-present-hint { display: block; position: fixed; right: 12px; bottom: 8px; color: rgba(255,255,255,.55); }
/* A picture or a print holds the slides and nothing else: whatever a script adds around them takes no room. */
body[data-mode=capture] > :not(.astra-deck), body[data-mode=capture] .astra-deck > :not(section.slide),
body[data-mode=print] > :not(.astra-deck), body[data-mode=print] .astra-deck > :not(section.slide) { display: none !important; }
/* A picture of one slide at its own size. */
body[data-mode=capture] { width: ${SLIDE_WIDTH}px; height: ${SLIDE_HEIGHT}px; overflow: hidden; background: var(--deck-bg); }
body[data-mode=capture] .slide { display: none; }
body[data-mode=capture] .slide.astra-current { display: flex; }
/* A print: one slide per page. */
@page { size: ${SLIDE_WIDTH}px ${SLIDE_HEIGHT}px; margin: 0; }
body[data-mode=print] { background: none; }
body[data-mode=print] .slide { break-after: page; }
body[data-mode=print] .slide:last-of-type { break-after: auto; }
`;

export const DECK_SCRIPT = `(() => {
  const body = document.body, mode = body.dataset.mode, deck = document.querySelector(".astra-deck");
  let slides = [...deck.querySelectorAll(":scope > section.slide")];
  if (!slides.length) slides = [...deck.querySelectorAll("section.slide")];
  if (mode === "capture") { (slides[Number(body.dataset.slide) - 1] || slides[0])?.classList.add("astra-current"); return; }
  if (mode !== "view") return;
  if (!slides.length) { const note = document.createElement("p"); note.className = "astra-deck-empty"; note.textContent = "还没有幻灯片。"; deck.prepend(note); return; }
  const frames = slides.map((slide, index) => {
    const label = document.createElement("p"); label.className = "astra-slide-number"; label.textContent = (index + 1) + " / " + slides.length;
    const frame = document.createElement("div"); frame.className = "astra-slide-frame";
    slide.before(label); slide.before(frame); frame.append(slide);
    return frame;
  });
  const hint = document.createElement("p"); hint.className = "astra-present-hint"; hint.textContent = "← → 翻页 · Esc 退出"; body.append(hint);
  let current = -1;
  const fit = () => {
    if (current >= 0) {
      const scale = Math.min(innerWidth / ${SLIDE_WIDTH}, innerHeight / ${SLIDE_HEIGHT});
      body.style.setProperty("--astra-w", ${SLIDE_WIDTH} * scale + "px"); body.style.setProperty("--astra-h", ${SLIDE_HEIGHT} * scale + "px");
      frames[current].style.setProperty("--astra-scale", String(scale));
    } else for (const frame of frames) frame.style.setProperty("--astra-scale", String(frame.clientWidth / ${SLIDE_WIDTH}));
  };
  const show = index => {
    current = Math.max(0, Math.min(frames.length - 1, index));
    frames.forEach((frame, at) => frame.classList.toggle("astra-current", at === current));
    body.classList.add("astra-present"); fit();
  };
  const leave = () => {
    const was = frames[current]; current = -1;
    body.classList.remove("astra-present"); frames.forEach(frame => frame.classList.remove("astra-current")); fit();
    was && was.scrollIntoView({ block: "center" });
  };
  frames.forEach((frame, index) => frame.addEventListener("click", () => current < 0 ? show(index) : show(current + 1)));
  addEventListener("keydown", event => {
    if (current < 0) return;
    if (["ArrowRight", "ArrowDown", "PageDown", " ", "Enter"].includes(event.key)) show(current + 1);
    else if (["ArrowLeft", "ArrowUp", "PageUp", "Backspace"].includes(event.key)) show(current - 1);
    else if (event.key === "Home") show(0); else if (event.key === "End") show(frames.length - 1);
    else if (event.key === "Escape") leave(); else return;
    event.preventDefault();
  });
  new ResizeObserver(fit).observe(document.documentElement); fit();
})();`;

const escapeText = (text: string) => text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** The whole document for one way of showing the deck: the list, one slide's picture, or the print. */
export function composeDeck(slides: string, { mode, slide = 1, title = "" }: { mode: DeckMode; slide?: number; title?: string }): string {
  // The slides are written into the deck as they are; a closing tag of ours inside them only ends the deck early.
  const number = Number.isSafeInteger(slide) && slide > 0 ? slide : 1;
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>${escapeText(title)}</title><style>${DECK_STYLE}</style></head>`
    + `<body data-mode="${mode}" data-slide="${number}"><main class="astra-deck">${slides}</main><script>${DECK_SCRIPT}</script></body></html>`;
}

/** How many slides the source holds, as the template counts them. */
export function slideCount(slides: string): number {
  return [...slides.matchAll(/<section\b[^>]*\bclass\s*=\s*(['"])(?:[^'"]*\s)?slide(?:\s[^'"]*)?\1/gi)].length;
}

export type Asset = { mime: string; data: Uint8Array };
export const ASSET_TYPES: Record<string, string> = {
  ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
};

/**
 * Images the slides name under `assets/`, written in as data, which is all the frame may load.
 * `read` returns an asset by its name inside the folder, or undefined; a name that climbs out, an
 * unknown type or a file over the limits stays as written and shows as a broken image.
 */
export function inlineAssets(slides: string, read: (name: string) => Asset | undefined, limits = { each: 8 * 1024 * 1024, total: 40 * 1024 * 1024 }):
  { html: string; missing: string[] } {
  const missing = new Set<string>(); const cache = new Map<string, string | undefined>(); let total = 0;
  const dataFor = (raw: string): string | undefined => {
    let name = raw;
    try { name = decodeURIComponent(raw); } catch { /* keep the written name */ }
    if (cache.has(name)) return cache.get(name);
    let url: string | undefined;
    const parts = name.split(/[\\/]+/);
    if (name && !parts.some(part => part === ".." || part === "") && ASSET_TYPES[`.${name.split(".").pop()!.toLowerCase()}`]) {
      const asset = read(name);
      if (asset && asset.data.byteLength <= limits.each && total + asset.data.byteLength <= limits.total) {
        total += asset.data.byteLength;
        url = `data:${asset.mime};base64,${Buffer.from(asset.data).toString("base64")}`;
      }
    }
    if (!url) missing.add(name);
    cache.set(name, url);
    return url;
  };
  const html = slides
    .replace(/\b(src|href)(\s*=\s*)(["'])\.?\/?assets\/([^"'#?]+)\3/gi, (whole, attribute, equals, quote, name) => {
      const url = dataFor(name); return url ? `${attribute}${equals}${quote}${url}${quote}` : whole;
    })
    .replace(/url\(\s*(["']?)\.?\/?assets\/([^"')]+)\1\s*\)/gi, (whole, quote, name) => {
      const url = dataFor(name); return url ? `url(${quote}${url}${quote})` : whole;
    });
  return { html, missing: [...missing] };
}
