import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Booking } from "../lib/api";
import { ago, cx, errMsg, when } from "../lib/util";
import { Button, Card, Empty, ErrorBox, Field, Input, Loading, Modal, StatusBadge, Textarea, useToast } from "../components/ui";

/** "2026-10-20T14:00:00+01:00" -> "2026-10-20T14:00" as it reads on a wall clock in `tz` (what <input type=datetime-local> wants). */
function toLocalInput(iso: string | null, tz: string): string {
  if (!iso) return "";
  try { return new Intl.DateTimeFormat("sv-SE", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(new Date(iso)).replace(" ", "T"); }
  catch { return ""; }
}

export default function BookingsTab({ id, tz }: { id: string; tz: string }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [filter, setFilter] = useState("pending");
  const [act, setAct] = useState<{ b: Booking; kind: "confirm" | "decline" } | null>(null);
  const [dt, setDt] = useState("");
  const [notes, setNotes] = useState("");
  const q = useQuery({ queryKey: ["bookings", id], queryFn: () => api.bookings(id, "all"), refetchInterval: 10000 });
  const done = () => { setAct(null); qc.invalidateQueries({ queryKey: ["bookings", id] }); qc.invalidateQueries({ queryKey: ["stats", id] }); };
  const decide = useMutation({
    mutationFn: () => act!.kind === "confirm" ? api.confirmBooking(id, act!.b.id, dt, notes) : api.declineBooking(id, act!.b.id, notes),
    onSuccess: () => { toast("ok", act!.kind === "confirm" ? "Confirmed. The customer has been told." : "Declined. The customer has been told."); done(); },
    onError: (e) => { toast("bad", errMsg(e)); },
  });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} retry={() => q.refetch()} />;
  const all = q.data ?? [];
  const rows = all.filter((b) => filter === "all" || b.status === filter);
  const open = (b: Booking, kind: "confirm" | "decline") => { setAct({ b, kind }); setDt(toLocalInput(b.suggested_at, tz)); setNotes(""); };
  return (
    <>
      <div className="mb-4 flex gap-2">{["pending", "confirmed", "declined", "all"].map((f) => (
        <button key={f} onClick={() => setFilter(f)} className={cx("rounded-lg border px-3 py-1.5 text-sm capitalize cursor-pointer", filter === f ? "border-amber/40 bg-amber/10 text-amber" : "border-line text-muted hover:text-text")}>{f}{f !== "all" && ` (${all.filter((b) => b.status === f).length})`}</button>))}</div>
      {rows.length === 0 ? <Empty>No {filter === "all" ? "" : filter} bookings.</Empty> : (
        <div className="space-y-3">{rows.map((b) => (
          <Card key={b.id}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="flex items-center gap-2"><span className="font-medium">{b.contact?.display_name || b.contact?.phone}</span><StatusBadge status={b.status} /></div>
                <div className="mt-1 text-sm">{b.subject || "—"}</div>
                <div className="mt-1 text-xs text-muted">Asked for: <span className="text-text">{b.preferred_text || "no time given"}</span>{b.suggested_at && <> · understood as <span className="text-text">{when(b.suggested_at)}</span></>}</div>
                {b.confirmed_at && <div className="mt-1 text-xs text-ok">Confirmed for {when(b.confirmed_at)}</div>}
                {b.staff_notes && <div className="mt-1 text-xs text-muted">Note: {b.staff_notes}</div>}
                <div className="mt-1 text-xs text-muted">{b.contact?.phone} · requested {ago(b.created_at)}</div>
              </div>
              {b.status === "pending" && <div className="flex gap-2"><Button variant="primary" onClick={() => open(b, "confirm")}>Confirm…</Button><Button variant="danger" onClick={() => open(b, "decline")}>Decline</Button></div>}
            </div>
          </Card>))}</div>)}
      <Modal open={!!act} onClose={() => setAct(null)} title={act?.kind === "confirm" ? "Confirm booking" : "Decline booking"}>
        <div className="space-y-4">
          {act?.kind === "confirm" && <Field label={`Date and time (${tz})`} hint="The customer is told this exact time."><Input type="datetime-local" value={dt} onChange={(e) => setDt(e.target.value)} required /></Field>}
          <Field label={act?.kind === "confirm" ? "Note to the customer (optional)" : "Reason (optional)"}><Textarea value={notes} onChange={(e) => setNotes(e.target.value)} className="min-h-[64px]" /></Field>
          <div className="flex justify-end gap-2"><Button variant="ghost" onClick={() => setAct(null)}>Cancel</Button>
            <Button variant={act?.kind === "confirm" ? "primary" : "danger"} busy={decide.isPending} disabled={act?.kind === "confirm" && !dt} onClick={() => decide.mutate()}>{act?.kind === "confirm" ? "Confirm & tell customer" : "Decline & tell customer"}</Button></div>
        </div>
      </Modal>
    </>
  );
}
