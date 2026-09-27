import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";

test("initial JavaScript stays within its compressed startup budget", () => {
  const dist = new URL("../dist/", import.meta.url);
  const html = readFileSync(new URL("index.html", dist), "utf8");
  const entry = html.match(/<script[^>]+src="([^"]+)"/);
  expect(entry, "Vite must emit a module entry point").not.toBeNull();
  const javascript = readFileSync(new URL(entry![1].replace(/^\//, ""), dist));
  expect(gzipSync(javascript).byteLength).toBeLessThan(85_000);
});
