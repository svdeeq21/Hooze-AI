/** The bulk-upload dialog: files attach to the right item by name, nothing uploads until asked, a failure doesn't lose the rest. */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { BulkMedia } from "../src/client/BulkMedia";
import { api, type Offering } from "../src/lib/api";

const off = (name: string): Offering => ({ name, description: "", price_minor: 100, price_note: "", category: "", url: "", media: [], details: {}, option_groups: [] });
const OFFERINGS = [off("Jollof rice"), off("Fried rice"), off("Chicken")];
const file = (name: string, type = "image/jpeg", size = 2000) => new File([new Uint8Array(size)], name, { type });

beforeAll(() => { (URL as any).createObjectURL = () => "blob:x"; (URL as any).revokeObjectURL = () => {}; });
afterEach(() => vi.restoreAllMocks());

function setup() {
  const onApply = vi.fn();
  render(<BulkMedia tenantId="t1" offerings={OFFERINGS} open onClose={() => {}} onApply={onApply} />);
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  return { onApply, input, user: userEvent.setup({ applyAccept: false }) };     // a dropped file ignores the picker's accept filter, so test that path
}

describe("BulkMedia", () => {
  it("auto-matches by filename, flags the rest, and uploads only what is matched", async () => {
    const up = vi.spyOn(api, "uploadMedia").mockImplementation(async (_id, f) => ({ url: `https://cdn/${f.name}`, type: "image", bytes: 1, name: f.name }));
    const { onApply, input, user } = setup();
    await user.upload(input, [file("jollof-rice-2.jpg"), file("jollof-rice-1.jpg"), file("chicken.png", "image/png"), file("random.jpg"), file("notes.pdf", "application/pdf")]);

    expect(screen.getByText(/3 ready to upload/)).toBeTruthy();
    expect(screen.getByText(/1 need an item chosen/)).toBeTruthy();
    expect(screen.getByText(/1 can't be used/)).toBeTruthy();
    expect(screen.getByText(/Only photos/)).toBeTruthy();
    const names = within(screen.getByRole("list")).getAllByText(/\.(jpg|png|pdf)$/).map((e) => e.textContent);
    expect(names.slice(0, 2)).toEqual(["chicken.png", "jollof-rice-1.jpg"]);                  // natural order: -1 before -2

    await user.click(screen.getByRole("button", { name: /upload 3 files/i }));
    await waitFor(() => expect(screen.getByText(/3 uploaded/)).toBeTruthy());
    expect(up).toHaveBeenCalledTimes(3);                                                      // random.jpg and the pdf never leave the browser
    const applied = onApply.mock.calls.map(([i, urls]) => `${i}:${urls[0].split("/").pop()}`).sort();
    expect(applied).toEqual(["0:jollof-rice-1.jpg", "0:jollof-rice-2.jpg", "2:chicken.png"]);
  });

  it("lets the person choose an item for an unmatched file, and skip one", async () => {
    const up = vi.spyOn(api, "uploadMedia").mockResolvedValue({ url: "https://cdn/x.jpg", type: "image", bytes: 1, name: "x" });
    const { onApply, input, user } = setup();
    await user.upload(input, [file("IMG_0042.jpg"), file("jollof.jpg")]);
    expect(screen.getByRole("button", { name: /upload 1 file$/i })).toBeTruthy();
    const row = screen.getByText("IMG_0042.jpg").closest("li") as HTMLElement;
    await user.selectOptions(within(row).getByRole("combobox"), "Fried rice");
    await user.click(screen.getByRole("button", { name: /upload 2 files/i }));
    await waitFor(() => expect(up).toHaveBeenCalledTimes(2));
    expect(onApply.mock.calls.map(([i]) => i).sort()).toEqual([0, 1]);
  });

  it("keeps successes, reports the failure with its reason, and retries only the failed file", async () => {
    let storageDown = true;
    const up = vi.spyOn(api, "uploadMedia").mockImplementation(async (_id, f) => {
      if (storageDown && f.name === "beef.jpg") throw new Error("Couldn't save the file to storage.");
      return { url: `https://cdn/${f.name}`, type: "image", bytes: 1, name: f.name };
    });
    const { onApply, input, user } = setup();
    await user.upload(input, [file("chicken.jpg"), file("fried rice.jpg")]);
    await user.upload(input, [file("beef.jpg")]);
    await user.selectOptions(within(screen.getByText("beef.jpg").closest("li") as HTMLElement).getByRole("combobox"), "Chicken");
    await user.click(screen.getByRole("button", { name: /upload 3 files/i }));
    await waitFor(() => expect(screen.getByText(/1 failed/)).toBeTruthy());
    expect(screen.getByText(/2 uploaded/)).toBeTruthy();
    expect(screen.getByText(/Couldn't save the file/)).toBeTruthy();
    expect(onApply).toHaveBeenCalledTimes(2);
    storageDown = false;
    await user.click(screen.getByRole("button", { name: /retry \/ upload 1/i }));
    await waitFor(() => expect(screen.getByText(/3 uploaded/)).toBeTruthy());
    expect(up).toHaveBeenCalledTimes(4);                                                      // 3 first time + only the failed one again
  });
});
