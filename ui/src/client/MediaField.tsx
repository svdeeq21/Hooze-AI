import { useRef, useState } from "react";
import { api } from "../lib/api";
import { checkFile, isVideoUrl, runPool } from "../lib/mediaMatch";
import { Input, Spinner } from "../components/ui";
import { errMsg } from "../lib/util";

/** Photos and videos of ONE menu item: thumbnails, an Upload button (or drag files in), and "paste a link" for files hosted elsewhere.
 *  Each finished upload is handed to the parent immediately (the parent applies it to the latest draft), so several uploads can finish at once safely. */
export function MediaField({ tenantId, urls, onAdd, onRemove }: { tenantId: string; urls: string[]; onAdd: (urls: string[]) => void; onRemove: (url: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(0);
  const [errors, setErrors] = useState<string[]>([]);
  const [over, setOver] = useState(false);
  const [link, setLink] = useState("");
  const [showLink, setShowLink] = useState(false);

  const upload = async (files: File[]) => {
    if (!files.length) return;
    setErrors([]);
    const bad: string[] = [];
    const ok = files.filter((f) => { const e = checkFile(f); if (e) bad.push(`${f.name}: ${e}`); return !e; });
    setBusy((n) => n + ok.length);
    const res = await runPool(ok, 3, async (f) => {
      try { const r = await api.uploadMedia(tenantId, f); onAdd([r.url]); } finally { setBusy((n) => n - 1); }
    });
    res.forEach((r, i) => { if (r.status === "rejected") bad.push(`${ok[i].name}: ${errMsg(r.reason)}`); });
    setErrors(bad);
  };

  const addLink = () => {
    const u = link.trim();
    if (!u) return;
    if (!/^https?:\/\//i.test(u)) { setErrors(["Links must start with https://"]); return; }
    onAdd([u]); setLink(""); setErrors([]);
  };

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        {urls.map((u) => (
          <div key={u} className="group relative h-16 w-16 overflow-hidden rounded-lg border border-line bg-raised">
            {isVideoUrl(u)
              ? <div className="flex h-full w-full flex-col items-center justify-center text-[10px] text-muted"><span className="text-lg">▶</span>video</div>
              : <img src={u} alt="" loading="lazy" className="h-full w-full object-cover" onError={(e) => { (e.currentTarget as HTMLImageElement).style.opacity = "0.25"; }} />}
            <button type="button" title="Remove" onClick={() => onRemove(u)}
              className="absolute right-0.5 top-0.5 hidden h-5 w-5 items-center justify-center rounded-full bg-ink/80 text-xs text-text hover:bg-bad group-hover:flex cursor-pointer">×</button>
          </div>))}
        <button type="button" onClick={() => input.current?.click()}
          onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(e) => { e.preventDefault(); setOver(false); void upload(Array.from(e.dataTransfer.files)); }}
          className={`flex h-16 min-w-[8rem] cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed px-3 text-xs transition ${over ? "border-amber text-amber" : "border-line text-muted hover:text-text"}`}>
          {busy > 0 ? <><Spinner className="h-4 w-4" />Uploading {busy}…</> : <>＋ Add photos / videos</>}
        </button>
        <input ref={input} type="file" multiple hidden accept="image/jpeg,image/png,image/webp,image/gif,video/mp4"
          onChange={(e) => { void upload(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      </div>
      <div className="mt-1.5 text-xs text-muted">
        JPG, PNG or WebP photos and MP4 videos (up to 16 MB). Sent when a customer asks to see this item.{" "}
        <button type="button" className="underline hover:text-text cursor-pointer" onClick={() => setShowLink((v) => !v)}>{showLink ? "hide" : "or paste a link"}</button>
      </div>
      {showLink && (
        <div className="mt-2 flex gap-2">
          <Input value={link} onChange={(e) => setLink(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addLink(); } }} placeholder="https://… a direct link to the photo or video" />
          <button type="button" onClick={addLink} className="rounded-lg border border-line bg-raised px-3 text-xs hover:bg-line cursor-pointer">Add</button>
        </div>)}
      {errors.length > 0 && <ul className="mt-2 space-y-0.5 text-xs text-bad">{errors.map((e, i) => <li key={i}>{e}</li>)}</ul>}
    </div>
  );
}
