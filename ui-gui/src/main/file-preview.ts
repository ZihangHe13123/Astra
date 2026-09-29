import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { open, stat } from 'node:fs/promises';
import { extname } from 'node:path';
import type { DocumentCheck, FilePreview } from '../file-preview-types.js';
import { inspectOffice, spreadsheetPreview } from './office-inspection.js';
import { convertOffice, type OfficeConversion } from './office-converter.js';

export const TEXT_PREVIEW_LIMIT = 256 * 1024;
export const DOCUMENT_PREVIEW_LIMIT = 32 * 1024 * 1024;
const IMAGE_PREVIEW_LIMIT = 10 * 1024 * 1024;
const imageTypes: Record<string, string> = { '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.gif': 'image/gif' };
const cancelled = () => new Error('文件预览已取消。');
const changed = () => new Error('源文件已变化，请重新预览。');
function version(info: {dev: number; ino: number; size: number; mtimeMs: number; ctimeMs: number}) { return createHash('sha256').update(JSON.stringify([info.dev, info.ino, info.size, info.mtimeMs, info.ctimeMs])).digest('hex'); }
/** Call only after ordinary session file authorization. */
export async function sourceVersion(path: string) { return version(await stat(path)); }
function checkSignal(signal?: AbortSignal) { if (signal?.aborted) throw cancelled(); }
function assertPDF(bytes: Uint8Array) { if (!Buffer.from(bytes).subarray(0, 1024).includes(Buffer.from('%PDF-')) || !Buffer.from(bytes).subarray(-1024).includes(Buffer.from('%%EOF'))) throw new Error('PDF 文件损坏或转换未产生完整 PDF。'); }

type Options = {
  maxInputBytes?: number; maxOutputBytes?: number; maxQueued?: number; maxCachedBytes?: number;
  convert?: (bytes: Uint8Array, extension: 'docx' | 'pptx', signal: AbortSignal) => Promise<OfficeConversion>;
  inspectOffice?: (bytes: Uint8Array, extension: 'docx' | 'pptx' | 'xlsx') => DocumentCheck;
};
type QueueEntry = { start: () => void; cancel: () => void };

/** One local conversion at a time, bounded queued metadata, and a bounded in-memory LRU. */
export class DocumentPreviewService {
  private active = false;
  private queue: QueueEntry[] = [];
  private cache = new Map<string, { result: FilePreview; bytes: number }>();
  private cacheBytes = 0;
  private lifetime = new AbortController();
  private jobs = new Set<Promise<FilePreview>>();
  constructor(private options: Options = {}) {}

  /** The caller must authorize the real source path before every call, including cache hits. */
  preview(path: string, signal?: AbortSignal, revalidate?: () => string): Promise<FilePreview> {
    const upstream = signal ? AbortSignal.any([signal, this.lifetime.signal]) : this.lifetime.signal;
    const authorize = () => { if (revalidate && revalidate() !== path) throw changed(); };
    const job = this.run(path, upstream, authorize).then(result => { authorize(); return result; });
    this.jobs.add(job); void job.then(() => this.jobs.delete(job), () => this.jobs.delete(job));
    return job;
  }
  async dispose() { this.lifetime.abort(); await Promise.allSettled(this.jobs); this.cache.clear(); this.cacheBytes = 0; }

  private async acquire(signal: AbortSignal): Promise<() => void> {
    checkSignal(signal);
    if (!this.active) { this.active = true; return () => this.release(); }
    if (this.queue.length >= (this.options.maxQueued ?? 4)) throw new Error('文档转换队列已满，请稍后重试。');
    return new Promise((resolve, reject) => {
      const entry: QueueEntry = {
        start: () => { signal.removeEventListener('abort', entry.cancel); resolve(() => this.release()); },
        cancel: () => { this.queue = this.queue.filter(item => item !== entry); signal.removeEventListener('abort', entry.cancel); reject(cancelled()); },
      };
      this.queue.push(entry); signal.addEventListener('abort', entry.cancel, {once:true});
    });
  }
  private release() { const next = this.queue.shift(); if (next) next.start(); else this.active = false; }

  private async run(path: string, signal: AbortSignal, authorize: () => void): Promise<FilePreview> {
    checkSignal(signal);
    const extension = extname(path).toLowerCase();
    const office = extension === '.docx' || extension === '.pptx';
    // Admission happens before opening/reading the source, so queued requests hold metadata only.
    const release = office ? await this.acquire(signal) : undefined;
    try {
      checkSignal(signal); authorize();
      const file = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
      let content: Buffer, initialVersion: string, truncated: boolean;
      try {
        authorize(); checkSignal(signal);
        const info = await file.stat(); if (!info.isFile()) throw new Error('只能预览普通文件。');
        initialVersion = version(info);
        const cacheKey = `${path}\0${initialVersion}`;
        const existing = office && this.cache.get(cacheKey);
        if (existing) {
          if (await sourceVersion(path) !== initialVersion) throw changed(); checkSignal(signal);
          this.cache.delete(cacheKey); this.cache.set(cacheKey, existing);
          return {...existing.result, cached:true};
        }
        const document = office || extension === '.pdf' || extension === '.xlsx';
        const mime = imageTypes[extension];
        const limit = document ? this.options.maxInputBytes ?? DOCUMENT_PREVIEW_LIMIT : mime ? IMAGE_PREVIEW_LIMIT : TEXT_PREVIEW_LIMIT;
        if ((document || mime) && info.size > limit) throw new Error(`文件超过预览大小限制（${Math.round(limit / 1024 / 1024)} MiB），请使用系统打开。`);
        const length = Math.min(info.size, limit); const buffer = Buffer.alloc(length); let read = 0;
        while (read < length) { checkSignal(signal); const {bytesRead} = await file.read(buffer, read, length-read, read); if (!bytesRead) break; read += bytesRead; }
        if (version(await file.stat()) !== initialVersion || await sourceVersion(path) !== initialVersion) throw changed();
        content = buffer.subarray(0, read); truncated = info.size > read;
      } finally { await file.close(); }
      checkSignal(signal);
      const base = {path, sourceVersion:initialVersion};
      if (office) {
        const format = extension.slice(1) as 'docx' | 'pptx';
        const checks = (this.options.inspectOffice ?? inspectOffice)(content, format);
        let result: OfficeConversion;
        try { result = await (this.options.convert ?? convertOffice)(content, format, signal); }
        catch (error) { checkSignal(signal); throw error; }
        checkSignal(signal);
        if (await sourceVersion(path) !== initialVersion) throw changed();
        if (result.pdf.byteLength > (this.options.maxOutputBytes ?? DOCUMENT_PREVIEW_LIMIT)) throw new Error('转换后的 PDF 超过预览大小限制，请使用系统打开。');
        assertPDF(result.pdf);
        const preview: FilePreview = {...base, kind:'pdf', converted:true, checks, missingFonts:result.missingFonts, data:`data:application/pdf;base64,${Buffer.from(result.pdf).toString('base64')}`};
        this.remember(`${path}\0${initialVersion}`, preview);
        return preview;
      }
      if (extension === '.pdf') { assertPDF(content); return {...base, kind:'pdf', data:`data:application/pdf;base64,${content.toString('base64')}`}; }
      if (extension === '.xlsx') return {...base, kind:'spreadsheet', ...spreadsheetPreview(content)};
      const mime = imageTypes[extension]; if (mime) return {...base, kind:'image', data:`data:${mime};base64,${content.toString('base64')}`};
      if (content.subarray(0, 8192).includes(0)) throw new Error('二进制文件请使用系统打开。');
      return {...base, kind:'text', text:new TextDecoder().decode(content, {stream:truncated}), truncated};
    } finally { release?.(); }
  }
  private remember(key: string, result: FilePreview) {
    const bytes = (result.data?.length ?? 0) * 2;
    const maximum = this.options.maxCachedBytes ?? 64 * 1024 * 1024;
    if (bytes > maximum) return;
    while (this.cache.size >= 8 || this.cacheBytes + bytes > maximum) { const oldest = this.cache.keys().next().value!; this.cacheBytes -= this.cache.get(oldest)!.bytes; this.cache.delete(oldest); }
    this.cache.set(key, {result, bytes}); this.cacheBytes += bytes;
  }
}

const defaultPreviews = new DocumentPreviewService();
/** Call only after the host has authorized the real file path. */
export const filePreview = (path: string, signal?: AbortSignal) => defaultPreviews.preview(path, signal);
