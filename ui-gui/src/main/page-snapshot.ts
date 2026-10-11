import { BrowserWindow } from "electron";
import { mkdir, readdir, readFile, stat, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import type { CardTheme } from "../card-theme.js";
import { CAPTURE_HOST_URL, PAGE_FRAME_URL, guardCardFrames } from "./card-frame.js";
import { PAGE_PREVIEW_LIMIT } from "./file-preview.js";
import { SNAPSHOT_SCREEN, SNAPSHOT_TIME_LIMIT, SNAPSHOT_WIDTH, growsWithWindow, pngSize, shownHeight, snapshotName, staleSnapshots,
  type PageSnapshot } from "./page-snapshot-state.js";

/** Draws a page where nobody sees it and saves its picture. What it decides along the way is in page-snapshot-state. */
export class PageSnapshots {
  private last: Promise<unknown> = Promise.resolve();
  constructor(private readonly folder: string, private readonly preload: string,
              private readonly theme: () => Promise<CardTheme | undefined>) {}

  /** One page at a time: each picture has a window of its own, and they are not worth running side by side. */
  capture(path: string): Promise<PageSnapshot> {
    const run = this.last.then(() => this.render(path));
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
    const window = new BrowserWindow({ show: false, width: SNAPSHOT_WIDTH, height: SNAPSHOT_SCREEN, useContentSize: true, frame: false,
      skipTaskbar: true, focusable: false,
      // The same preload as the app's window: it strips from the page's frame what a policy cannot, and gives this window no bridge.
      webPreferences: { offscreen: { deviceScaleFactor: 1 }, preload: this.preload, contextIsolation: true, sandbox: true,
        nodeIntegration: false, nodeIntegrationInSubFrames: true, backgroundThrottling: false } });
    const contents = window.webContents;
    contents.setAudioMuted(true);
    contents.setWindowOpenHandler(() => ({ action: "deny" }));
    contents.on("will-navigate", event => event.preventDefault());
    guardCardFrames(contents);
    let timer: ReturnType<typeof setTimeout> | undefined;
    const limit = new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("页面在限定时间内没有画完。")), SNAPSHOT_TIME_LIMIT); });
    try { return await Promise.race([this.draw(window, path, source, theme), limit]); }
    // Destroying the window is what stops a page whose script never ends.
    finally { clearTimeout(timer); if (!window.isDestroyed()) window.destroy(); }
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
