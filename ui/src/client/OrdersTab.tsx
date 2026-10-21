import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Order } from "../lib/api";
import { ago, cx, errMsg, naira, when } from "../lib/util";
import { Badge, Button, Card, Empty, ErrorBox, Loading, Modal, StatusBadge, useToast } from "../components/ui";

const FILTERS: { id: string; label: string; statuses: string }[] = [
  { id: "new", label: "New (paid)", statuses: "PAID" }, { id: "prep", label: "Preparing", statuses: "PREPARING" }, { id: "ready", label: "Ready", statuses: "READY" },
  { id: "unpaid", label: "Awaiting payment", statuses: "PAYMENT_PENDING" }, { id: "done", label: "Completed", statuses: "COMPLETED" }, { id: "lost", label: "Cancelled / expired", statuses: "CANCELLED,EXPIRED" }, { id: "all", label: "All", statuses: "all" },
];
const NEXT: Record<string, { to: string; label: string; primary?: boolean }[]> = {
  PAID: [{ to: "PREPARING", label: "Start preparing", primary: true }, { to: "READY", label: "Mark ready" }],
  PREPARING: [{ to: "READY", label: "Mark ready", primary: true }],
  READY: [{ to: "COMPLETED", label: "Complete (handed over)", primary: true }],
};

export default function OrdersTab({ id, currency }: { id: string; currency: string }) {
  const [f, setF] = useState("new");
  const [open, setOpen] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["orders", id], queryFn: () => api.orders(id, "all"), refetchInterval: 10000 });
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} retry={() => q.refetch()} />;
  const all = q.data ?? [];
  const sel = FILTERS.find((x) => x.id === f)!;
  const rows = sel.statuses === "all" ? all : all.filter((o) => sel.statuses.split(",").includes(o.status));
  const count = (s: string) => (s === "all" ? all.length : all.filter((o) => s.split(",").includes(o.status)).length);
  return (
    <>
      <div className="mb-4 flex flex-wrap gap-2">{FILTERS.map((x) => (
        <button key={x.id} onClick={() => setF(x.id)} className={cx("rounded-lg border px-3 py-1.5 text-sm cursor-pointer", f === x.id ? "border-amber/40 bg-amber/10 text-amber" : "border-line text-muted hover:text-text")}>{x.label} <span className="opacity-70">({count(x.statuses)})</span></button>))}</div>
      {rows.length === 0 ? <Empty>No orders here.</Empty> : (
        <Card className="overflow-hidden !p-0"><table className="w-full text-sm">
          <thead className="border-b border-line text-left text-xs uppercase tracking-wider text-muted"><tr><th className="px-4 py-3">Customer</th><th>Total</th><th>Type</th><th>Status</th><th className="pr-4">Paid</th></tr></thead>
          <tbody className="divide-y divide-line">{rows.map((o) => (
            <tr key={o.id} onClick={() => setOpen(o.id)} className="cursor-pointer hover:bg-raised/50">
              <td className="px-4 py-3"><div className="font-medium">{o.contact?.display_name || o.contact?.phone}</div><div className="text-xs text-muted">#{o.id.slice(0, 6)}</div></td>
              <td>{naira(o.subtotal_minor + (o.delivery_fee_minor || 0), currency)}</td><td className="capitalize text-muted">{(o.fulfillment_method ?? "—").toLowerCase()}</td>
              <td><StatusBadge status={o.status} /></td><td className="pr-4 text-xs text-muted">{o.paid_at ? ago(o.paid_at) : "—"}</td>
            </tr>))}</tbody></table></Card>)}
      <OrderModal id={id} oid={open} currency={currency} onClose={() => setOpen(null)} />
    </>
  );
}

function OrderModal({ id, oid, currency, onClose }: { id: string; oid: string | null; currency: string; onClose: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["order", id, oid], queryFn: () => api.order(id, oid!), enabled: !!oid });
  const move = useMutation({
    mutationFn: (to: string) => api.setOrderStatus(id, oid!, to),
    onSuccess: (r, to) => { toast("ok", to === "READY" ? (r.customer_notified ? "Marked ready. Customer told." : "Marked ready (customer not messaged).") : "Updated."); qc.invalidateQueries({ queryKey: ["orders", id] }); qc.invalidateQueries({ queryKey: ["order", id, oid] }); qc.invalidateQueries({ queryKey: ["stats", id] }); },
    onError: (e) => toast("bad", errMsg(e)),
  });
  const d = q.data;
  const o: Order | undefined = d?.order;
  return (
    <Modal open={!!oid} onClose={onClose} title={o ? `Order #${o.id.slice(0, 6)}` : "Order"} wide>
      {!d || !o ? (q.error ? <ErrorBox error={q.error} /> : <Loading />) : (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div><div className="font-medium">{d.contact?.display_name || d.contact?.phone}</div><div className="text-xs text-muted">{d.contact?.phone} · {when(o.created_at)}</div></div><StatusBadge status={o.status} />
          </div>
          <table className="w-full text-sm"><tbody className="divide-y divide-line">{d.items.map((it) => (
            <tr key={it.id}><td className="py-2">{it.quantity} × {it.product_name}{Array.isArray(it.modifiers) && it.modifiers.length > 0 && <div className="text-xs text-muted">{(it.modifiers as { name?: string }[]).map((m) => m.name).filter(Boolean).join(", ")}</div>}</td>
              <td className="py-2 text-right">{naira(it.line_total_minor ?? it.unit_price_minor * it.quantity, currency)}</td></tr>))}
            {o.delivery_fee_minor > 0 && <tr><td className="py-2 text-muted">Delivery</td><td className="py-2 text-right">{naira(o.delivery_fee_minor, currency)}</td></tr>}
            <tr className="font-medium"><td className="py-2">Total</td><td className="py-2 text-right">{naira(o.subtotal_minor + (o.delivery_fee_minor || 0), currency)}</td></tr></tbody></table>
          <div className="grid gap-3 text-sm md:grid-cols-2">
            <div><div className="text-xs text-muted">Fulfilment</div><div className="capitalize">{(o.fulfillment_method ?? "—").toLowerCase()}</div>{o.delivery_address && <div className="text-muted">{o.delivery_address}</div>}</div>
            <div><div className="text-xs text-muted">Payment</div>{d.payments.length === 0 ? "—" : d.payments.map((p) => <div key={p.id} className="flex items-center gap-2"><Badge tone={p.status === "success" ? "ok" : "warn"}>{p.status}</Badge><span className="font-mono text-xs text-muted">{p.reference}</span></div>)}</div>
          </div>
          {(NEXT[o.status] || ["PAID", "PREPARING", "READY"].includes(o.status)) && (
            <div className="flex flex-wrap justify-end gap-2 border-t border-line pt-4">
              {["PAID", "PREPARING", "READY"].includes(o.status) && <Button variant="danger" disabled={move.isPending} onClick={() => confirm("Cancel this order? The customer will be told and you'll need to refund them in Paystack.") && move.mutate("CANCELLED")}>Cancel order</Button>}
              {(NEXT[o.status] ?? []).map((n) => <Button key={n.to} variant={n.primary ? "primary" : "subtle"} busy={move.isPending && move.variables === n.to} disabled={move.isPending} onClick={() => move.mutate(n.to)}>{n.label}</Button>)}
            </div>)}
          {o.status === "PAYMENT_PENDING" && <p className="text-xs text-muted">Waiting for the customer to pay. It expires automatically if they don't.</p>}
        </div>)}
    </Modal>
  );
}
