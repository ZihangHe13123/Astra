import { createHash } from "node:crypto";
import { basename } from "node:path";

/**
 * A picture of a page, for a reader that cannot open the panel: the model that wrote it.
 *
 * The page is drawn in a window nobody sees, inside the same isolated frame it has on screen, at
 * the size the enlarged panel has in a window of the default size. A page taller than that is
 * shown whole up to three screens; one that only grows because its window does is shown as its
 * first screen, since stretching it would show a layout nobody sees.
 */
export const SNAPSHOT_WIDTH = 1000;
export const SNAPSHOT_SCREEN = 700;
export const SNAPSHOT_MAX_HEIGHT = SNAPSHOT_SCREEN * 3;
/** Pictures are kept so a conversation can still show the ones it took; only the oldest go. */
export const SNAPSHOTS_KEPT = 40;
export const SNAPSHOT_TIME_LIMIT = 15000;

/** `paths` holds every slide's picture when a deck was drawn whole, for a PPTX. */
export type PageSnapshot = { path: string; width: number; height: number; pageHeight: number; scriptError: string; paths?: string[] };
/** What the backend may ask of a picture: a canvas slide, or a print of a canvas into its exports folder. */
export type SnapshotOptions = { slide?: number; export?: "pdf" | "slides"; target?: string };
/** A slide is drawn at its own size and handed to the model at this width, which reads its text and costs less. */
export const SLIDE_PICTURE_WIDTH = 1280;
export const PRINT_TIME_LIMIT = 90000;

/**
 * How a 1920x1080 slide is drawn in the window it got. Windows keeps a window inside the screen's
 * work area, smaller than a slide on most screens (1920x1040 with a taskbar). There the page is
 * zoomed out to n/120, so the slide covers a whole 16n x 9n of the window, and drawn at 120/n device
 * pixels a point, so the picture is still 1920x1080 and laid out as on a full-size window.
 */
export function slideFit(width: number, height: number): { zoom: number; scale: number; width: number; height: number } {
  const steps = Math.min(120, Math.floor(Math.min(width / 16, height / 9)));
  if (steps <= 0) throw new Error("没有地方画幻灯片：窗口的大小是 0。");
  return { zoom: steps / 120, scale: 120 / steps, width: 16 * steps, height: 9 * steps };
}

/** How much of a page of this height one picture shows. */
export function shownHeight(pageHeight: number): number {
  if (!Number.isFinite(pageHeight)) return SNAPSHOT_SCREEN;
  return Math.min(SNAPSHOT_MAX_HEIGHT, Math.max(SNAPSHOT_SCREEN, Math.ceil(pageHeight)));
}
/** A page laid out against its window gets taller when the window does; its own content does not. */
export function growsWithWindow(before: number, after: number): boolean { return after > before + 1; }

/** One file per page content and theme, named after the page so the picture says what it is of. */
export function snapshotName(path: string, source: string, dark: boolean): string {
  // Letters and digits of the page's own name, so it can neither step out of the folder nor hide in it.
  const stem = basename(path).replace(/\.html?$/i, "").replace(/[^\p{L}\p{N}._-]+/gu, "-").replace(/^[.-]+/, "").slice(0, 60) || "page";
  const digest = createHash("sha256").update(dark ? "dark\0" : "light\0").update(source).digest("hex").slice(0, 10);
  return `${stem}-${digest}.png`;
}

/** The pixel size a PNG states in its header. */
export function pngSize(png: Uint8Array): { width: number; height: number } {
  const view = new DataView(png.buffer, png.byteOffset, png.byteLength);
  return png.byteLength < 24 ? { width: 0, height: 0 } : { width: view.getUint32(16), height: view.getUint32(20) };
}

/** The files to remove so that only the newest `keep` remain. */
export function staleSnapshots(files: { name: string; modified: number }[], keep = SNAPSHOTS_KEPT): string[] {
  return [...files].sort((a, b) => b.modified - a.modified).slice(keep).map(file => file.name);
}
