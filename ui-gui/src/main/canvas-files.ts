import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readdir, readFile, realpath, rename, rm, stat, writeFile } from "node:fs/promises";
import { basename, dirname, extname, join, relative, sep } from "node:path";
import type { CanvasVersion } from "../file-preview-types.js";
import { ASSET_TYPES, composeDeck, inlineAssets, slideCount, type Asset, type DeckMode } from "./canvas-deck.js";
import { insertComment } from "./canvas-doc.js";

/**
 * A canvas on disk (specs/canvas.md): a folder `<name>.canvas` with `canvas.json`, the slides and
 * `assets/`. The desktop names a canvas by its `canvas.json`, an ordinary file the usual file
 * authorization accepts; everything it reads or writes is inside that folder, where it really is: a
 * link out of it, of a file or of a folder on the way (assets, .versions, exports), is refused.
 */
export const CANVAS_MANIFEST = "canvas.json";
export const SLIDES_FILE = "slides.html";
export const DOC_FILE = "doc.md";
const ENTRIES: Record<string, string> = { slides: SLIDES_FILE, doc: DOC_FILE };
const MAX_VERSIONS = 30;
export const SLIDES_LIMIT = 2 * 1024 * 1024;
const ASSET_LIMIT = 8 * 1024 * 1024;
const TOTAL_ASSET_LIMIT = 40 * 1024 * 1024;
const MAX_ASSETS = 256;

export const isCanvasManifest = (path: string) => basename(path) === CANVAS_MANIFEST && /\.canvas$/i.test(basename(dirname(path)));

/** `inlined` is the slides with their images written in, from which each slide's own document is composed. */
export type CanvasDocument = { title: string; type: string; slides: number; source: string; inlined: string; html: string; missing: string[] };

export async function manifestOf(manifestPath: string): Promise<{ title: string; type: string }> {
  if (!isCanvasManifest(manifestPath)) throw new Error("不是画布：画布是名为 <名字>.canvas 的文件夹里的 canvas.json。");
  let manifest: unknown;
  try { manifest = JSON.parse(await readFile(manifestPath, "utf8")); }
  catch { throw new Error("canvas.json 读不出来，或不是 JSON。"); }
  const { type, title } = (manifest ?? {}) as { type?: unknown; title?: unknown };
  if (type !== "slides" && type !== "doc") throw new Error(`不认识的画布类型：${String(type)}。`);
  return { type, title: typeof title === "string" ? title.slice(0, 200) : "" };
}

/** The canvas folder where it really is: the boundary every path of the canvas is held to. */
const canvasRoot = (manifestPath: string) => realpath(dirname(manifestPath));
const within = (root: string, path: string) => path === root || path.startsWith(root + sep);

/** Where `parts` really are, if that exists and is inside `root`; undefined otherwise. */
async function inside(root: string, ...parts: string[]): Promise<string | undefined> {
  try {
    const path = await realpath(join(root, ...parts));
    return within(root, path) ? path : undefined;
  } catch { return undefined; }
}

/** A file of the canvas to read: refused when it is missing, not a file, or a link out of the canvas. */
async function canvasFile(root: string, ...parts: string[]): Promise<string> {
  const path = await inside(root, ...parts);
  if (!path || !(await stat(path)).isFile()) throw new Error(`画布的 ${parts.join("/")} 不在画布文件夹里，或不是文件。`);
  return path;
}

/** A folder of the canvas to write into, made when missing; refused when a link leads it out of the canvas. */
async function canvasFolder(root: string, ...parts: string[]): Promise<string> {
  await mkdir(join(root, ...parts), { recursive: true });
  const path = await inside(root, ...parts);
  if (!path || !(await stat(path)).isDirectory()) throw new Error(`画布的 ${parts.join("/")} 不在画布文件夹里。`);
  return path;
}

/** An asset's name inside the canvas's own assets folder, or undefined when it, or the folder, would leave it. */
async function assetInside(root: string, name: string): Promise<string | undefined> {
  const assets = await inside(root, "assets");
  if (!assets) return undefined;
  try {
    const path = await realpath(join(assets, name));
    return path.startsWith(assets + sep) && (await stat(path)).isFile() ? path : undefined;
  } catch { return undefined; }
}

/** The canvas as one document for `mode`, with the images it names written in. */
export async function readCanvas(manifestPath: string, mode: DeckMode, slide = 1, version = ""): Promise<CanvasDocument> {
  const { type, title } = await manifestOf(manifestPath);
  const root = await canvasRoot(manifestPath);
  const contentPath = version ? await versionFile(manifestPath, version) : await canvasFile(root, ENTRIES[type]);
  const info = await stat(contentPath);
  if (info.size > SLIDES_LIMIT) throw new Error(`${ENTRIES[type]} 超过 ${SLIDES_LIMIT / 1024 / 1024} MiB，桌面端不渲染。`);
  const source = await readFile(contentPath, "utf8");
  // A document is Markdown, shown by the app's own renderer; only a deck is composed with the template.
  if (type === "doc") return { title, type, slides: 0, source, inlined: "", html: "", missing: [] };
  // The images are read first, so that writing them in needs nothing but what was read.
  const names = new Set([...source.matchAll(/(?:\b(?:src|href)\s*=\s*["']|url\(\s*["']?)\.?\/?assets\/([^"'#?)]+)/gi)].map(match => {
    try { return decodeURIComponent(match[1]); } catch { return match[1]; }
  }));
  const assets = new Map<string, Asset>();
  let remaining = TOTAL_ASSET_LIMIT, considered = 0;
  for (const name of names) {
    if (++considered > MAX_ASSETS || remaining <= 0) break;
    const mime = ASSET_TYPES[extname(name).toLowerCase()];
    const path = mime ? await assetInside(root, name) : undefined;
    if (!mime || !path) continue;
    const handle = await open(path, "r");
    try {
      const info = await handle.stat();
      if (!info.isFile() || info.size > ASSET_LIMIT || info.size > remaining) continue;
      // Reserve before allocating; bounded reads cannot grow with a changing file.
      remaining -= info.size;
      const data = Buffer.alloc(info.size);
      let offset = 0;
      while (offset < data.length) {
        const { bytesRead } = await handle.read(data, offset, data.length - offset, offset);
        if (!bytesRead) break;
        offset += bytesRead;
      }
      const after = await handle.stat();
      if (offset === data.length && after.size === info.size && after.mtimeMs === info.mtimeMs)
        assets.set(name, { mime, data });
    } finally { await handle.close(); }
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
  await add(manifestPath); await add(join(folder, SLIDES_FILE)); await add(join(folder, DOC_FILE));
  const names = await readdir(join(folder, "assets")).catch(() => [] as string[]);
  for (const name of names.sort()) await add(join(folder, "assets", name));
  return hash.digest("hex");
}

/** Where a print of the canvas may be written: its own exports folder, nowhere else. */
export async function exportTarget(manifestPath: string, target: string): Promise<string> {
  const exports = join(dirname(manifestPath), "exports");
  if (dirname(target) !== exports || !/^[^\\/]+\.pdf$/i.test(basename(target))) throw new Error("导出只能写进画布自己的 exports 文件夹。");
  return join(await canvasFolder(await canvasRoot(manifestPath), "exports"), basename(target));
}

/** Where the slides' pictures for a PPTX may be written: the `.slides` folder in its exports, nowhere else. */
export async function slidesTarget(manifestPath: string, target: string): Promise<string> {
  if (target !== join(dirname(manifestPath), "exports", ".slides")) throw new Error("幻灯片图片只能写进画布 exports 里的 .slides 文件夹。");
  return join(await canvasFolder(await canvasRoot(manifestPath), "exports"), ".slides");
}

export async function listVersions(manifestPath: string): Promise<CanvasVersion[]> {
  const root = await canvasRoot(manifestPath);
  const kept = await inside(root, ".versions");
  const versions: CanvasVersion[] = [];
  if (!kept) return versions;
  for (const id of (await readdir(kept).catch(() => [] as string[])).filter(name => /^\d{4}$/.test(name)).sort()) {
    try {
      const meta = JSON.parse(await readFile(await canvasFile(root, ".versions", id, "meta.json"), "utf8"));
      if (meta && typeof meta === "object" && Object.values(ENTRIES).includes(meta.file))
        versions.push({ id, at: Number(meta.at) || 0, source: String(meta.source || ""), sha256: String(meta.sha256 || ""), file: meta.file });
    } catch { /* a version that cannot be read is left out */ }
  }
  return versions;
}

/** The content file of one kept version, by its id. */
async function versionFile(manifestPath: string, id: string): Promise<string> {
  if (!/^\d{4}$/.test(id)) throw new Error("没有这个版本。");
  const version = (await listVersions(manifestPath)).find(item => item.id === id);
  if (!version) throw new Error("没有这个版本。");
  return canvasFile(await canvasRoot(manifestPath), ".versions", id, version.file);
}

/** Keep the canvas's current content as a new version, unless it is the last one already; drop the oldest. */
export async function recordVersion(manifestPath: string, source: string): Promise<string | undefined> {
  const { type } = await manifestOf(manifestPath);
  const root = await canvasRoot(manifestPath);
  const file = ENTRIES[type], data = await readFile(await canvasFile(root, file));
  const sha256 = createHash("sha256").update(data).digest("hex");
  const versions = await listVersions(manifestPath);
  if (versions.at(-1)?.sha256 === sha256) return undefined;
  const kept = await canvasFolder(root, ".versions");
  // Numbered past every folder there, a version that cannot be read included.
  const taken = (await readdir(kept).catch(() => [] as string[])).filter(name => /^\d{4}$/.test(name)).map(Number);
  let number = Math.max(0, ...taken) + 1, id: string, folder: string;
  for (;;) {
    id = String(number++).padStart(4, "0");
    if (id.length > 4) throw new Error("这个画布的版本号用完了。");
    folder = join(kept, id);
    try { await mkdir(folder); break; }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
  }
  if (await inside(root, ".versions", id) !== folder) throw new Error("版本文件夹已改变。");
  await writeFile(join(folder, file), data);
  await writeFile(join(folder, "meta.tmp"), JSON.stringify({ at: Date.now() / 1000, source, sha256, file }));
  await rename(join(folder, "meta.tmp"), join(folder, "meta.json"));
  for (const old of versions.slice(0, Math.max(0, versions.length + 1 - MAX_VERSIONS)))
    await rm(join(kept, old.id), { recursive: true, force: true });
  return id;
}

/**
 * Write the canvas's content as `change` makes it from what is there, as a new version labelled `source`. What
 * was there is kept first, should a write have missed the record; a write that lands meanwhile wins.
 */
async function rewrite(manifestPath: string, source: string, change: (current: Buffer) => Buffer | Promise<Buffer>): Promise<string | undefined> {
  const { type } = await manifestOf(manifestPath);
  const root = await canvasRoot(manifestPath);
  // The file as it is named in the canvas: renaming over it replaces a link there, never what it points to.
  await canvasFile(root, ENTRIES[type]);
  const target = join(root, ENTRIES[type]);
  await recordVersion(manifestPath, "write");
  const current = await readFile(target);
  const temporary = `${target}.${randomUUID()}.tmp`;
  await writeFile(temporary, await change(current));
  if (!current.equals(await readFile(target))) { await rm(temporary, { force: true }); throw new Error("画布刚被改过，请再试一次。"); }
  await rename(temporary, target);
  return recordVersion(manifestPath, source);
}

/** Replace the content with a kept version's, as a new version of its own. */
export async function restoreVersion(manifestPath: string, id: string): Promise<string | undefined> {
  const kept = await readFile(await versionFile(manifestPath, id));
  return rewrite(manifestPath, "restore", () => kept);
}

/** A comment from the person on a passage of a document canvas, written where the doc_* tools read it. */
export async function commentOnDocument(manifestPath: string, passage: string, comment: string): Promise<string | undefined> {
  if ((await manifestOf(manifestPath)).type !== "doc") throw new Error("只有文档画布可以批注。");
  return rewrite(manifestPath, "comment", current => Buffer.from(insertComment(current.toString("utf8"), passage, comment)));
}
