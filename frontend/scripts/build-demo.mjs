// Build the online demo: the interface in demo mode, its service worker and
// the recording of e2e/demo-record.mjs, as one static folder.
//
//   npm run demo:build --prefix frontend                      -> dist-demo/, for /tankarr/demo/
//   TANKARR_DEMO_BASE=/demo/ npm run demo:build --prefix frontend
//
// The folder is served under TANKARR_DEMO_BASE (the docs site publishes it
// at https://renzodef.github.io/tankarr/demo/); the service worker's scope
// is that path, so the base must be the final one, not "./".
import { cp, rm, stat } from "node:fs/promises";
import { resolve } from "node:path";
import { build } from "vite";

const root = resolve(import.meta.dirname, "..");
const base = process.env.TANKARR_DEMO_BASE ?? "/tankarr/demo/";
const outDir = resolve(root, process.env.TANKARR_DEMO_OUTDIR ?? "dist-demo");
const recording = resolve(root, process.env.TANKARR_DEMO_OUTPUT ?? "demo-data");

if (!base.startsWith("/") || !base.endsWith("/")) {
  throw new Error(`TANKARR_DEMO_BASE must start and end with "/": ${base}`);
}
await stat(resolve(recording, "recording.json")).catch(() => {
  throw new Error(`${recording}/recording.json is missing: run npm run demo:record first`);
});

process.env.VITE_TANKARR_DEMO = "1";
await rm(outDir, { recursive: true, force: true });
await build({ root, base, logLevel: "warn", build: { outDir, emptyOutDir: true } });
// The worker is its own entry, outside the page's module graph.
await build({
  root,
  base,
  configFile: false,
  logLevel: "warn",
  build: {
    outDir,
    emptyOutDir: false,
    lib: { entry: resolve(root, "src/demo/sw.ts"), formats: ["es"], fileName: () => "sw.js" },
    rollupOptions: { output: { codeSplitting: false } },
  },
});
await cp(recording, resolve(outDir, "demo-data"), { recursive: true });
console.log(`demo built in ${outDir} for ${base}`);
