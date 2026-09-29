import { mkdtemp, open, rm, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

export type OfficeConversion = { pdf: Uint8Array; missingFonts: string[] };
const MAX_PDF_BYTES = 32 * 1024 * 1024;
/** The engine receives a private snapshot only. It can never save over the user's source. */
export async function convertOffice(bytes: Uint8Array, extension: 'docx' | 'pptx', signal: AbortSignal): Promise<OfficeConversion> {
  let directory: string | undefined;
  let converter: Awaited<ReturnType<typeof import('@deepseek-ai/libreoffice-kit').createConverter>> | undefined;
  try {
    signal.throwIfAborted();
    const { createConverter } = await import('@deepseek-ai/libreoffice-kit');
    converter = await createConverter({
      timeoutMs:60_000, maxInputBytes:32*1024*1024, maxOutputBytes:MAX_PDF_BYTES,
      maxArchiveEntries:5000, maxUncompressedBytes:64*1024*1024, maxImageResolution:192,
    });
    signal.throwIfAborted();
    directory = await mkdtemp(join(tmpdir(), 'astra-office-preview-'));
    const inputPath = join(directory, `source.${extension}`); const outputPath = join(directory, 'preview.pdf');
    await writeFile(inputPath, bytes, {flag:'wx', mode:0o600, signal});
    const result = await converter.render({inputPath, outputPath}, signal);
    signal.throwIfAborted();
    const output = await open(outputPath, 'r');
    try {
      const info = await output.stat();
      if (!info.isFile() || info.size > MAX_PDF_BYTES) throw new Error('转换后的 PDF 超过预览大小限制。');
      const pdf = Buffer.alloc(info.size); let offset = 0;
      while (offset < pdf.length) { signal.throwIfAborted(); const {bytesRead} = await output.read(pdf, offset, pdf.length-offset, offset); if (!bytesRead) break; offset += bytesRead; }
      if (offset !== pdf.length || (await output.stat()).size !== info.size) throw new Error('PDF 转换输出不完整。');
      return {pdf, missingFonts:result.missingFonts};
    } finally { await output.close(); }
  } catch (error) {
    if (signal.aborted) throw new Error('文件预览已取消。');
    const code = (error as {code?: string}).code;
    if (code === 'ERR_MODULE_NOT_FOUND' || code === 'MODULE_NOT_FOUND' || code === 'unavailable') throw new Error('本机未安装可用的 Office 预览引擎。请安装包含文档组件的 Astra，或使用系统打开。');
    if (code === 'timeout') throw new Error('文档转换超过 60 秒，请使用系统打开。');
    throw new Error(`Office 转换失败：${error instanceof Error ? error.message : String(error)}`);
  } finally {
    try { await converter?.dispose(); }
    finally { if (directory) await rm(directory, {recursive:true, force:true}); }
  }
}
