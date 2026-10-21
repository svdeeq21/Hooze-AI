import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { api } from "../lib/api";
import { ago, errMsg, slugify } from "../lib/util";
import { Badge, Button, Card, CopyField, Empty, ErrorBox, Field, Input, Loading, Modal, PageHeader, StatusBadge, useToast } from "../components/ui";

export default function Clients() {
  const toast = useToast();
  const qc = useQueryClient();
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["tenants"], queryFn: api.tenants, refetchInterval: 15000 });
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [created, setCreated] = useState<{ id: string; key: string } | null>(null);
  const create = useMutation({
    mutationFn: () => api.createTenant(slug || slugify(name), name.trim()),
    onSuccess: (r) => { qc.invalidateQueries({ queryKey: ["tenants"] }); setCreated({ id: r.tenant.id, key: r.api_key }); },
    onError: (e) => toast("bad", errMsg(e)),
  });
  const close = () => { setOpen(false); setName(""); setSlug(""); setCreated(null); };
  return (
    <>
      <PageHeader title="Clients" sub="Every business using Hooze." action={<Button variant="primary" onClick={() => setOpen(true)}>+ New client</Button>} />
      {q.isLoading ? <Loading /> : q.error ? <ErrorBox error={q.error} retry={() => q.refetch()} /> : !q.data?.length ? <Empty>No clients yet. Create one to start setting up their assistant.</Empty> : (
        <Card className="overflow-hidden !p-0">
          <table className="w-full text-sm">
            <thead className="border-b border-line text-left text-xs uppercase tracking-wider text-muted"><tr><th className="px-4 py-3">Client</th><th>Status</th><th>WhatsApp</th><th>Setup</th><th>Chats</th><th className="pr-4">Added</th></tr></thead>
            <tbody className="divide-y divide-line">{q.data.map((t) => (
              <tr key={t.id} className="hover:bg-raised/50">
                <td className="px-4 py-3"><Link to="/clients/$tenantId" params={{ tenantId: t.id }} search={{ tab: "overview" }} className="font-medium hover:text-amber">{t.name}</Link><div className="text-xs text-muted">{t.slug}</div></td>
                <td><StatusBadge status={t.status} /></td>
                <td>{t.channels ? <Badge tone="ok">{t.channels} connected</Badge> : <Badge tone="bad">not connected</Badge>}</td>
                <td>{t.active_version ? <Badge tone="ok">live · v{t.active_version}</Badge> : <Badge tone="warn">not set up</Badge>}</td>
                <td>{t.conversations}{t.handoff > 0 && <span className="ml-2"><Badge tone="warn">{t.handoff} waiting</Badge></span>}</td>
                <td className="pr-4 text-xs text-muted">{ago(t.created_at)}</td>
              </tr>))}</tbody>
          </table>
        </Card>)}
      <Modal open={open} onClose={close} title={created ? "Client created" : "New client"}>
        {created ? (
          <div className="space-y-4">
            <p className="text-sm text-muted">Save this key now: it is shown <b className="text-text">once</b>. You rarely need it (this dashboard uses your platform key), but it is what you'd give the client for their own dashboard later.</p>
            <CopyField value={created.key} label="Client API key" />
            <Button variant="primary" onClick={() => { const id = created.id; close(); nav({ to: "/clients/$tenantId", params: { tenantId: id }, search: { tab: "setup" } }); }}>Continue to setup →</Button>
          </div>
        ) : (
          <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
            <Field label="Business name"><Input value={name} onChange={(e) => { setName(e.target.value); if (!slug) setSlug(""); }} required autoFocus placeholder="e.g. Savoury Spud" /></Field>
            <Field label="Short ID" hint="Lowercase letters, numbers and dashes. Can't be changed later."><Input value={slug || slugify(name)} onChange={(e) => setSlug(slugify(e.target.value))} required minLength={2} /></Field>
            <div className="flex justify-end gap-2"><Button type="button" variant="ghost" onClick={close}>Cancel</Button><Button type="submit" variant="primary" busy={create.isPending}>Create</Button></div>
          </form>)}
      </Modal>
    </>
  );
}
