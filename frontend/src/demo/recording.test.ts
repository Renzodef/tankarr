import { describe, expect, it } from "vitest";
import { lookup, recordingKey, type Recording } from "./recording";

const recording: Recording = {
  version: 1,
  recorded_at: "2026-10-01T00:00:00Z",
  responses: {
    [recordingKey("GET", "/api/manga?cached=true")]: { status: 200, type: "application/json", body: '[{"id":"a"}]' },
    [recordingKey("GET", "/api/settings")]: { status: 200, type: "application/json", body: '{"log_level":"info"}' },
    [recordingKey("GET", "/api/calendar?days=14&ahead=14")]: { status: 200, type: "application/json", body: '{"releases":[]}' },
    [recordingKey("GET", "/api/metadata/artwork/a/series?v=1")]: { status: 200, type: "image/png", file: "files/a.png" },
    [recordingKey("POST", "/api/wanted/search")]: { status: 200, type: "application/json", body: '{"queued":3}' },
  },
};

describe("the demo answers from its recording", () => {
  it("serves the exact request", () => {
    expect(lookup(recording, "get", "/api/manga?cached=true").body).toBe('[{"id":"a"}]');
    expect(lookup(recording, "GET", "/api/calendar?days=14&ahead=14").body).toBe('{"releases":[]}');
  });

  it("ignores the parameters that only steer the server's cache", () => {
    expect(lookup(recording, "GET", "/api/manga?fresh=true").body).toBe('[{"id":"a"}]');
    expect(lookup(recording, "GET", "/api/settings?fresh=true").body).toBe('{"log_level":"info"}');
  });

  it("falls back to the path when the query was not recorded", () => {
    expect(lookup(recording, "GET", "/api/settings?section=sources").body).toBe('{"log_level":"info"}');
  });

  it("answers an unrecorded read with 404, never with another page's data", () => {
    const missing = lookup(recording, "GET", "/api/manga/unknown");
    expect(missing.status).toBe(404);
    expect(JSON.parse(missing.body ?? "")).toEqual({ detail: "This request is not part of the demo recording" });
  });

  it("points binary responses at their file", () => {
    expect(lookup(recording, "GET", "/api/metadata/artwork/a/series?v=1")).toMatchObject({ type: "image/png", file: "files/a.png" });
  });

  it("acknowledges writes without applying them", () => {
    expect(lookup(recording, "POST", "/api/wanted/search").body).toBe('{"queued":3}');
    expect(JSON.parse(lookup(recording, "PUT", "/api/settings").body ?? "")).toEqual({ applied: [], settings: { log_level: "info" } });
    expect(lookup(recording, "DELETE", "/api/manga/a").status).toBe(204);
    expect(lookup(recording, "POST", "/api/manga?cached=true").body).toBe('[{"id":"a"}]');
    expect(JSON.parse(lookup(recording, "POST", "/api/anything").body ?? "")).toEqual({ ok: true, demo: true });
  });
});
