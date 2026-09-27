import { afterEach, expect, it, vi } from "vitest";
import { api } from "./api";

afterEach(() => vi.unstubAllGlobals());

it.each(["preview", "save"] as const)("sends the editor's complete boundary list as a replacement on %s", async (action) => {
  const boundaries = [{ volume: "1.5", first_chapter: "0.00000000000000001" }];
  const snapshot = "confirmed/snapshot";
  const response = { boundaries, intervals: [], warnings: [], confirmation_snapshot: snapshot };
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));
  vi.stubGlobal("fetch", fetcher);

  const result = action === "preview"
    ? await api.previewChapterMap("series/one", boundaries)
    : await api.saveChapterMap("series/one", boundaries, snapshot);

  expect(fetcher).toHaveBeenCalledTimes(1);
  const [path, options] = fetcher.mock.calls[0];
  expect(path).toBe(`/api/manga/series%2Fone/chapter-map?${action === "preview"
    ? "dry_run=true"
    : `confirmation_snapshot=${encodeURIComponent(snapshot)}`}`);
  expect(options?.method).toBe("PUT");
  expect(JSON.parse(options?.body as string)).toEqual({ boundaries, mode: "replace" });
  expect(result).toEqual(response);
});
