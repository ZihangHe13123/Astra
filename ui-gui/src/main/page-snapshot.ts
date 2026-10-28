import { BrowserWindow } from "electron";
import { mkdir, readdir, readFile, rm, stat, unlink, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import type { CardTheme } from "../card-theme.js";
import { CAPTURE_HOST_URL, PAGE_FRAME_URL, guardCardFrames, printAddress } from "./card-frame.js";
import { SLIDE_HEIGHT, SLIDE_WIDTH, composeDeck } from "./canvas-deck.js";
import { exportTarget, isCanvasManifest, readCanvas, slidesTarget } from "./canvas-files.js";
import { PAGE_PREVIEW_LIMIT } from "./file-preview.js";
import { PRINT_TIME_LIMIT, SLIDE_PICTURE_WIDTH, SNAPSHOT_SCREEN, SNAPSHOT_TIME_LIMIT, SNAPSHOT_WIDTH, growsWithWindow, pngSize, shownHeight,
  snapshotName, staleSnapshots, type PageSnapshot, type SnapshotOptions } from "./page-snapshot-state.js";

/** Draws a page where nobody sees it and saves its picture. What it decides along the way is in page-snapshot-state. */
export class PageSnapshots {
  private last: Promise<unknown> = Promise.resolve();
  constructor(private readonly folder: string, private readonly preload: string,
              private readonly theme: () => Promise<CardTheme | undefined>) {}

  /** One page at a time: each picture has a window of its own, and they are not worth running side by side. A canvas gives one slide, or its print. */
  capture(path: string, options: SnapshotOptions = {}): Promise<PageSnapshot> {
    const run = this.last.then(() => !isCanvasManifest(path) ? this.render(path)
      : options.export === "pdf" ? this.print(path, options.target ?? "")
      : options.export === "slides" ? this.slides(path, options.target ?? "") : this.slide(path, options.slide ?? 1));
    this.last = run.catch(() => undefined);
    return run;
  }

  private async render(path: string): Promise<PageSnapshot> {
    if (!/\.html?$/i.test(path)) throw new Error("只有 HTML 页面可以渲染成图片。");
    const info = await stat(path);
    if (!info.isFile()) throw new Error("页面不是普通文件。");
    if (info.size > PAGE_PREVIEW_LIMIT) throw new Error(`页面超过 ${PAGE_PREVIEW_LIMIT / 1024 / 1024} MiB，桌面端只显示它的源码。`);
    const source = (await readFile(path)).toString("utf8");
    const theme = await this.theme().catch(() => undefined);
    return this.within(this.window(SNAPSHOT_WIDTH, SNAPSHOT_SCREEN, true), SNAPSHOT_TIME_LIMIT, "页面在限定时间内没有画完。",
      window => this.draw(window, path, source, theme));
  }

  /** A window nobody sees, which can open nothing and go nowhere. */
  private window(width: number, height: number, offscreen: boolean): BrowserWindow {
    const window = new BrowserWindow({ show: false, width, height, useContentSize: true, frame: false, skipTaskbar: true, focusable: false,
      // The same preload as the app's window: it strips from the page's frame what a policy cannot, and gives this window no bridge.
      webPreferences: { ...(offscreen ? { offscreen: { deviceScaleFactor: 1 } } : {}), preload: this.preload, contextIsolation: true, sandbox: true,
        nodeIntegration: false, nodeIntegrationInSubFrames: true, backgroundThrottling: false } });
    const contents = window.webContents;
    contents.setAudioMuted(true);
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    contents.on("will-navigate", event => event.preventDefault());
    guardCardFrames(contents);
    return window;
  }

  private async within<T>(window: BrowserWindow, limit: number, message: string, work: (window: BrowserWindow) => Promise<T>): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const late = new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error(message)), limit); });
    try { return await Promise.race([work(window), late]); }
    // Destroying the window is what stops a page whose script never ends.
    finally { clearTimeout(timer); if (!window.isDestroyed()) window.destroy(); }
  }

  /** One slide of a canvas, drawn at its own size in the same isolated frame, and saved at the width the model reads. */
  private async slide(path: string, slide: number): Promise<PageSnapshot> {
    const canvas = await readCanvas(path, "capture", slide);
    if (!canvas.slides) throw new Error("画布里还没有幻灯片。");
    if (slide > canvas.slides) throw new Error(`画布只有 ${canvas.slides} 页。`);
    const theme = await this.theme().catch(() => undefined);
    return this.within(this.window(SLIDE_WIDTH, SLIDE_HEIGHT, true), SNAPSHOT_TIME_LIMIT, "幻灯片在限定时间内没有画完。", async window => {
      const contents = window.webContents;
      const host = <T>(call: string): Promise<T> => contents.executeJavaScript(`astraCapture.${call}`);
      await contents.loadURL(CAPTURE_HOST_URL);
      const opened = { html: canvas.html, theme: theme ?? null, address: PAGE_FRAME_URL, width: SLIDE_WIDTH, height: SLIDE_HEIGHT };
      await host<number>(`open(${JSON.stringify(opened)})`);
      const image = await contents.capturePage({ x: 0, y: 0, width: SLIDE_WIDTH, height: SLIDE_HEIGHT });
      if (image.isEmpty()) throw new Error("没有截到幻灯片的画面。");
      const png = image.resize({ width: SLIDE_PICTURE_WIDTH, quality: "best" }).toPNG();
      const scriptError = String(await host<string>("problem()").catch(() => "") || "").slice(0, 300);
      await mkdir(this.folder, { recursive: true, mode: 0o700 });
      const target = join(this.folder, snapshotName(`${dirname(path)}-${slide}.html`, canvas.html, false));
      await writeFile(target, png, { mode: 0o600 });
      await this.prune(target).catch(error => console.error("Astra page snapshots:", error));
      return { path: target, ...pngSize(png), pageHeight: SLIDE_HEIGHT, scriptError };
    });
  }

  /** Every slide of a deck at its own size, one window drawing them in turn, into the canvas's exports/.slides (for a PPTX). */
  private async slides(path: string, requested: string): Promise<PageSnapshot> {
    const folder = slidesTarget(path, resolve(requested));
    const canvas = await readCanvas(path, "capture", 1);
    if (!canvas.slides) throw new Error("画布里还没有幻灯片。");
    const theme = await this.theme().catch(() => undefined);
    await rm(folder, { recursive: true, force: true });
    await mkdir(folder, { recursive: true });
    const limit = Math.min(10 * 60000, SNAPSHOT_TIME_LIMIT + canvas.slides * 8000);
    return this.within(this.window(SLIDE_WIDTH, SLIDE_HEIGHT, true), limit, "幻灯片在限定时间内没有画完。", async window => {
      const contents = window.webContents;
      const host = <T>(call: string): Promise<T> => contents.executeJavaScript(`astraCapture.${call}`);
      await contents.loadURL(CAPTURE_HOST_URL);
      const paths: string[] = [];
      for (let slide = 1; slide <= canvas.slides; slide++) {
        const html = composeDeck(canvas.inlined, { mode: "capture", slide, title: canvas.title });
        await host<number>(`open(${JSON.stringify({ html, theme: theme ?? null, address: PAGE_FRAME_URL, width: SLIDE_WIDTH, height: SLIDE_HEIGHT })})`);
        const image = await contents.capturePage({ x: 0, y: 0, width: SLIDE_WIDTH, height: SLIDE_HEIGHT });
        if (image.isEmpty()) throw new Error(`没有截到第 ${slide} 页的画面。`);
        let png = image.toPNG();
        if (pngSize(png).width !== SLIDE_WIDTH) png = image.resize({ width: SLIDE_WIDTH, quality: "best" }).toPNG();
        const target = join(folder, `slide-${String(slide).padStart(3, "0")}.png`);
        await writeFile(target, png);
        paths.push(target);
      }
      return { path: folder, width: SLIDE_WIDTH, height: SLIDE_HEIGHT, pageHeight: SLIDE_HEIGHT, scriptError: "", paths };
    });
  }

  /**
   * The whole deck printed to a PDF in the canvas's exports folder. Chromium prints only a window's
   * own document, so the print is that document (card-frame.ts printAddress), under the frame's
   * policy and in an opaque origin of its own, in a window nobody sees.
   */
  private async print(path: string, requested: string): Promise<PageSnapshot> {
    const target = exportTarget(path, resolve(requested));
    const canvas = await readCanvas(path, "print");
    if (!canvas.slides) throw new Error("画布里还没有幻灯片。");
    const { address, forget } = printAddress(canvas.html);
    try {
      return await this.within(this.window(1280, 720, false), PRINT_TIME_LIMIT, "幻灯片在限定时间内没有打印完。", async window => {
        const contents = window.webContents;
        // A script in the deck that tries to leave is stopped (will-navigate), but the attempt aborts this
        // promise; what counts is that the window still holds the print, loaded, with its slides.
        await contents.loadURL(address).catch(() => undefined);
        for (let tries = 0; ; tries++) {
          const state = contents.getURL() === address
            ? await contents.executeJavaScript("[document.readyState, document.querySelectorAll('section.slide').length]").catch(() => ["", 0]) : ["", 0];
          if (state[0] === "complete" && state[1] > 0) break;
          if (tries > 100) throw new Error("幻灯片没有载入，无法打印。");
          await new Promise(resolve => setTimeout(resolve, 100));
        }
        // What the deck draws as it loads (fonts, images, a script) is drawn before it is printed.
        await contents.executeJavaScript("(document.fonts ? document.fonts.ready : Promise.resolve()).then(() => new Promise(r => setTimeout(r, 300)))");
        const pdf = await contents.printToPDF({ printBackground: true, preferCSSPageSize: true, margins: { top: 0, bottom: 0, left: 0, right: 0 } });
        await mkdir(dirname(target), { recursive: true });
        await writeFile(target, pdf);
        return { path: target, width: SLIDE_WIDTH, height: SLIDE_HEIGHT, pageHeight: SLIDE_HEIGHT, scriptError: "" };
      });
    } finally { forget(); }
  }

  private async draw(window: BrowserWindow, path: string, source: string, theme: CardTheme | undefined): Promise<PageSnapshot> {
    const contents = window.webContents;
    const host = <T>(call: string): Promise<T> => contents.executeJavaScript(`astraCapture.${call}`);
    await contents.loadURL(CAPTURE_HOST_URL);
    const opened = { html: source, theme: theme ?? null, address: `${PAGE_FRAME_URL}${theme?.dark ? "?dark" : ""}`, width: SNAPSHOT_WIDTH, height: SNAPSHOT_SCREEN };
    const pageHeight = Math.max(SNAPSHOT_SCREEN, await host<number>(`open(${JSON.stringify(opened)})`));
    let height = shownHeight(pageHeight);
    if (height > SNAPSHOT_SCREEN) {
      window.setContentSize(SNAPSHOT_WIDTH, height);
      if (growsWithWindow(pageHeight, await host<number>(`fit(${height})`))) {
        height = SNAPSHOT_SCREEN;
        window.setContentSize(SNAPSHOT_WIDTH, height);
        await host<number>(`fit(${height})`);
      }
    }
    const image = await contents.capturePage({ x: 0, y: 0, width: SNAPSHOT_WIDTH, height });
    if (image.isEmpty()) throw new Error("没有截到页面的画面。");
    // The file says how many pixels it holds; a display that draws two for one must not double the picture.
    let png = image.toPNG();
    if (pngSize(png).width !== SNAPSHOT_WIDTH) png = image.resize({ width: SNAPSHOT_WIDTH, quality: "best" }).toPNG();
    const scriptError = String(await host<string>("problem()").catch(() => "") || "").slice(0, 300);
    await mkdir(this.folder, { recursive: true, mode: 0o700 });
    const target = join(this.folder, snapshotName(path, source, !!theme?.dark));
    await writeFile(target, png, { mode: 0o600 });
    await this.prune(target).catch(error => console.error("Astra page snapshots:", error));
    return { path: target, ...pngSize(png), pageHeight, scriptError };
  }

  private async prune(written: string): Promise<void> {
    const files: { name: string; modified: number }[] = [];
    for (const name of await readdir(this.folder)) {
      if (!name.endsWith(".png")) continue;
      const info = await stat(join(this.folder, name)).catch(() => undefined);
      if (info?.isFile()) files.push({ name, modified: info.mtimeMs });
    }
    for (const name of staleSnapshots(files)) if (join(this.folder, name) !== written) await unlink(join(this.folder, name)).catch(() => undefined);
  }
}
