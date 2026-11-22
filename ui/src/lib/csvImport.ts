// Import a whole catalog (hundreds or thousands of listings / products) from a spreadsheet saved as CSV, instead of typing each one.
// Columns are matched by name, in any order, case-insensitive. Anything not recognised becomes a detail shown on the card
// (location, bedrooms, size, title...). Example header: name,price,description,category,location,bedrooms,link,photos
import type { Offering } from "./api";

export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], cell = "", quoted = false;
  const s = text.replace(/^﻿/, "");
  for (let i = 0; i < s.length; i++) {
    const ch = s[i];
    if (quoted) {
      if (ch === '"' && s[i + 1] === '"') { cell += '"'; i++; }
      else if (ch === '"') quoted = false;
      else cell += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { row.push(cell); cell = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && s[i + 1] === "\n") i++;
      row.push(cell); cell = "";
      if (row.some((c) => c.trim())) rows.push(row);
      row = [];
    } else cell += ch;
  }
  row.push(cell);
  if (row.some((c) => c.trim())) rows.push(row);
  return rows;
}

// "₦150,000,000", "150m", "1.5bn", "45k", "4500" -> major units (naira); null if no number
export function parseAmount(raw: string): number | null {
  const m = String(raw ?? "").toLowerCase().replace(/[₦$£,\s]/g, "").replace(/^n(?=\d)/, "").match(/^(\d+(?:\.\d+)?)(k|m|million|b|bn|billion)?$/);
  if (!m) return null;
  const mult: Record<string, number> = { k: 1e3, m: 1e6, million: 1e6, b: 1e9, bn: 1e9, billion: 1e9 };
  return Math.round(parseFloat(m[1]) * (m[2] ? mult[m[2]] : 1));
}

const FIELDS: Record<string, string[]> = {
  name: ["name", "title", "product", "item", "listing", "property"],
  price: ["price", "amount", "cost", "price (ngn)", "price(ngn)", "price ngn"],
  price_note: ["price note", "price_note", "pricing note"],
  description: ["description", "desc", "about", "summary"],
  category: ["category", "section", "type", "group"],
  url: ["link", "url", "website", "page", "tour", "virtual tour"],
  media: ["photos", "images", "pictures", "media", "photo", "image", "videos", "files", "brochure"],
  delivery_url: ["download", "download link", "download_link", "delivery_url", "delivery link", "file link"],
};

export interface ImportResult { offerings: Offering[]; skipped: number; columns: { used: string[]; details: string[] } }

export function rowsToOfferings(rows: string[][]): ImportResult {
  if (rows.length < 2) return { offerings: [], skipped: 0, columns: { used: [], details: [] } };
  const header = rows[0].map((h) => h.trim());
  const role = header.map((h) => Object.entries(FIELDS).find(([, names]) => names.includes(h.toLowerCase()))?.[0] ?? null);
  const offerings: Offering[] = [];
  let skipped = 0;
  for (const r of rows.slice(1)) {
    const o: Offering = { name: "", description: "", price_minor: null, price_note: "", category: "", url: "", media: [], details: {}, option_groups: [], delivery_url: "" };
    header.forEach((h, i) => {
      const v = (r[i] ?? "").trim();
      if (!v) return;
      switch (role[i]) {
        case "name": o.name = v; break;
        case "price": { const a = parseAmount(v); if (a !== null) o.price_minor = a * 100; else o.price_note = v; break; }
        case "price_note": o.price_note = v; break;
        case "description": o.description = v; break;
        case "category": o.category = v; break;
        case "url": o.url = v; break;
        case "media": o.media.push(...v.split(/[|;\s]+/).filter((u) => /^https?:\/\//i.test(u))); break;
        case "delivery_url": o.delivery_url = v; break;
        default: if (h) o.details[h.toLowerCase().replace(/\s+/g, "_")] = v;
      }
    });
    if (!o.name) { skipped++; continue; }
    if (o.price_minor !== null && !o.details.price) o.details.price = `₦${(o.price_minor / 100).toLocaleString()}`;
    offerings.push(o);
  }
  return { offerings, skipped, columns: { used: header.filter((_, i) => role[i]), details: header.filter((h, i) => h && !role[i]) } };
}

// Add the imported items; an item whose name already exists is UPDATED (so re-importing a fresh export of the sheet is safe).
export function mergeOfferings(existing: Offering[], incoming: Offering[]): { merged: Offering[]; added: number; updated: number } {
  const merged = existing.map((o) => ({ ...o }));
  const byName = new Map(merged.map((o, i) => [o.name.trim().toLowerCase(), i]));
  let added = 0, updated = 0;
  for (const o of incoming) {
    const k = o.name.trim().toLowerCase();
    const i = byName.get(k);
    if (i === undefined) { byName.set(k, merged.length); merged.push(o); added++; }
    else { const old = merged[i]; merged[i] = { ...old, ...o, media: o.media.length ? o.media : old.media, option_groups: old.option_groups, details: { ...old.details, ...o.details } }; updated++; }
  }
  return { merged, added, updated };
}
