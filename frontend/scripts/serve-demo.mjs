// Serve the built demo (dist-demo/) under its base path, the way the docs
// site does, for a local look and for the browser smoke test:
//
//   node scripts/serve-demo.mjs [port]      -> http://127.0.0.1:18881/tankarr/demo/
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..", process.env.TANKARR_DEMO_OUTDIR ?? "dist-demo");
const base = process.env.TANKARR_DEMO_BASE ?? "/tankarr/demo/";
const port = Number(process.argv[2] ?? process.env.PORT ?? 18881);

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
  ".avif": "image/avif",
  ".woff2": "font/woff2",
  ".map": "application/json",
  ".txt": "text/plain; charset=utf-8",
};

const server = createServer(async (request, response) => {
  const url = new URL(request.url ?? "/", `http://${request.headers.host}`);
  if (!url.pathname.startsWith(base)) {
    response.writeHead(404, { "Content-Type": "text/plain" }).end(`Not under ${base}`);
    return;
  }
  const relative = decodeURIComponent(url.pathname.slice(base.length)) || "index.html";
  const path = normalize(join(root, relative));
  if (!path.startsWith(root)) {
    response.writeHead(403).end();
    return;
  }
  try {
    const info = await stat(path);
    const file = info.isDirectory() ? join(path, "index.html") : path;
    response.writeHead(200, {
      "Content-Type": TYPES[extname(file)] ?? "application/octet-stream",
      "Cache-Control": "no-cache",
    });
    createReadStream(file).pipe(response);
  } catch {
    response.writeHead(404, { "Content-Type": "text/plain" }).end("Not found");
  }
});

server.listen(port, "127.0.0.1", () => {
  console.log(`demo at http://127.0.0.1:${port}${base}`);
});
