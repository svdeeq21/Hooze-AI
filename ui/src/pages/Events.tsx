import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { when } from "../lib/util";
import { Card, Empty, ErrorBox, Input, Loading, PageHeader, Select, StatusBadge } from "../components/ui";

export default function Events() {
  const [tenant, setTenant] = useState("");
  const [severity, setSeverity] = useState("WARNING,ERROR,CRITICAL");
  const [event, setEvent] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const tenants = useQuery({ queryKey: ["tenants"], queryFn: api.tenants });
  const q = useQuery({ queryKey: ["events", tenant, severity, event], queryFn: () => api.events({ tenant_id: tenant, severity, event: event.trim().toUpperCase(), limit: 150 }), refetchInterval: 10000 });
  const nameOf = (id: string | null) => tenants.data?.find((t) => t.id === id)?.name ?? (id ? id.slice(0, 8) : "platform");
  return (
    <>
      <PageHeader title="Events" sub="What the system has been doing, and what went wrong." />
      <div className="mb-4 grid gap-3 md:grid-cols-3">
        <Select value={tenant} onChange={(e) => setTenant(e.target.value)}><option value="">All clients</option>{tenants.data?.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}</Select>
        <Select value={severity} onChange={(e) => setSeverity(e.target.value)}>
          <option value="WARNING,ERROR,CRITICAL">Problems only</option><option value="">Everything</option><option value="ERROR,CRITICAL">Errors only</option><option value="INFO">Info only</option></Select>
        <Input value={event} onChange={(e) => setEvent(e.target.value)} placeholder="Filter by name, e.g. TURN_CRASHED" />
      </div>
      {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} /> : !q.data?.length ? <Empty>No events match.</Empty> : (
        <Card className="!p-0"><ul className="divide-y divide-line">{q.data.map((e, i) => (
          <li key={i} className="px-4 py-2.5">
            <button className="flex w-full cursor-pointer items-center justify-between gap-3 text-left" onClick={() => setOpen(open === i ? null : i)}>
              <div className="min-w-0"><div className="truncate font-mono text-xs text-text">{e.event}</div><div className="text-xs text-muted">{nameOf(e.tenant_id)} · {when(e.created_at)}</div></div><StatusBadge status={e.severity} />
            </button>
            {open === i && <pre className="mt-2 overflow-x-auto rounded-lg bg-ink p-3 text-xs text-muted">{JSON.stringify(e.metadata, null, 2)}</pre>}
          </li>))}</ul></Card>)}
    </>
  );
}
