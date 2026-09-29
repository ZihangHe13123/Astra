import { build } from "esbuild";
import { build as vite } from "vite";
import { cp } from "node:fs/promises";
await vite({ base: "./", build: { outDir: "dist/renderer", emptyOutDir: true } });
for (const folder of ["cmaps", "standard_fonts", "wasm"]) await cp(`node_modules/pdfjs-dist/${folder}`, `dist/renderer/pdfjs/${folder}`, { recursive: true });
for (const part of ["main", "preload"]) await build({ entryPoints: [`src/${part}/index.ts`], bundle: true, platform: "node", format: "cjs", external: ["electron", "@deepseek-ai/libreoffice-kit"], outfile: `dist/${part}.cjs` });
