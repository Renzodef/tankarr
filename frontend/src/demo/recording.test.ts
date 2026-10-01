import { describe, expect, it } from "vitest";
import { lookup, recordingKey, type Recorded, type Recording } from "./recording";

const files: Record<string, string> = {
  "files/manga.json": '[{"id":"a"}]',
  "files/settings.json": '{"log_level":"info"}',
};
const read = async (entry: Recorded) => (entry.file ? files[entry.file] : (entry.body ?? ""));

const recording: Recording = {
  version: 2,
  recorded_at: "2026-10-01T00:00:00Z",
  responses: {
    [recordingKey("GET", "/api/manga?cached=true")]: { status: 200, type: "application/json", file: "files/manga.json" },
    [recordingKey("GET", "/api/settings")]: { status: 200, type: "application/json", file: "files/settings.json" },
    [recordingKey("GET", "/api/calendar?days=14&ahead=14")]: { status: 200, type: "application/json", body: '{"releases":[]}' },
    [recordingKey("GET", "/api/metadata/artwork/a/series?v=1")]: { status: 200, type: "image/png", file: "files/a.png" },
    [recordingKey("POST", "/api/wanted/search")]: { status: 200, type: "application/json", body: '{"queued":3}' },
  },
};

describe("the demo answers from its recording", () => {
  it("serves the exact request, inline or from its file", async () => {
    expect(await lookup(recording, "get", "/api/manga?cached=true", read)).toMatchObject({ file: "files/manga.json" });
    expect((await lookup(recording, "GET", "/api/calendar?days=14&ahead=14", read)).body).toBe('{"releases":[]}');
  });

  it("ignores the parameters that only steer the server's cache", async () => {
    expect(await lookup(recording, "GET", "/api/manga?fresh=true", read)).toMatchObject({ file: "files/manga.json" });
    expect(await lookup(recording, "GET", "/api/settings?fresh=true", read)).toMatchObject({ file: "files/settings.json" });
  });

  it("falls back to the path when the query was not recorded", async () => {
    expect(await lookup(recording, "GET", "/api/settings?section=sources", read)).toMatchObject({ file: "files/settings.json" });
  });

  it("answers an unrecorded read with 404, never with another page's data", async () => {
    const missing = await lookup(recording, "GET", "/api/manga/unknown", read);
    expect(missing.status).toBe(404);
    expect(JSON.parse(missing.body ?? "")).toEqual({ detail: "This request is not part of the demo recording" });
  });

  it("points binary responses at their file", async () => {
    expect(await lookup(recording, "GET", "/api/metadata/artwork/a/series?v=1", read)).toMatchObject({ type: "image/png", file: "files/a.png" });
  });

  it("acknowledges writes without applying them", async () => {
    expect((await lookup(recording, "POST", "/api/wanted/search", read)).body).toBe('{"queued":3}');
    const saved = await lookup(recording, "PUT", "/api/settings", read);
    expect(JSON.parse(saved.body ?? "")).toEqual({ applied: [], settings: { log_level: "info" } });
    expect((await lookup(recording, "DELETE", "/api/manga/a", read)).status).toBe(204);
    expect(await lookup(recording, "POST", "/api/manga?cached=true", read)).toMatchObject({ status: 200, file: "files/manga.json" });
    expect(JSON.parse((await lookup(recording, "POST", "/api/anything", read)).body ?? "")).toEqual({ ok: true, demo: true });
  });
});
