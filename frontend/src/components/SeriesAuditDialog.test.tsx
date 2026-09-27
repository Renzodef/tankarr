import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";
import SeriesAuditDialog, { type SeriesAuditClient } from "./SeriesAuditDialog";
import type { SeriesAuditFile, SeriesAuditPreview, SeriesAuditReport, SeriesAuditResult } from "../types";

const file: SeriesAuditFile = { id: "chapter-one", unit: "chapter", number: "1", title: "Example Chapter 1", provider: "suwayomi", provider_manga_id: "exact-source-id", source_name: "Source One", language: "en", pages: 5, verdict: { verdict: "degraded", reason: "Pages have unusual dimensions." }, size_bytes: 1024, first_thumbnail_url: "/api/manga/example/audit/files/chapter-one/first?revision=file-revision", last_thumbnail_url: "/api/manga/example/audit/files/chapter-one/last?revision=file-revision", thumbnail_status: "pending", last_page_black: null, anomalies: [{ code: "few_pages", label: "Fewer than 8 pages" }], can_retire: true, can_reject_source: true };
const report: SeriesAuditReport = { manga_id: "example", revision: "audit-revision", files: [file], sources: [{ provider: file.provider, provider_manga_id: "exact-source-id", source_name: "Source One", language: "en", file_ids: [file.id], can_reject: true }] };
const preview: SeriesAuditPreview = { manga_id: "example", action: "retire", chapter_ids: [file.id], source: null, revision: "review-revision", files: [file], confirmation_snapshot: "a".repeat(64) };
const result: SeriesAuditResult = { manga_id: "example", files_retired: 1, files_deleted: 0, chapters_reset: 1, releases_removed: 0, source_removed: false, cleanup_errors: [] };
const confirmationLabel = "I reviewed these files and confirm moving them to the recycle bin";

function apiClient(): SeriesAuditClient {
  return {
    seriesAudit: vi.fn().mockResolvedValue(report),
    previewAuditRetirement: vi.fn().mockResolvedValue(preview),
    retireAuditFiles: vi.fn().mockResolvedValue(result),
    previewAuditSourceRejection: vi.fn().mockResolvedValue({ ...preview, action: "reject_source", source: { provider: file.provider, provider_manga_id: file.provider_manga_id } }),
    rejectAuditSource: vi.fn().mockResolvedValue({ ...result, source_removed: true, releases_removed: 4 }),
  };
}
function dialog(client = apiClient(), onChanged = vi.fn(), onClose = vi.fn()) {
  return { ...render(<SeriesAuditDialog mangaId="example" title="Example" client={client} onChanged={onChanged} onClose={onClose} />), client, onChanged, onClose };
}
async function selectFile() { fireEvent.click(await screen.findByLabelText("Select Chapter 1 from Source One")); }
async function reviewFiles() { await selectFile(); fireEvent.click(screen.getByRole("button", { name: "Retire selected files… (1)" })); return screen.findByLabelText(confirmationLabel); }
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("shows file quality, source, pages and lazy first and last thumbnails", async () => {
  dialog();
  expect(await screen.findByText("Example Chapter 1")).toBeTruthy();
  expect(screen.getByText(/Source One · English · 5 pages/)).toBeTruthy();
  expect(screen.getByText("Degraded")).toBeTruthy();
  expect(screen.getByText("Pages have unusual dimensions.")).toBeTruthy();
  expect(screen.getByText("Fewer than 8 pages")).toBeTruthy();
  for (const edge of ["First", "Last"]) expect(screen.getByAltText(`${edge} page of Chapter 1 from Source One`).getAttribute("loading")).toBe("lazy");
});

it("previews only the selected files and confirms the reviewed IDs and revision", async () => {
  const { client, onChanged } = dialog();
  await reviewFiles();
  expect(client.previewAuditRetirement).toHaveBeenCalledWith("example", [file.id], "audit-revision", expect.any(AbortSignal));
  expect(client.retireAuditFiles).not.toHaveBeenCalled();
  expect(document.activeElement).toBe(screen.getByRole("heading", { name: "Review selected files" }));
  expect((screen.getByRole("button", { name: "Move files to recycle bin" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText(confirmationLabel));
  fireEvent.click(screen.getByRole("button", { name: "Move files to recycle bin" }));
  expect(await screen.findByRole("status")).toHaveProperty("textContent", "1 file moved to the recycle bin.");
  expect(client.retireAuditFiles).toHaveBeenCalledWith("example", [file.id], preview.revision, preview.confirmation_snapshot, expect.any(AbortSignal));
  expect(onChanged).toHaveBeenCalledOnce();
  await waitFor(() => expect(client.seriesAudit).toHaveBeenCalledTimes(2));
});

it("reviews every file from the exact source before rejecting it", async () => {
  const client = apiClient();
  vi.mocked(client.previewAuditSourceRejection).mockResolvedValue({ ...preview, action: "reject_source", source: { provider: file.provider, provider_manga_id: "exact-source-id" }, chapter_ids: [file.id, "chapter-two"], releases_to_remove: 4, files: [file, { ...file, id: "chapter-two", number: "2", title: "Example Chapter 2", language: "it" }] });
  dialog(client);
  fireEvent.click(await screen.findByRole("button", { name: "Reject Source One for this series" }));
  await screen.findByText("Example Chapter 2");
  expect(screen.getByText("Italiano")).toBeTruthy();
  expect(screen.getByText("4 releases from this source will be removed from Tankarr.")).toBeTruthy();
  expect(client.previewAuditSourceRejection).toHaveBeenCalledWith("example", { provider: file.provider, provider_manga_id: "exact-source-id" }, report.revision, expect.any(AbortSignal));
  expect(client.rejectAuditSource).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("I reviewed this source and its files and confirm rejection and retirement"));
  fireEvent.click(screen.getByRole("button", { name: "Reject source and retire files" }));
  await screen.findByText(/The source was rejected for this series/);
  expect(client.rejectAuditSource).toHaveBeenCalledWith("example", { provider: file.provider, provider_manga_id: "exact-source-id" }, preview.revision, preview.confirmation_snapshot, expect.any(AbortSignal));
});

it("does not offer source rejection for ambiguous origins or retirement for unavailable files", async () => {
  const client = apiClient();
  vi.mocked(client.seriesAudit).mockResolvedValue({ ...report, files: [{ ...file, can_reject_source: false, can_retire: false, thumbnail_status: "unavailable", first_thumbnail_url: null, last_thumbnail_url: null }] });
  dialog(client);
  expect((await screen.findByLabelText("Select Chapter 1 from Source One") as HTMLInputElement).disabled).toBe(true);
  expect(screen.queryByRole("button", { name: "Reject Source One for this series" })).toBeNull();
  expect(screen.getAllByText("Preview unavailable")).toHaveLength(2);
});

it("invalidates the destructive review after a conflict and requires a fresh audit", async () => {
  const client = apiClient();
  vi.mocked(client.retireAuditFiles).mockRejectedValue(new ApiError("Library files changed.", 409));
  dialog(client);
  fireEvent.click(await reviewFiles());
  fireEvent.click(screen.getByRole("button", { name: "Move files to recycle bin" }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", expect.stringContaining("review a new preview"));
  expect(screen.queryByLabelText(confirmationLabel)).toBeNull();
  const newReport = { ...report, revision: "new-audit-revision" };
  vi.mocked(client.seriesAudit).mockResolvedValue(newReport);
  fireEvent.click(screen.getByRole("button", { name: "Refresh audit" }));
  await waitFor(() => expect((screen.getByRole("button", { name: "Retire selected files… (1)" }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.click(screen.getByRole("button", { name: "Retire selected files… (1)" }));
  await screen.findByLabelText(confirmationLabel);
  expect(client.previewAuditRetirement).toHaveBeenLastCalledWith("example", [file.id], newReport.revision, expect.any(AbortSignal));
  expect((screen.getByRole("button", { name: "Move files to recycle bin" }) as HTMLButtonElement).disabled).toBe(true);
});

it("refreshes cached page findings once after first and last thumbnails load", async () => {
  const client = apiClient();
  vi.mocked(client.seriesAudit).mockResolvedValueOnce(report).mockResolvedValue({ ...report, files: [{ ...file, thumbnail_status: "ready", last_page_black: true, anomalies: [...file.anomalies, { code: "last_page_black", label: "Last page is a black card" }] }] });
  dialog(client);
  fireEvent.load(await screen.findByAltText("First page of Chapter 1 from Source One"));
  fireEvent.load(screen.getByAltText("Last page of Chapter 1 from Source One"));
  expect(await screen.findByText("Last page is a black card")).toBeTruthy();
  expect(client.seriesAudit).toHaveBeenCalledTimes(2);
  fireEvent.load(screen.getByAltText("Last page of Chapter 1 from Source One"));
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 300)); });
  expect(client.seriesAudit).toHaveBeenCalledTimes(2);
});

it("shows a failed image as unavailable without retrying it in a loop", async () => {
  const { client } = dialog();
  fireEvent.error(await screen.findByAltText("First page of Chapter 1 from Source One"));
  expect(screen.getByText("Preview unavailable")).toBeTruthy();
  expect(screen.queryByAltText("First page of Chapter 1 from Source One")).toBeNull();
  expect(client.seriesAudit).toHaveBeenCalledOnce();
});

it("ignores an older audit response after changing series", async () => {
  let resolveOld!: (value: SeriesAuditReport) => void;
  const old = new Promise<SeriesAuditReport>((resolve) => { resolveOld = resolve; });
  const client = apiClient();
  vi.mocked(client.seriesAudit).mockReturnValueOnce(old).mockResolvedValue({ ...report, manga_id: "new", files: [{ ...file, title: "New series file" }] });
  const { rerender } = dialog(client);
  rerender(<SeriesAuditDialog mangaId="new" title="New" client={client} onClose={vi.fn()} onChanged={vi.fn()} />);
  await screen.findByText("New series file");
  await act(async () => { resolveOld(report); await old; });
  expect(screen.queryByText("Example Chapter 1")).toBeNull();
  expect(vi.mocked(client.seriesAudit).mock.calls[0][1]?.aborted).toBe(true);
});

it("discards a late preview after the dialog closes", async () => {
  let resolvePreview!: (value: SeriesAuditPreview) => void;
  const pending = new Promise<SeriesAuditPreview>((resolve) => { resolvePreview = resolve; });
  const client = apiClient();
  vi.mocked(client.previewAuditRetirement).mockReturnValue(pending);
  const { onClose } = dialog(client);
  await selectFile();
  fireEvent.click(screen.getByRole("button", { name: "Retire selected files… (1)" }));
  fireEvent.click(screen.getAllByRole("button", { name: "Close" }).at(-1)!);
  expect(onClose).toHaveBeenCalledOnce();
  await act(async () => { resolvePreview(preview); await pending; });
  expect(screen.queryByLabelText(confirmationLabel)).toBeNull();
  expect(client.retireAuditFiles).not.toHaveBeenCalled();
});

it("keeps file rendering bounded and selects only the visible filtered files", async () => {
  const client = apiClient();
  vi.mocked(client.seriesAudit).mockResolvedValue({ ...report, files: Array.from({ length: 25 }, (_, i) => ({ ...file, id: `chapter-${i}`, number: String(i), anomalies: i === 24 ? file.anomalies : [] })) });
  dialog(client);
  await screen.findByText("25 downloaded files. First and last page previews load as you scroll.");
  expect(screen.getAllByRole("article")).toHaveLength(24);
  fireEvent.click(screen.getByRole("button", { name: "Show 1 more file" }));
  expect(screen.getAllByRole("article")).toHaveLength(25);
  fireEvent.click(screen.getByLabelText("Only files with anomalies"));
  expect(screen.getAllByRole("article")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Select visible files" }));
  expect(within(screen.getByRole("article")).getByRole("checkbox")).toHaveProperty("checked", true);
  fireEvent.click(screen.getByRole("button", { name: "Retire selected files… (1)" }));
  await screen.findByLabelText(confirmationLabel);
  expect(client.previewAuditRetirement).toHaveBeenCalledWith("example", ["chapter-24"], report.revision, expect.any(AbortSignal));
});

it("prevents repeat mutation and ignores callbacks after unmount", async () => {
  let resolveMutation!: (value: SeriesAuditResult) => void;
  const pending = new Promise<SeriesAuditResult>((resolve) => { resolveMutation = resolve; });
  const client = apiClient();
  vi.mocked(client.retireAuditFiles).mockReturnValue(pending);
  const { onChanged, unmount } = dialog(client);
  fireEvent.click(await reviewFiles());
  fireEvent.click(screen.getByRole("button", { name: "Move files to recycle bin" }));
  expect((screen.getByRole("button", { name: "Applying…" }) as HTMLButtonElement).disabled).toBe(true);
  expect(client.retireAuditFiles).toHaveBeenCalledOnce();
  unmount();
  await act(async () => { resolveMutation(result); await pending; });
  expect(onChanged).not.toHaveBeenCalled();
});

it("sends explicit file and source reviews with the revision and signed confirmation", async () => {
  const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify({}), { status: 200 }));
  vi.stubGlobal("fetch", fetcher);
  const revision = "b".repeat(64);
  await api.previewAuditRetirement("series/one", ["file-one"], revision);
  expect(fetcher.mock.calls[0][0]).toBe("/api/manga/series%2Fone/audit/retire?dry_run=true");
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ chapter_ids: ["file-one"], revision });
  await api.retireAuditFiles("series/one", ["file-one"], revision, preview.confirmation_snapshot);
  const url = new URL(fetcher.mock.calls[1][0], "https://tankarr.example");
  expect(url.searchParams.get("confirmation_snapshot")).toBe(preview.confirmation_snapshot);
  expect(url.searchParams.has("dry_run")).toBe(false);
  expect(fetcher.mock.calls[1][1].method).toBe("POST");
  expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ chapter_ids: ["file-one"], revision });
  const source = { provider: "suwayomi", provider_manga_id: "exact-source-id" };
  await api.previewAuditSourceRejection("series/one", source, revision);
  expect(fetcher.mock.calls[2][0]).toBe("/api/manga/series%2Fone/audit/reject-source?dry_run=true");
  await api.rejectAuditSource("series/one", source, revision, preview.confirmation_snapshot);
  expect(JSON.parse(fetcher.mock.calls[3][1].body)).toEqual({ ...source, revision });
  expect(new URL(fetcher.mock.calls[3][0], "https://tankarr.example").searchParams.get("confirmation_snapshot")).toBe(preview.confirmation_snapshot);
});
