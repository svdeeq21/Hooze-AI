import { describe, expect, it } from "vitest";
import { checkFile, isVideoUrl, matchFiles, runPool } from "../src/lib/mediaMatch";

const MENU = ["Jollof rice", "Fried rice", "Chicken", "Chicken 65", "Beef", "Water", "Masa"];

describe("matchFiles", () => {
  it("matches exact names whatever the separators, case or extension", () => {
    expect(matchFiles(["jollof-rice.jpg", "FRIED_RICE.PNG", "Beef.webp", "water.mp4"], MENU)).toEqual([0, 1, 4, 5]);
  });
  it("treats -1, (2), copy, IMG as noise so several photos land on one item", () => {
    expect(matchFiles(["jollof-rice-1.jpg", "jollof rice (2).jpg", "jollof_rice copy.png", "masa 3.jpg"], MENU)).toEqual([0, 0, 0, 6]);
  });
  it("keeps a number that is part of the real name", () => {
    expect(matchFiles(["chicken-65.jpg", "chicken.jpg", "chicken-2.jpg"], MENU)).toEqual([3, 2, 2]);
  });
  it("finds the item inside a longer file name, longest name winning", () => {
    expect(matchFiles(["jollof rice party pack.jpg", "my fried rice photo.jpg"], MENU)).toEqual([0, 1]);
  });
  it("accepts a short file name that belongs to exactly one item, but refuses ambiguity", () => {
    expect(matchFiles(["jollof.jpg", "rice.jpg", "chick.jpg"], MENU)).toEqual([0, null, null]);   // 'rice' fits two items -> ask
  });
  it("never guesses on duplicates, junk or empty names", () => {
    expect(matchFiles(["pizza.jpg", "IMG_0042.jpg", ".jpg"], MENU)).toEqual([null, null, null]);
    expect(matchFiles(["beef.jpg"], ["Beef", "beef"])).toEqual([null]);
  });
});

describe("checkFile", () => {
  const f = (name: string, type: string, mb = 1) => ({ name, type, size: mb * 1024 * 1024 });
  it("accepts photos and mp4, explains everything else", () => {
    expect(checkFile(f("a.jpg", "image/jpeg"))).toBeNull();
    expect(checkFile(f("a.mp4", "video/mp4", 15))).toBeNull();
    expect(checkFile(f("a.heic", "image/heic"))).toContain("HEIC");
    expect(checkFile(f("a.mov", "video/quicktime"))).toContain(".mov");
    expect(checkFile(f("a.pdf", "application/pdf"))).toContain("Only photos");
    expect(checkFile(f("a.mp4", "video/mp4", 20))).toContain("16 MB");
    expect(checkFile(f("a.png", "image/png", 40))).toContain("15 MB");
  });
});

describe("helpers", () => {
  it("detects video links", () => {
    expect(isVideoUrl("https://x/y/a.mp4?v=2")).toBe(true);
    expect(isVideoUrl("https://x/y/a.jpg")).toBe(false);
  });
  it("runPool limits concurrency, keeps order and never rejects", async () => {
    let live = 0, peak = 0;
    const res = await runPool([1, 2, 3, 4, 5, 6], 2, async (n) => { live++; peak = Math.max(peak, live); await new Promise((r) => setTimeout(r, 5)); live--; if (n === 4) throw new Error("boom"); return n * 10; });
    expect(peak).toBe(2);
    expect(res.map((r) => (r.status === "fulfilled" ? r.value : "x"))).toEqual([10, 20, 30, "x", 50, 60]);
  });
});
