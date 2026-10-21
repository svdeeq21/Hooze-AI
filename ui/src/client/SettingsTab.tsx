import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, type TenantDetail } from "../lib/api";
import { ago, errMsg } from "../lib/util";
import { Badge, Button, Card, CopyField, Empty, Field, Input, Modal, useToast } from "../components/ui";

export default function SettingsTab({ id, detail }: { id: string; detail: TenantDetail }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [name, setName] = useState(detail.tenant.name);
  const [label, setLabel] = useState("");
  const [newKey, setNewKey] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries();
  const rename = useMutation({ mutationFn: () => api.patchTenant(id, { name: name.trim() }), onSuccess: () => { toast("ok", "Renamed."); refresh(); }, onError: (e) => toast("bad", errMsg(e)) });
  const mk = useMutation({ mutationFn: () => api.createKey(id, label.trim() || "key"), onSuccess: (r) => { setNewKey(r.api_key); setLabel(""); refresh(); }, onError: (e) => toast("bad", errMsg(e)) });
  const rv = useMutation({ mutationFn: (kid: string) => api.revokeKey(id, kid), onSuccess: () => { toast("ok", "Key revoked."); refresh(); }, onError: (e) => toast("bad", errMsg(e)) });
  const nudge = useMutation({ mutationFn: () => api.resetNudges(id), onSuccess: (r) => toast("ok", `Follow-up progress reset for ${r.reset} conversation(s).`), onError: (e) => toast("bad", errMsg(e)) });
  return (
    <div className="space-y-5">
      <Card title="Client name">
        <form className="flex items-end gap-3" onSubmit={(e) => { e.preventDefault(); rename.mutate(); }}>
          <Field label="Name" className="flex-1"><Input value={name} onChange={(e) => setName(e.target.value)} required /></Field>
          <Button type="submit" busy={rename.isPending} disabled={name.trim() === detail.tenant.name}>Save</Button></form>
      </Card>
      <Card title="Follow-up messages">
        <p className="mb-3 text-sm text-muted">While follow-ups are in test mode (“dry run”) each quiet customer's step still advances. Press this <b className="text-text">just before switching follow-ups on for real</b> so everyone starts from the first message. Customers who said “stop” are never re-enrolled.</p>
        <Button busy={nudge.isPending} onClick={() => confirm("Reset follow-up progress for all open conversations of this client?") && nudge.mutate()}>Reset follow-up progress</Button>
      </Card>
      <Card title="Client API keys" action={<span className="text-xs text-muted">for this client's own dashboard (later)</span>}>
        {detail.api_keys.length === 0 ? <Empty>No keys.</Empty> : (
          <ul className="mb-4 divide-y divide-line">{detail.api_keys.map((k) => (
            <li key={k.id} className="flex items-center justify-between py-2 text-sm"><div>{k.label} <span className="text-xs text-muted">· {ago(k.created_at)}</span></div>
              {k.revoked_at ? <Badge tone="bad">revoked</Badge> : <Button size="sm" variant="danger" onClick={() => confirm(`Revoke "${k.label}"? Anything using it stops working.`) && rv.mutate(k.id)}>Revoke</Button>}</li>))}</ul>)}
        <form className="flex items-end gap-3" onSubmit={(e) => { e.preventDefault(); mk.mutate(); }}>
          <Field label="New key label" className="flex-1"><Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. owner dashboard" /></Field><Button type="submit" busy={mk.isPending}>Create key</Button></form>
      </Card>
      <Modal open={!!newKey} onClose={() => setNewKey(null)} title="Key created"><p className="mb-3 text-sm text-muted">Copy it now: it will never be shown again.</p>{newKey && <CopyField value={newKey} />}</Modal>
    </div>
  );
}
