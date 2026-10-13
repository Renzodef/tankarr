import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api";
import type { Manga } from "../types";
import SeriesSourcePicker from "./SeriesSourcePicker";

vi.mock("../api", () => ({ api: { releaseSourceCandidates: vi.fn(), addReleaseSource: vi.fn(), clearReleaseSourcePreference: vi.fn() } }));

const manga = { id: "fixture-series", title: "Fixture work", preferred_language: "en", preferred_source_key: "suwayomi:101", release_sources: [] } as unknown as Manga;

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);

it("maps the selected title and pins its source only for the current series", async () => {
  vi.mocked(api.releaseSourceCandidates).mockResolvedValue({ candidates: [{ provider: "suwayomi", id: "fixture-entry", title: "Fixture work", source_name: "Fixture source", language: "en" }], errors: [] });
  vi.mocked(api.addReleaseSource).mockResolvedValue({ releases: 1, new: 1, queued: 0 });
  const changed = vi.fn();
  render(<SeriesSourcePicker manga={manga} onChanged={changed} />);
  fireEvent.click(screen.getByText("Download source"));
  fireEvent.click(screen.getByRole("button", { name: /^Search$/ }));
  await screen.findByText("Fixture source · Fixture work · en");
  fireEvent.change(screen.getByLabelText("Choose a source"), { target: { value: "0" } });
  fireEvent.click(screen.getByRole("button", { name: "Use this source" }));
  await waitFor(() => expect(api.addReleaseSource).toHaveBeenCalledWith("fixture-series", "suwayomi", "fixture-entry", true));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
});

it("returns the current series to automatic selection", async () => {
  vi.mocked(api.clearReleaseSourcePreference).mockResolvedValue({});
  const changed = vi.fn();
  render(<SeriesSourcePicker manga={manga} onChanged={changed} />);
  fireEvent.click(screen.getByText("Download source"));
  fireEvent.click(screen.getByRole("button", { name: "Use automatic source selection" }));
  await waitFor(() => expect(api.clearReleaseSourcePreference).toHaveBeenCalledWith("fixture-series"));
  await waitFor(() => expect(changed).toHaveBeenCalledOnce());
});
