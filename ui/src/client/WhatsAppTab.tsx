import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Channel, type TenantDetail } from "../lib/api";
import { errMsg } from "../lib/util";
import { Badge, Button, Card, CopyField, Empty, Field, Input, Loading, Modal, StatusBadge, Toggle, useToast } from "../components/ui";

const isLive = (s: string) => ["open", "connected"].includes(s.toLowerCase());

export default function WhatsAppTab({ id, detail }: { id: string; detail: TenantDetail }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [qrFor, setQrFor] = useState<Channel | null>(null);
  const [hookFor, setHookFor] = useState<Channel | null>(null);
  const [hookBase, setHookBase] = useState("");
  const [form, setForm] = useState({ instance_name: "", base_url: "http://localhost:8080", api_key: "", create_instance: false, webhook_base_url: "http://host.docker.internal:8000" });
  const [result, setResult] = useState<{ url: string; ok: boolean; note: string | null } | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["tenant", id] });

  const provision = useMutation({
    mutationFn: () => api.provision(id, { ...form, instance_name: form.instance_name.trim(), webhook_base_url: form.webhook_base_url.trim() || undefined }),
    onSuccess: (r) => { setResult({ url: r.webhook_url, ok: r.webhook_configured, note: r.note }); refresh(); setForm((f) => ({ ...f, api_key: "", instance_name: "" })); },
    onError: (e) => toast("bad", errMsg(e)),
  });
  const remove = useMutation({
    mutationFn: (c: Channel) => api.removeChannel(id, c.id),
    onSuccess: () => { toast("ok", "Number removed."); refresh(); },
    onError: (e) => toast("bad", errMsg(e)),
  });
  const reconnect = useMutation({
    mutationFn: (c: Channel) => api.reconnectWebhook(id, c.id, hookBase.trim() || undefined),
    onSuccess: () => { toast("ok", "Webhook updated: Evolution now sends messages to us with a fresh secret."); setHookFor(null); refresh(); },
    onError: (e) => toast("bad", errMsg(e)),
  });

  return (
    <div className="space-y-5">
      <Card title="Connected numbers">
        {detail.channels.length === 0 ? <Empty>No WhatsApp number yet. Add one below.</Empty> : (
          <ul className="divide-y divide-line">{detail.channels.map((c) => (
            <li key={c.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
              <div><div className="font-medium">{c.instance_name}</div><div className="text-xs text-muted">{c.base_url}</div></div>
              <div className="flex flex-wrap items-center gap-2">
                {isLive(c.connection) ? <Badge tone="ok">connected</Badge> : <StatusBadge status={c.connection.toUpperCase()} />}
                {!isLive(c.connection) && <Button size="sm" variant="primary" onClick={() => setQrFor(c)}>Show QR code</Button>}
                <Button size="sm" onClick={() => { setHookFor(c); setHookBase(form.webhook_base_url); }}>Reconnect webhook</Button>
                <Button size="sm" variant="ghost" onClick={refresh}>↻</Button>
                <Button size="sm" variant="danger" busy={remove.isPending && remove.variables?.id === c.id} onClick={() => confirm(`Remove ${c.instance_name}? It stops receiving messages and is no longer monitored. Past chats are kept.`) && remove.mutate(c)}>Remove</Button>
              </div>
            </li>))}</ul>)}
      </Card>

      <Card title="Add a WhatsApp number">
        <form className="grid gap-4 md:grid-cols-2" onSubmit={(e) => { e.preventDefault(); provision.mutate(); }}>
          <Field label="Instance name" hint="Any short name, e.g. spudbox-main."><Input value={form.instance_name} onChange={(e) => setForm({ ...form, instance_name: e.target.value })} required minLength={2} /></Field>
          <Field label="Evolution API address" hint="Where hooze-core reaches Evolution (from the machine running hooze-core)."><Input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} required /></Field>
          <Field label="Evolution API key"><Input type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} required /></Field>
          <Field label="How Evolution reaches hooze-core" hint="Docker on Windows: http://host.docker.internal:8000 · same compose stack: http://hooze-core:8000 · live: your public https address."><Input value={form.webhook_base_url} onChange={(e) => setForm({ ...form, webhook_base_url: e.target.value })} /></Field>
          <div className="md:col-span-2"><Toggle checked={form.create_instance} onChange={(v) => setForm({ ...form, create_instance: v })} label="Create this instance on Evolution for me (leave off if it already exists)" /></div>
          <div className="md:col-span-2"><Button type="submit" variant="primary" busy={provision.isPending}>Add number</Button></div>
        </form>
        {result && (
          <div className={`mt-4 rounded-lg border p-4 text-sm ${result.ok ? "border-ok/30 bg-ok/10" : "border-warn/30 bg-warn/10"}`}>
            <div className="mb-2 font-medium">{result.ok ? "Added and webhook set ✓. Now scan the QR code above." : "Added, but the webhook was not set automatically."}</div>
            {result.note && <p className="mb-2 text-muted">{result.note}</p>}
            <CopyField value={result.url} label="Webhook address" />
          </div>)}
      </Card>

      <QrModal id={id} channel={qrFor} onClose={() => { setQrFor(null); refresh(); }} />
      <Modal open={!!hookFor} onClose={() => setHookFor(null)} title="Reconnect webhook">
        <p className="mb-4 text-sm text-muted">This points Evolution at hooze-core again using a <b className="text-text">new secret</b> (the old one is only stored as a hash, so it can't be re-sent). Use it after changing your tunnel address or if messages stopped arriving.</p>
        <Field label="How Evolution reaches hooze-core"><Input value={hookBase} onChange={(e) => setHookBase(e.target.value)} placeholder="Leave empty to use the server's PUBLIC_BASE_URL" /></Field>
        <div className="mt-4 flex justify-end gap-2"><Button variant="ghost" onClick={() => setHookFor(null)}>Cancel</Button><Button variant="primary" busy={reconnect.isPending} onClick={() => hookFor && reconnect.mutate(hookFor)}>Reconnect</Button></div>
      </Modal>
    </div>
  );
}

function QrModal({ id, channel, onClose }: { id: string; channel: Channel | null; onClose: () => void }) {
  const q = useQuery({ queryKey: ["qr", channel?.id], queryFn: () => api.channelQr(id, channel!.id), enabled: !!channel, refetchInterval: (query) => (query.state.data?.connected ? false : 5000), retry: false });
  const src = q.data?.qr_base64 ? (q.data.qr_base64.startsWith("data:") ? q.data.qr_base64 : `data:image/png;base64,${q.data.qr_base64}`) : null;
  return (
    <Modal open={!!channel} onClose={onClose} title={`Pair ${channel?.instance_name ?? ""}`}>
      {q.isLoading ? <Loading label="Asking WhatsApp for a code…" /> : q.error ? <p className="text-sm text-bad">{errMsg(q.error)}</p> : q.data?.connected ? (
        <p className="py-6 text-center text-ok">✓ Connected. You can close this.</p>
      ) : (
        <div className="text-center">
          {src ? <img src={src} alt="WhatsApp pairing QR code" className="mx-auto h-64 w-64 rounded-lg bg-white p-2" /> : <p className="py-6 text-sm text-muted">No QR code yet. Retrying…</p>}
          {q.data?.pairing_code && <p className="mt-3 text-sm">Or enter this code on the phone: <b className="font-mono text-amber">{q.data.pairing_code}</b></p>}
          <ol className="mx-auto mt-4 max-w-xs list-decimal space-y-1 pl-5 text-left text-xs text-muted">
            <li>On the business phone open WhatsApp → Settings → Linked devices.</li><li>Tap “Link a device” and scan this code.</li><li>This window closes updating itself once linked.</li></ol>
        </div>)}
    </Modal>
  );
}
