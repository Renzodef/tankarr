import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import VolumeCoverDialog from "./VolumeCoverDialog";

afterEach(cleanup);

function dialog(manual = true) {
  const props = {
    volume: "4",
    coverUrl: null,
    manual,
    saving: false,
    onClose: vi.fn(),
    onUpload: vi.fn(),
    onRemove: vi.fn(),
    onReject: vi.fn(),
  };
  render(<VolumeCoverDialog {...props} />);
  return props;
}

it("uploads a chosen image and removes a custom volume cover", () => {
  const props = dialog(true);
  expect(screen.getByRole("button", { name: "Remove custom cover" })).toBeTruthy();
  const file = new File(["png"], "poster.png", { type: "image/png" });
  fireEvent.change(screen.getByLabelText("Upload cover for volume 4"), { target: { files: [file] } });
  fireEvent.click(screen.getByRole("button", { name: "Save cover" }));
  expect(props.onUpload).toHaveBeenCalledWith(file);
  fireEvent.click(screen.getByRole("button", { name: "Remove custom cover" }));
  expect(props.onRemove).toHaveBeenCalledOnce();
});

it("rejects a cover that is too large or not an image and hides removal when automatic", () => {
  const props = dialog(false);
  expect(screen.queryByRole("button", { name: "Remove custom cover" })).toBeNull();
  const text = new File(["hello"], "notes.txt", { type: "text/plain" });
  fireEvent.change(screen.getByLabelText("Upload cover for volume 4"), { target: { files: [text] } });
  expect(props.onReject).toHaveBeenCalledWith("Choose a JPEG, PNG, WebP, GIF, or AVIF image.");
  const huge = new File(["x"], "huge.png", { type: "image/png" });
  Object.defineProperty(huge, "size", { value: 21 * 1024 * 1024 });
  fireEvent.change(screen.getByLabelText("Upload cover for volume 4"), { target: { files: [huge] } });
  expect(props.onReject).toHaveBeenCalledWith("The cover image exceeds the 20 MiB safety limit.");
  expect(props.onUpload).not.toHaveBeenCalled();
});
