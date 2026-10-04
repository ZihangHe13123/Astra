import type { MermaidConfig } from "mermaid";

export const MAX_MERMAID_SOURCE = 20_000;
export const MAX_MERMAID_SVG = 1_000_000;
const MAX_SVG_DIMENSION = 8192;
const MAX_SVG_AREA = 4_000_000;
const MAX_PENDING = 16;
const MAX_CACHE_ENTRIES = 16;
const MAX_CACHE_CHARS = 2_000_000;
export type MermaidTheme = "light" | "dark";
class MermaidRenderError extends Error {}
export function mermaidErrorMessage(error: unknown): string {
  return error instanceof MermaidRenderError ? error.message : "图表暂时无法显示，请检查源码后重试。";
}

/** Diagram configuration is owned by the app, not by model-produced text. */
export function checkedMermaidSource(source: string): string {
  if (!source.trim()) throw new MermaidRenderError("图表内容为空。");
  if (source.length > MAX_MERMAID_SOURCE || source.split("\n").length > 500) {
    throw new MermaidRenderError("图表较大，请查看源码。");
  }
  // Mermaid's YAML/frontmatter and init directives can override appearance,
  // sanitizer and resource options. Extended shape metadata can embed images.
  // Keep these inputs as code instead of trying to rewrite their grammar.
  if (/^\s*---(?:\r?\n|$)|%%\s*\{|@\s*\{/u.test(source)
      || /<\s*[!/?a-z]|!\s*\[/iu.test(source)
      || /(?:^|[;\r\n])\s*(?:style|classDef|linkStyle)\s/iu.test(source)
      || /url\s*\(|@import|@font-face/iu.test(source)) {
    throw new MermaidRenderError("此图表包含自定义样式、配置或嵌入内容，请查看源码。");
  }
  return source;
}

export function mermaidConfig(theme: MermaidTheme): MermaidConfig {
  return {
    startOnLoad: false, securityLevel: "strict", suppressErrorRendering: true,
    theme: theme === "dark" ? "dark" : "default", layout: "dagre",
    htmlLabels: false, maxTextSize: MAX_MERMAID_SOURCE, maxEdges: 300,
    fontFamily: "sans-serif", flowchart: { htmlLabels: false },
    secure: ["secure", "securityLevel", "startOnLoad", "maxTextSize", "maxEdges",
      "suppressErrorRendering", "htmlLabels", "dompurifyConfig", "themeCSS",
      "themeVariables", "fontFamily", "flowchart", "layout"],
  };
}

/** Generated SVG may refer to its own markers, but never external resources. */
export function localSvgReferences(value: string): boolean {
  if (/\\|@import|@font-face|expression\s*\(/iu.test(value)) return false;
  for (const match of value.matchAll(/url\s*\(([^)]*)\)/giu)) {
    const target = match[1].trim().replace(/^(["'])(.*)\1$/u, "$2");
    if (!/^#[\w.:-]+$/u.test(target)) return false;
  }
  return true;
}

/** SVG-in-img needs explicit intrinsic dimensions, not Mermaid's width=100%. */
export function mermaidViewBox(value: string | null): { x: number; y: number; width: number; height: number } {
  const fields = value?.trim().split(/[\s,]+/u) || [];
  const number = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/iu;
  if (fields.length !== 4 || fields.some(field => !number.test(field))) {
    throw new MermaidRenderError("图表尺寸无效，请查看源码。");
  }
  const [x, y, width, height] = fields.map(Number);
  if (![x, y, width, height].every(Number.isFinite) || width <= 0 || height <= 0
      || Math.max(Math.abs(x), Math.abs(y), width, height) > MAX_SVG_DIMENSION
      || width * height > MAX_SVG_AREA) {
    throw new MermaidRenderError("图表尺寸较大或无效，请查看源码。");
  }
  return { x, y, width, height };
}

/** A second SVG-only boundary precedes display through a noninteractive img. */
export async function sanitizeMermaidSvg(svg: string): Promise<string> {
  if (!svg || svg.length > MAX_MERMAID_SVG) throw new MermaidRenderError("图表较大，请查看源码。");
  const { default: DOMPurify } = await import("dompurify");
  const clean = DOMPurify.sanitize(svg, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: ["foreignObject", "image", "a", "script", "animate", "animateMotion", "animateTransform", "set"],
    FORBID_ATTR: ["href", "xlink:href"],
  });
  const parsed = new DOMParser().parseFromString(clean, "image/svg+xml");
  const root = parsed.documentElement;
  if (root.localName !== "svg" || parsed.querySelector("parsererror")) throw new MermaidRenderError("图表暂时无法显示，请查看源码。");
  for (const node of [root, ...root.querySelectorAll("*")]) {
    if (node.localName.toLowerCase() === "style" && !localSvgReferences(node.textContent || "")) {
      throw new MermaidRenderError("图表包含外部资源，请查看源码。");
    }
    for (const attribute of [...node.attributes]) {
      if (/^on/iu.test(attribute.name) || /(?:^|:)href$/iu.test(attribute.name)
          || !localSvgReferences(attribute.value)) {
        throw new MermaidRenderError("图表包含外部资源，请查看源码。");
      }
    }
  }
  const { x, y, width, height } = mermaidViewBox(root.getAttribute("viewBox"));
  // Percentage width is appropriate for inline SVG, but gives a blob image a
  // viewport-sized intrinsic canvas and magnifies narrow diagrams. Preserve
  // the layout's own aspect ratio; outer CSS may shrink it to the chat width.
  root.removeAttribute("style");
  root.setAttribute("viewBox", `${x} ${y} ${width} ${height}`);
  root.setAttribute("width", String(width));
  root.setAttribute("height", String(height));
  root.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  return new XMLSerializer().serializeToString(root);
}

type MermaidEngine = {
  initialize(config: MermaidConfig): void;
  render(id: string, source: string): Promise<{ svg: string }>;
};
type RendererDependencies = {
  load: () => Promise<MermaidEngine>;
  sanitize: (svg: string) => Promise<string>;
};

/** Serialize Mermaid's global configuration and bound retained source/SVG data. */
export function createMermaidRenderer(dependencies: RendererDependencies) {
  let queue = Promise.resolve();
  let sequence = 0;
  let cacheChars = 0;
  const cache = new Map<string, string>();
  const pending = new Map<string, Promise<string>>();
  return (source: string, theme: MermaidTheme): Promise<string> => {
    try { checkedMermaidSource(source); } catch (error) { return Promise.reject(error); }
    const key = `${theme}\n${source}`;
    const hit = cache.get(key);
    if (hit !== undefined) { cache.delete(key); cache.set(key, hit); return Promise.resolve(hit); }
    const running = pending.get(key);
    if (running) return running;
    if (pending.size >= MAX_PENDING) return Promise.reject(new MermaidRenderError("图表较多，请稍后重新绘制。"));
    const work = queue.then(async () => {
      const engine = await dependencies.load();
      engine.initialize(mermaidConfig(theme));
      const { svg } = await engine.render(`astra-mermaid-${++sequence}`, source);
      if (svg.length > MAX_MERMAID_SVG) throw new MermaidRenderError("图表较大，请查看源码。");
      const clean = await dependencies.sanitize(svg);
      if (!clean || clean.length > MAX_MERMAID_SVG) throw new MermaidRenderError("图表较大，请查看源码。");
      cache.set(key, clean); cacheChars += key.length + clean.length;
      while (cache.size > MAX_CACHE_ENTRIES || cacheChars > MAX_CACHE_CHARS) {
        const oldest = cache.keys().next().value!;
        cacheChars -= oldest.length + cache.get(oldest)!.length;
        cache.delete(oldest);
      }
      return clean;
    }).catch(error => { throw new MermaidRenderError(mermaidErrorMessage(error)); });
    pending.set(key, work);
    queue = work.then(() => { pending.delete(key); }, () => { pending.delete(key); });
    return work;
  };
}

let engine: Promise<MermaidEngine> | undefined;
export const renderMermaid = createMermaidRenderer({
  load: () => engine ||= import("mermaid").then(module => module.default).catch(error => { engine = undefined; throw error; }),
  sanitize: sanitizeMermaidSvg,
});
