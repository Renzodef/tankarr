import { afterEach, describe, expect, it } from "vitest";
import { activate, detectLocale, saveLocale, storedLocale, t, tn } from "./index";

afterEach(async () => {
  window.localStorage.clear();
  await activate("en");
});

describe("t", () => {
  it("returns the English text when nothing is loaded", () => {
    expect(t("Refresh view")).toBe("Refresh view");
    expect(t("Monitored: {mode}", { mode: "all" })).toBe("Monitored: all");
    expect(t("Keeps {unknown}")).toBe("Keeps {unknown}");
  });

  it("translates from the active catalogue and falls back per string", async () => {
    await activate("it", { "Refresh view": "Aggiorna vista" });
    expect(t("Refresh view")).toBe("Aggiorna vista");
    expect(t("Save Changes")).toBe("Save Changes");
    expect(document.documentElement.lang).toBe("it");
  });
});

describe("tn", () => {
  it("picks the English form from the count", () => {
    expect(tn(1, "{count} chapter", "{count} chapters")).toBe("1 chapter");
    expect(tn(0, "{count} chapter", "{count} chapters")).toBe("0 chapters");
  });

  it("uses the catalogue's plural forms with the language's rules", async () => {
    await activate("it", { "{count} chapters": { one: "{count} capitolo", other: "{count} capitoli" } });
    expect(tn(1, "{count} chapter", "{count} chapters")).toBe("1 capitolo");
    expect(tn(3, "{count} chapter", "{count} chapters")).toBe("3 capitoli");
    // The singular key alone, without a count, gives the general form.
    expect(t("{count} chapters", { count: 2 })).toBe("2 capitoli");
  });
});

describe("locale choice", () => {
  it("prefers the stored choice, then the browser, then English", () => {
    expect(detectLocale(["fr-FR", "it-IT"])).toBe("it");
    expect(detectLocale(["de"])).toBe("en");
    saveLocale("it");
    expect(storedLocale()).toBe("it");
    expect(detectLocale(["en-US"])).toBe("it");
    saveLocale("auto");
    expect(storedLocale()).toBe("auto");
    expect(detectLocale(["en-US"])).toBe("en");
  });

  it("ignores an unknown stored value", () => {
    window.localStorage.setItem("tankarr.locale", "klingon");
    expect(storedLocale()).toBe("auto");
  });
});
