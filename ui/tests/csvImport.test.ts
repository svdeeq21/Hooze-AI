import { describe, expect, it } from "vitest";
import { mergeOfferings, parseAmount, parseCsv, rowsToOfferings } from "../src/lib/csvImport";

describe("catalog import from a spreadsheet (CSV)", () => {
  it("reads quotes, commas inside quotes, blank lines and Windows line endings", () => {
    expect(parseCsv('name,description\r\n"Ikate Penthouse","4 bed, rooftop ""wow"""\r\n\r\nJabi Flat,2 bed\r\n'))
      .toEqual([["name", "description"], ["Ikate Penthouse", '4 bed, rooftop "wow"'], ["Jabi Flat", "2 bed"]]);
  });

  it("understands the ways people write prices", () => {
    expect([parseAmount("₦150,000,000"), parseAmount("150m"), parseAmount("1.5bn"), parseAmount("45k"), parseAmount("N4500"), parseAmount("on request")])
      .toEqual([150000000, 150000000, 1500000000, 45000, 4500, null]);
  });

  it("maps known columns, keeps the rest as card details, and skips rows with no name", () => {
    const csv = "Name,Price,Location,Bedrooms,Link,Photos,Category\nIkate Penthouse,450m,Ikate Lekki,4,https://x.example/ikate,https://c.example/1.jpg|https://c.example/2.jpg,For sale\n,10m,Nowhere,1,,,\nJahi Terrace,on request,Jahi,4,,,For sale";
    const r = rowsToOfferings(parseCsv(csv));
    expect(r.skipped).toBe(1);
    expect(r.columns.details).toEqual(["Location", "Bedrooms"]);
    expect(r.offerings[0]).toMatchObject({ name: "Ikate Penthouse", price_minor: 45000000000, url: "https://x.example/ikate", category: "For sale",
      media: ["https://c.example/1.jpg", "https://c.example/2.jpg"], details: { location: "Ikate Lekki", bedrooms: "4", price: "₦450,000,000" } });
    expect(r.offerings[1]).toMatchObject({ name: "Jahi Terrace", price_minor: null, price_note: "on request" });
  });

  it("re-importing updates existing items by name instead of duplicating them, and handles 1,000 rows", () => {
    const rows = ["name,price"].concat(Array.from({ length: 1000 }, (_, i) => `Item ${i},${1000 + i}`)).join("\n");
    const first = rowsToOfferings(parseCsv(rows)).offerings;
    const a = mergeOfferings([], first);
    expect(a).toMatchObject({ added: 1000, updated: 0 });
    const b = mergeOfferings(a.merged, rowsToOfferings(parseCsv("name,price\nitem 5,9999\nBrand New,10")).offerings);
    expect(b).toMatchObject({ added: 1, updated: 1 });
    expect(b.merged.length).toBe(1001);
    expect(b.merged[5].price_minor).toBe(999900);
  });
});
