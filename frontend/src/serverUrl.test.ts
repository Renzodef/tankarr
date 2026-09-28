import { describe, expect, it } from "vitest";
import { URL_BASE, resolveUnderBase, serverUrl } from "./serverUrl";

describe("serverUrl", () => {
  it("leaves every URL alone at the root of a host", () => {
    expect(URL_BASE).toBe("");
    expect(serverUrl("/api/manga")).toBe("/api/manga");
    expect(serverUrl("https://example.test/cover.jpg")).toBe("https://example.test/cover.jpg");
    expect(serverUrl("#/wanted")).toBe("#/wanted");
  });

  it("places server-relative URLs under the base and nothing else", () => {
    expect(resolveUnderBase("/tankarr", "/api/manga?fresh=true")).toBe("/tankarr/api/manga?fresh=true");
    expect(resolveUnderBase("/tankarr", "/api/metadata/artwork/x/series")).toBe("/tankarr/api/metadata/artwork/x/series");
    // Already under the base, or not a path on this server: untouched.
    expect(resolveUnderBase("/tankarr", "/tankarr/api/manga")).toBe("/tankarr/api/manga");
    expect(resolveUnderBase("/tankarr", "/tankarr")).toBe("/tankarr");
    expect(resolveUnderBase("/tankarr", "//cdn.example.test/cover.jpg")).toBe("//cdn.example.test/cover.jpg");
    expect(resolveUnderBase("/tankarr", "https://reader.example/book/1")).toBe("https://reader.example/book/1");
    expect(resolveUnderBase("/tankarr", "#/reader/abc")).toBe("#/reader/abc");
    // A base that is a prefix of a path name is not the same directory.
    expect(resolveUnderBase("/tank", "/tankarr/api/manga")).toBe("/tank/tankarr/api/manga");
  });
});
