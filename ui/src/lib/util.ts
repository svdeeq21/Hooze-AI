export const naira = (minor: number | null | undefined, currency = "NGN") =>
  minor == null ? "—" : new Intl.NumberFormat("en-NG", { style: "currency", currency, maximumFractionDigits: minor % 100 ? 2 : 0 }).format(minor / 100);

export const ago = (iso: string | null | undefined): string => {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};
export const when = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString("en-NG", { dateStyle: "medium", timeStyle: "short" }) : "—");
export const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);
export const csv = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);
export const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
export const cx = (...c: (string | false | null | undefined)[]) => c.filter(Boolean).join(" ");
export const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** What people type -> the digits WhatsApp uses (mirrors hooze/kernel/phone.py): 0801 234 5678, +234 801…, (0801) 234-5678 -> 2348012345678. */
export const normalizePhone = (raw: string): string => {
  let d = raw.replace(/\(\s*0\s*\)/g, "").replace(/\D/g, "");
  if (!d) return "";
  if (d.startsWith("00")) d = d.slice(2);
  else if (d.startsWith("0") && d.length === 11) d = "234" + d.slice(1);
  else if (d.length === 10 && !d.startsWith("234")) d = "234" + d;
  return d;
};
