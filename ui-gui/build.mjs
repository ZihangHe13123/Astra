import { build } from "esbuild";
import { build as vite } from "vite";
import { cp, rm } from "node:fs/promises";
await vite({ base: "./", build: { outDir: "dist/renderer", emptyOutDir: true } });
// A paired phone runs the renderer above with this bridge loaded first (src/main/phone-gateway.ts).
await rm("dist/phone", { recursive: true, force: true });
await cp("phone-public", "dist/phone", { recursive: true });
await build({ entryPoints: ["src/phone/bridge-entry.ts"], bundle: true, platform: "browser", format: "iife", target: "safari16", outfile: "dist/phone/bridge.js" });
for (const folder of ["cmaps", "standard_fonts", "wasm"]) await cp(`node_modules/pdfjs-dist/${folder}`, `dist/renderer/pdfjs/${folder}`, { recursive: true });
for (const part of ["main", "preload"]) await build({ entryPoints: [`src/${part}/index.ts`], bundle: true, platform: "node", format: "cjs", external: ["electron", "@deepseek-ai/libreoffice-kit"], outfile: `dist/${part}.cjs` });
