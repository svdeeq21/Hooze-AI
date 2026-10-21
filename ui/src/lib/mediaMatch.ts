/**
 * Bulk upload helpers: decide which menu item a file belongs to from its filename, check a file before uploading it, and run uploads a few at a time.
 *
 * Matching is deliberately conservative: a wrong photo on the wrong dish is worse than asking the person to choose, so anything ambiguous is
 * left unmatched (null) and the UI asks. Order of tries: exact name -> exact after dropping "copy / 2 / (1)" noise -> the item's name appears
 * in the file name -> the file name is part of exactly one item's name.
 */
export const isVideoUrl = (u: string) => /\.(mp4|mov|3gp)(\?|#|$)/i.test(u);

const words = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim().replace(/\s+/g, " ");
const stripExt = (n: string) => n.replace(/\.[A-Za-z0-9]{2,5}$/, "");
const NOISE = /\s+(copy|final|new|edited|img|image|photo|pic|\d{1,3})$/;
const stripNoise = (k: string) => { let prev = ""; while (k !== prev) { prev = k; k = k.replace(NOISE, ""); } return k; };
const hasPhrase = (hay: string, needle: string) => needle.length > 0 && ` ${hay} `.includes(` ${needle} `);

/** One entry per file: the index of the offering it belongs to, or null if unsure. */
export function matchFiles(fileNames: string[], offeringNames: string[]): (number | null)[] {
  const names = offeringNames.map(words);
  const unique = (pred: (n: string) => boolean): number | null => {
    const hits = names.flatMap((n, i) => (n && pred(n) ? [i] : []));
    return hits.length === 1 ? hits[0] : null;
  };
  return fileNames.map((raw) => {
    const full = words(stripExt(raw));
    const bare = stripNoise(full);
    if (!full) return null;
    for (const key of [full, bare]) {
      const hits = names.flatMap((n, i) => (n === key ? [i] : []));
      if (hits.length === 1) return hits[0];
      if (hits.length > 1) return null;                                   // two items share this exact name: can't tell
    }
    // the item's name appears inside the file name ("jollof rice party pack" -> "Jollof rice"); the longest such name wins
    const inside = names.flatMap((n, i) => (n.length >= 3 && hasPhrase(bare, n) ? [{ i, len: n.length }] : [])).sort((a, b) => b.len - a.len);
    if (inside.length) return inside.length === 1 || inside[0].len > inside[1].len ? inside[0].i : null;
    // the file name is part of exactly one item's name ("jollof" -> "Jollof rice")
    return bare.length >= 3 ? unique((n) => hasPhrase(n, bare)) : null;
  });
}

export const MAX_IMAGE_MB = 15;
export const MAX_VIDEO_MB = 16;

/** Friendly reason a file can't be uploaded, or null if it looks fine. (The server checks again from the file's real bytes.) */
export function checkFile(f: { name: string; type: string; size: number }): string | null {
  const ext = (f.name.split(".").pop() || "").toLowerCase();
  const type = f.type.toLowerCase();
  if (type === "image/heic" || type === "image/heif" || ext === "heic" || ext === "heif") return "iPhone HEIC photo: share or export it as JPG first.";
  if (type === "video/quicktime" || ext === "mov") return ".mov video: please convert it to .mp4.";
  const isImage = ["image/jpeg", "image/png", "image/webp", "image/gif"].includes(type) || (!type && ["jpg", "jpeg", "png", "webp", "gif"].includes(ext));
  const isVideo = type === "video/mp4" || (!type && ext === "mp4");
  if (!isImage && !isVideo) return "Only photos (JPG, PNG, WebP) and MP4 videos can be uploaded.";
  if (isImage && f.size > MAX_IMAGE_MB * 1024 * 1024) return `Photo is ${Math.round(f.size / 1048576)} MB; the limit is ${MAX_IMAGE_MB} MB.`;
  if (isVideo && f.size > MAX_VIDEO_MB * 1024 * 1024) return `Video is ${Math.round(f.size / 1048576)} MB; WhatsApp's limit is ${MAX_VIDEO_MB} MB.`;
  return null;
}

/** Run `fn` over `items` with at most `limit` in flight. Never rejects: every item gets its own settled result. */
export async function runPool<T, R>(items: T[], limit: number, fn: (item: T, index: number) => Promise<R>): Promise<PromiseSettledResult<R>[]> {
  const out: PromiseSettledResult<R>[] = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      try { out[i] = { status: "fulfilled", value: await fn(items[i], i) }; } catch (reason) { out[i] = { status: "rejected", reason }; }
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}
