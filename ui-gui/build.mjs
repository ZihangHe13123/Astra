import { build } from "esbuild";
import { build as vite } from "vite";
await vite({ base: "./", build: { outDir: "dist/renderer", emptyOutDir: true } });
for (const part of ["main", "preload"]) await build({ entryPoints: [`src/${part}/index.ts`], bundle: true, platform: "node", format: "cjs", external: ["electron"], outfile: `dist/${part}.cjs` });
