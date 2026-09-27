import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import ImportDecisionsDetails, { importDecisionsSearchText } from "./ImportDecisionsDetails";

afterEach(cleanup);
function expand() {
  const details = screen.getByText(/^Files:/).closest("details")!;
  details.open = true;
  fireEvent(details, new Event("toggle", { bubbles: true }));
}

it("summarizes imported, owned and skipped files and renders details only when opened", () => {
  const evidence = { import_decisions: { imported_paths: ["/srv/library/Example/Example v12.cbz"], already_owned_paths: ["/srv/downloads/Example v01.cbz"], skipped: [{ path: "Extras/cover.cbz", reason: "unnumbered" }] } };
  render(<ImportDecisionsDetails evidence={evidence} />);
  expect(screen.getByText("Files: 1 imported · 1 already owned · 1 skipped")).toBeTruthy();
  expect(screen.queryByText("Example v12.cbz")).toBeNull();
  expand();
  expect(screen.getByText("Example v12.cbz").getAttribute("title")).toBe("/srv/library/Example/Example v12.cbz");
  expect(screen.getByText("Example v01.cbz")).toBeTruthy();
  expect(screen.getByText(/No book or chapter number/)).toBeTruthy();
});

it("uses the rejection skip evidence when import decisions are absent", () => {
  render(<ImportDecisionsDetails evidence={{ verdict: "refused", skip_decisions: [{ path: "Book 2.cbz", reason: "ambiguous_editions" }, { path: "Book 3.cbz", reason: "owned_conflict", detail: "A book was imported while this pack downloaded." }] }} />);
  expand();
  expect(screen.getByText(/Multiple editions for the same book/)).toBeTruthy();
  expect(screen.getByText(/Already owned: A book was imported/)).toBeTruthy();
});

it("shows legacy imported paths and readable free-form skip reasons", () => {
  render(<ImportDecisionsDetails importedPaths={["C:\\Library\\Example v04.cbz"]} evidence={{ skip_decisions: [{ path: "bad.cbz", reason: "Archive has no pages" }] }} />);
  expand();
  expect(screen.getByText("Example v04.cbz")).toBeTruthy();
  expect(screen.getByText(/Archive has no pages/)).toBeTruthy();
});

it("keeps an explicit empty final decision instead of showing older imported paths", () => {
  const { container } = render(<ImportDecisionsDetails importedPaths={["Old book.cbz"]} evidence={{ import_decisions: { imported_paths: [], skipped: [] }, skip_decisions: [{ path: "Old.cbz", reason: "unnumbered" }] }} />);
  expect(container.textContent).toBe("");
});

it("ignores malformed evidence without exposing JSON and keeps file outcomes searchable", () => {
  const evidence = { import_decisions: { imported_paths: [null, 12, "", "Book 1.cbz", "Book 1.cbz"], already_owned_paths: "wrong", skipped: [null, { path: 12 }, { path: "Book 2.cbz", reason: "identical_copy" }] } };
  render(<ImportDecisionsDetails evidence={evidence} />);
  expect(screen.getByText("Files: 1 imported · 1 skipped")).toBeTruthy();
  expect(importDecisionsSearchText(evidence)).toContain("Book 2.cbz Identical duplicate");
  expect(screen.queryByText(/import_decisions/)).toBeNull();
});
