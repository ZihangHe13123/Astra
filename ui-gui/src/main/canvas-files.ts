import { createHash } from "node:crypto";
import { readdir, readFile, realpath, stat } from "node:fs/promises";
import { basename, dirname, extname, join, relative, sep } from "node:path";
import { ASSET_TYPES, composeDeck, inlineAssets, slideCount, type Asset, type DeckMode } from "./canvas-deck.js";

/**
 * A canvas on disk (specs/canvas.md): a folder `<name>.canvas` with `canvas.json`, the slides and
 * `assets/`. The desktop names a canvas by its `canvas.json`, an ordinary file the usual file
 * authorization accepts; everything it reads is inside that folder.
 */
export const CANVAS_MANIFEST = "canvas.json";
export const SLIDES_FILE = "slides.html";
export const SLIDES_LIMIT = 2 * 1024 * 1024;
const ASSET_LIMIT = 8 * 1024 * 1024;

export const isCanvasManifest = (path: string) => basename(path) === CANVAS_MANIFEST && /\.canvas$/i.test(basename(dirname(path)));

/** `inlined` is the slides with their images written in, from which each slide's own document is composed. */
export type CanvasDocument = { title: string; type: string; slides: number; source: string; inlined: string; html: string; missing: string[] };

async function manifestOf(manifestPath: string): Promise<{ title: string; type: string }> {
  if (!isCanvasManifest(manifestPath)) throw new Error("不是画布：画布是名为 <名字>.canvas 的文件夹里的 canvas.json。");
  let manifest: unknown;
  try { manifest = JSON.parse(await readFile(manifestPath, "utf8")); }
  catch { throw new Error("canvas.json 读不出来，或不是 JSON。"); }
  const { type, title } = (manifest ?? {}) as { type?: unknown; title?: unknown };
  if (type !== "slides") throw new Error(`不认识的画布类型：${String(type)}。`);
  return { type, title: typeof title === "string" ? title.slice(0, 200) : "" };
}

/** An asset's name inside the folder, or undefined when it would leave it. */
async function assetInside(folder: string, name: string): Promise<string | undefined> {
  try {
    const root = await realpath(join(folder, "assets"));
    const path = await realpath(join(root, name));
    return path.startsWith(root + sep) && (await stat(path)).isFile() ? path : undefined;
  } catch { return undefined; }
}

/** The canvas as one document for `mode`, with the images it names written in. */
export async function readCanvas(manifestPath: string, mode: DeckMode, slide = 1): Promise<CanvasDocument> {
  const { type, title } = await manifestOf(manifestPath);
  const folder = dirname(manifestPath);
  const slidesPath = join(folder, SLIDES_FILE);
  const info = await stat(slidesPath).catch(() => undefined);
  if (!info?.isFile()) throw new Error(`画布缺少 ${SLIDES_FILE}。`);
  if (info.size > SLIDES_LIMIT) throw new Error(`${SLIDES_FILE} 超过 ${SLIDES_LIMIT / 1024 / 1024} MiB，桌面端不渲染。`);
  const source = await readFile(slidesPath, "utf8");
  // The images are read first, so that writing them in needs nothing but what was read.
  const names = new Set([...source.matchAll(/(?:\b(?:src|href)\s*=\s*["']|url\(\s*["']?)\.?\/?assets\/([^"'#?)]+)/gi)].map(match => {
    try { return decodeURIComponent(match[1]); } catch { return match[1]; }
  }));
  const assets = new Map<string, Asset>();
  for (const name of names) {
    const mime = ASSET_TYPES[extname(name).toLowerCase()];
    const path = mime ? await assetInside(folder, name) : undefined;
    if (!mime || !path || (await stat(path)).size > ASSET_LIMIT) continue;
    assets.set(name, { mime, data: await readFile(path) });
  }
  const { html, missing } = inlineAssets(source, name => assets.get(name));
  return { title, type, slides: slideCount(source), source, inlined: html, html: composeDeck(html, { mode, slide, title }), missing };
}

/** One version for everything the canvas is drawn from: its manifest, its slides and its images. */
export async function canvasVersion(manifestPath: string): Promise<string> {
  const folder = dirname(manifestPath);
  const hash = createHash("sha256");
  const add = async (path: string) => {
    const info = await stat(path).catch(() => undefined);
    hash.update(JSON.stringify([relative(folder, path), info?.size ?? -1, info?.mtimeMs ?? 0, info?.ino ?? 0]));
  };
  await add(manifestPath); await add(join(folder, SLIDES_FILE));
  const names = await readdir(join(folder, "assets")).catch(() => [] as string[]);
  for (const name of names.sort()) await add(join(folder, "assets", name));
  return hash.digest("hex");
}

/** Where a print of the canvas may be written: its own exports folder, nowhere else. */
export function exportTarget(manifestPath: string, target: string): string {
  const exports = join(dirname(manifestPath), "exports");
  if (dirname(target) !== exports || !/^[^\\/]+\.pdf$/i.test(basename(target))) throw new Error("导出只能写进画布自己的 exports 文件夹。");
  return target;
}

/** Where the slides' pictures for a PPTX may be written: the `.slides` folder in its exports, nowhere else. */
export function slidesTarget(manifestPath: string, target: string): string {
  if (target !== join(dirname(manifestPath), "exports", ".slides")) throw new Error("幻灯片图片只能写进画布 exports 里的 .slides 文件夹。");
  return target;
}
