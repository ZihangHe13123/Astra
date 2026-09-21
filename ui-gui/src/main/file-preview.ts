import { open } from "node:fs/promises";
import { extname } from "node:path";

export const TEXT_PREVIEW_LIMIT = 256 * 1024;
const IMAGE_PREVIEW_LIMIT = 10 * 1024 * 1024;
const imageTypes: Record<string, string> = { ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif" };

/** Call only after the host has authorized the real file path. */
export async function filePreview(path: string) {
  const file = await open(path, "r");
  try {
    const { size } = await file.stat();
    const mime = imageTypes[extname(path).toLowerCase()];
    if (mime && size > IMAGE_PREVIEW_LIMIT) throw new Error("图片超过预览大小限制，请使用系统打开。");
    const length = Math.min(size, mime ? IMAGE_PREVIEW_LIMIT : TEXT_PREVIEW_LIMIT);
    const buffer = Buffer.alloc(length);
    let read = 0;
    while (read < length) {
      const { bytesRead } = await file.read(buffer, read, length - read, read);
      if (!bytesRead) break;
      read += bytesRead;
    }
    const content = buffer.subarray(0, read);
    if (mime) return { data: `data:${mime};base64,${content.toString("base64")}`, path };
    if (content.subarray(0, 8192).includes(0)) throw new Error("二进制文件请使用系统打开。");
    // stream=true keeps an incomplete trailing UTF-8 sequence out of the preview.
    const truncated = size > read;
    return { text: new TextDecoder().decode(content, { stream: truncated }), truncated, path };
  } finally { await file.close(); }
}
