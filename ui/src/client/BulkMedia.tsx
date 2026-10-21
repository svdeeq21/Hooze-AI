import { useEffect, useMemo, useRef, useState } from "react";
import type { Offering } from "../lib/api";
import { api } from "../lib/api";
import { checkFile, matchFiles, runPool } from "../lib/mediaMatch";
import { Button, Modal, Select, Spinner } from "../components/ui";
import { errMsg } from "../lib/util";

type Row = { id: number; file: File; preview: string | null; target: number | null; error: string | null; status: "ready" | "uploading" | "done" | "failed"; message?: string };
const natural = (a: File, b: File) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: "base" });

/** Drop in a folder of photos: files named after a menu item attach to it automatically, the rest wait for a choice.
 *  Nothing is uploaded until "Upload" is pressed, so a wrong match can be fixed first. Each finished upload is applied right away. */
export function BulkMedia({ tenantId, offerings, open, onClose, onApply }: {
  tenantId: string; offerings: Offering[]; open: boolean; onClose: () => void; onApply: (index: number, urls: string[]) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [running, setRunning] = useState(false);
  const nextId = useRef(1);
  const names = useMemo(() => offerings.map((o) => o.name), [offerings]);

  useEffect(() => () => rows.forEach((r) => r.preview && URL.revokeObjectURL(r.preview)), []);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (!open) { rows.forEach((r) => r.preview && URL.revokeObjectURL(r.preview)); setRows([]); setRunning(false); } }, [open]);   // eslint-disable-line react-hooks/exhaustive-deps

  const addFiles = (list: File[]) => {
    const files = [...list].sort(natural);
    const targets = matchFiles(files.map((f) => f.name), names);
    setRows((cur) => [...cur, ...files.map((file, i): Row => {
      const error = checkFile(file);
      return { id: nextId.current++, file, preview: !error && file.type.startsWith("image/") ? URL.createObjectURL(file) : null, target: error ? null : targets[i], error, status: "ready" };
    })]);
  };
  const setRow = (id: number, patch: Partial<Row>) => setRows((cur) => cur.map((r) => (r.id === id ? { ...r, ...patch } : r)));

  const todo = rows.filter((r) => !r.error && r.target != null && (r.status === "ready" || r.status === "failed"));
  const needChoice = rows.filter((r) => !r.error && r.target == null && r.status !== "done");
  const done = rows.filter((r) => r.status === "done").length;
  const failed = rows.filter((r) => r.status === "failed").length;

  const run = async () => {
    setRunning(true);
    todo.forEach((r) => setRow(r.id, { status: "uploading", message: undefined }));
    await runPool(todo, 3, async (r) => {
      try {
        const res = await api.uploadMedia(tenantId, r.file);
        onApply(r.target as number, [res.url]);
        setRow(r.id, { status: "done" });
      } catch (e) { setRow(r.id, { status: "failed", message: errMsg(e) }); }
    });
    setRunning(false);
  };

  const label = (o: Offering, i: number) => o.name.trim() || `(unnamed item ${i + 1})`;

  return (
    <Modal open={open} onClose={() => { if (!running) onClose(); }} title="Bulk upload photos and videos" wide>
      <p className="mb-3 text-sm text-muted">Choose many files at once. A file named like an item (<span className="text-text">jollof-rice.jpg</span>, <span className="text-text">jollof-rice-2.jpg</span>) attaches to it automatically. Check the matches, fix any, then upload.</p>
      <input ref={input} type="file" multiple hidden accept="image/jpeg,image/png,image/webp,image/gif,video/mp4" onChange={(e) => { addFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      <div onClick={() => !running && input.current?.click()} onDragOver={(e) => e.preventDefault()} onDrop={(e) => { e.preventDefault(); if (!running) addFiles(Array.from(e.dataTransfer.files)); }}
        className="mb-4 cursor-pointer rounded-xl border border-dashed border-line px-4 py-6 text-center text-sm text-muted hover:text-text">
        Drop files here, or click to choose
      </div>

      {rows.length > 0 && (
        <>
          <div className="mb-2 flex flex-wrap items-center gap-3 text-xs text-muted">
            <span>{todo.length} ready to upload</span>
            {needChoice.length > 0 && <span className="text-warn">{needChoice.length} need an item chosen (or skip)</span>}
            {rows.some((r) => r.error) && <span className="text-bad">{rows.filter((r) => r.error).length} can't be used</span>}
            {done > 0 && <span className="text-ok">{done} uploaded ✓</span>}
            {failed > 0 && <span className="text-bad">{failed} failed</span>}
          </div>
          <ul className="max-h-[46vh] divide-y divide-line overflow-y-auto rounded-lg border border-line">
            {rows.map((r) => (
              <li key={r.id} className="flex items-center gap-3 p-2">
                <div className="flex h-12 w-12 shrink-0 items-center justify-center overflow-hidden rounded-md bg-raised text-xs text-muted">
                  {r.preview ? <img src={r.preview} alt="" className="h-full w-full object-cover" /> : r.file.type.startsWith("video/") ? "▶" : "?"}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-sm">{r.file.name}</div>
                  {r.error ? <div className="text-xs text-bad">{r.error}</div>
                    : r.status === "failed" ? <div className="text-xs text-bad">{r.message}</div>
                    : r.status === "done" ? <div className="text-xs text-ok">Added to “{label(offerings[r.target as number], r.target as number)}”</div>
                    : <div className="text-xs text-muted">{(r.file.size / 1048576).toFixed(1)} MB</div>}
                </div>
                {r.status === "uploading" ? <Spinner className="h-4 w-4" />
                  : r.status === "done" || r.error ? null
                  : <Select className="w-52" value={r.target ?? ""} onChange={(e) => setRow(r.id, { target: e.target.value === "" ? null : Number(e.target.value) })}>
                      <option value="">— skip this file —</option>
                      {offerings.map((o, i) => <option key={i} value={i}>{label(o, i)}</option>)}
                    </Select>}
              </li>))}
          </ul>
        </>)}

      <div className="mt-4 flex items-center justify-between gap-2">
        <span className="text-xs text-muted">Photos are resized automatically. Press “Save” on the Setup page afterwards.</span>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onClose} disabled={running}>{done > 0 && todo.length === 0 ? "Done" : "Close"}</Button>
          <Button variant="primary" onClick={run} busy={running} disabled={todo.length === 0}>{failed > 0 && !running ? `Retry / upload ${todo.length}` : `Upload ${todo.length} file${todo.length === 1 ? "" : "s"}`}</Button>
        </div>
      </div>
    </Modal>
  );
}
