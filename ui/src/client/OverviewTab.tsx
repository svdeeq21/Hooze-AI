import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type Stats, type TenantDetail } from "../lib/api";
import { errMsg, naira } from "../lib/util";
import { Button, Card, Stat, useToast } from "../components/ui";

const live = (s: string) => ["open", "connected"].includes(s.toLowerCase());

export default function OverviewTab({ id, detail, stats, go }: { id: string; detail: TenantDetail; stats?: Stats; go: (tab: string) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const cfg = detail.active_config?.config;
  const isOrder = cfg?.sales_process.goal === "order";
  const toggle = useMutation({
    mutationFn: (status: "active" | "suspended") => api.patchTenant(id, { status }),
    onSuccess: (_, s) => { toast("ok", s === "suspended" ? "Assistant paused for this client." : "Assistant resumed."); qc.invalidateQueries(); },
    onError: (e) => toast("bad", errMsg(e)),
  });
  const checks = [
    { ok: detail.channels.some((c) => live(c.connection)), label: "WhatsApp number connected", tab: "whatsapp", hint: detail.channels.length ? "A number is added but not connected: scan its QR code." : "Add and scan a WhatsApp number." },
    { ok: !!cfg, label: "Assistant set up and live", tab: "setup", hint: "Fill in the business details and press Go live." },
    { ok: !!cfg && cfg.handoff.staff_phones.length > 0, label: "Staff phone number added for alerts", tab: "setup", hint: "Without it, nobody is told about new bookings or orders." },
    ...(isOrder ? [{ ok: detail.integrations.some((i) => i.kind === "paystack" && i.status === "active"), label: "Paystack connected", tab: "payments", hint: "Customers can't pay until this is connected." }] : []),
  ];
  const ready = checks.every((c) => c.ok);
  return (
    <div className="space-y-5">
      <Card title={ready ? "Ready for customers ✓" : "Before this client can go live"}>
        <ul className="space-y-2">{checks.map((c) => (
          <li key={c.label} className="flex items-center justify-between gap-3 text-sm">
            <div className="flex items-center gap-2.5"><span className={c.ok ? "text-ok" : "text-warn"}>{c.ok ? "●" : "○"}</span><span>{c.label}</span>{!c.ok && <span className="text-xs text-muted">{c.hint}</span>}</div>
            {!c.ok && <Button size="sm" onClick={() => go(c.tab)}>Fix</Button>}
          </li>))}</ul>
      </Card>
      {stats && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Stat label="Chats in progress" value={stats.conversations.active ?? 0} />
          <Stat label="Need a human" value={stats.conversations.handoff ?? 0} tone={stats.conversations.handoff ? "warn" : undefined} />
          <Stat label="Messages in (24h)" value={stats.messages_24h.inbound} hint={`${stats.messages_24h.bot_replies} bot replies`} />
          {isOrder ? <Stat label="Paid (7 days)" value={naira(stats.revenue_minor.last_7d, cfg?.identity.currency)} hint={`${naira(stats.revenue_minor.total, cfg?.identity.currency)} all time`} />
                   : <Stat label="Bookings waiting" value={stats.bookings.pending ?? 0} tone={stats.bookings.pending ? "warn" : undefined} />}
        </div>)}
      <Card title="Assistant control">
        <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
          <p className="max-w-xl text-muted">{detail.tenant.status === "suspended" ? "The assistant is paused: incoming messages are ignored." : "Pausing stops the assistant answering for this client. Nothing is deleted."}</p>
          {detail.tenant.status === "suspended"
            ? <Button variant="primary" busy={toggle.isPending} onClick={() => toggle.mutate("active")}>Resume assistant</Button>
            : <Button variant="danger" busy={toggle.isPending} onClick={() => confirm(`Pause the assistant for ${detail.tenant.name}? Customers will get no replies until you resume.`) && toggle.mutate("suspended")}>Pause assistant</Button>}
        </div>
      </Card>
    </div>
  );
}
