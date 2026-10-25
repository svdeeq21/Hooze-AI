import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { api, type ErasePreview, type KeyRole, type MediaCleanup, type MergeReport, type TenantDetail } from "../lib/api";
import { ago, errMsg } from "../lib/util";
import { Badge, Button, Card, CopyField, Empty, Field, Input, Modal, Select, useToast } from "../components/ui";

const ROLE_HELP: Record<KeyRole, string> = {
  admin: "Everything, including activating the setup and changing payment keys.",
  staff: "Day-to-day work only (chats, orders, bookings). Cannot change setup or payment keys.",
  viewer: "Read-only.",
};
const download = (name: string, data: unknown) => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const a = document.createElement("a"); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url);
};

export default function SettingsTab({ id, detail }: { id: string; detail: TenantDetail }) {
  const toast = useToast();
  const qc = useQueryClient();
  const nav = useNavigate();
  const [name, setName] = useState(detail.tenant.name);
  const [label, setLabel] = useState("");
  const [role, setRole] = useState<KeyRole>("staff");
  const [newKey, setNewKey] = useState<string | null>(null);
  const [photos, setPhotos] = useState<MediaCleanup | null>(null);
  const [chats, setChats] = useState<MergeReport | null>(null);
  const [phone, setPhone] = useState("");
  const [erase, setErase] = useState<ErasePreview | null>(null);
  const [typedSlug, setTypedSlug] = useState("");
  const refresh = () => qc.invalidateQueries();
  const bad = (e: unknown) => toast("bad", errMsg(e));
  const suspended = detail.tenant.status === "suspended";

  const rename = useMutation({ mutationFn: () => api.patchTenant(id, { name: name.trim() }), onSuccess: () => { toast("ok", "Renamed."); refresh(); }, onError: bad });
  const mk = useMutation({ mutationFn: () => api.createKey(id, label.trim() || "key", role), onSuccess: (r) => { setNewKey(r.api_key); setLabel(""); refresh(); }, onError: bad });
  const rv = useMutation({ mutationFn: (kid: string) => api.revokeKey(id, kid), onSuccess: () => { toast("ok", "Key revoked."); refresh(); }, onError: bad });
  const nudge = useMutation({ mutationFn: () => api.resetNudges(id), onSuccess: (r) => toast("ok", `Follow-up progress reset for ${r.reset} conversation(s).`), onError: bad });

  const previewPhotos = useMutation({ mutationFn: () => api.cleanupMedia(id, true), onSuccess: setPhotos, onError: bad });
  const deletePhotos = useMutation({ mutationFn: () => api.cleanupMedia(id, false), onSuccess: (r) => { setPhotos(null); toast("ok", `Deleted ${r.deleted} unused file(s).`); }, onError: bad });
  const previewChats = useMutation({ mutationFn: () => api.mergeChats(id, true), onSuccess: setChats, onError: bad });
  const mergeChats = useMutation({ mutationFn: () => api.mergeChats(id, false), onSuccess: (r) => { setChats(null); toast("ok", `Joined ${r.customers_with_duplicates} customer(s)' chats.`); refresh(); }, onError: bad });
  const reindex = useMutation({
    mutationFn: () => api.reindex(id),
    onSuccess: (r) => { const t = r.tenants[id]; t?.status === "ok" ? toast("ok", `Search index up to date (${t.updated ?? 0} refreshed, ${t.unchanged ?? 0} already current).`) : toast("bad", `Re-index ${t?.status ?? "failed"}${t?.error ? `: ${t.error}` : ""}. Check the embedding model.`); },
    onError: bad,
  });

  const exportData = useMutation({ mutationFn: () => api.exportCustomer(id, phone.trim()), onSuccess: (d) => { download(`customer-${phone.replace(/\D/g, "")}.json`, d); toast("ok", "Downloaded."); }, onError: bad });
  const previewErase = useMutation({ mutationFn: () => api.eraseCustomer(id, phone.trim(), true), onSuccess: setErase, onError: bad });
  const doErase = useMutation({ mutationFn: () => api.eraseCustomer(id, phone.trim(), false), onSuccess: () => { setErase(null); setPhone(""); toast("ok", "Customer's personal data erased. Orders and payments were kept."); }, onError: bad });

  const del = useMutation({
    mutationFn: () => api.deleteClient(id, detail.tenant.slug),
    onSuccess: (r) => {
      if (r.instances_to_remove_by_hand.length) alert(`Client deleted, but these WhatsApp instances could not be removed from the Evolution server. Remove them by hand:\n\n${r.instances_to_remove_by_hand.map((i) => `${i.instance} (${i.error})`).join("\n")}`);
      else toast("ok", `Client deleted (${r.files_deleted} file(s) and ${r.instances_deleted} WhatsApp instance(s) removed).`);
      refresh(); nav({ to: "/clients" });
    },
    onError: bad,
  });

  return (
    <div className="space-y-5">
      <Card title="Client name">
        <form className="flex items-end gap-3" onSubmit={(e) => { e.preventDefault(); rename.mutate(); }}>
          <Field label="Name" className="flex-1"><Input value={name} onChange={(e) => setName(e.target.value)} required /></Field>
          <Button type="submit" busy={rename.isPending} disabled={name.trim() === detail.tenant.name}>Save</Button></form>
      </Card>

      <Card title="Client API keys" action={<span className="text-xs text-muted">for this client's own dashboard (later)</span>}>
        {detail.api_keys.length === 0 ? <Empty>No keys.</Empty> : (
          <ul className="mb-4 divide-y divide-line">{detail.api_keys.map((k) => (
            <li key={k.id} className="flex items-center justify-between py-2 text-sm"><div>{k.label} <Badge tone="mute">{k.role ?? "staff"}</Badge> <span className="text-xs text-muted">· {ago(k.created_at)}</span></div>
              {k.revoked_at ? <Badge tone="bad">revoked</Badge> : <Button size="sm" variant="danger" onClick={() => confirm(`Revoke "${k.label}"? Anything using it stops working.`) && rv.mutate(k.id)}>Revoke</Button>}</li>))}</ul>)}
        <form className="flex items-end gap-3" onSubmit={(e) => { e.preventDefault(); mk.mutate(); }}>
          <Field label="New key label" className="flex-1"><Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. owner dashboard" /></Field>
          <Field label="Access"><Select aria-label="Key access" value={role} onChange={(e) => setRole(e.target.value as KeyRole)}><option value="staff">Staff</option><option value="viewer">Viewer (read-only)</option><option value="admin">Admin</option></Select></Field>
          <Button type="submit" busy={mk.isPending}>Create key</Button></form>
        <p className="mt-2 text-xs text-muted">{ROLE_HELP[role]} Existing keys are Staff.</p>
      </Card>

      <Card title="Housekeeping">
        <div className="space-y-5">
          <div>
            <div className="text-sm font-medium">Unused photos and videos</div>
            <p className="mb-2 text-sm text-muted">Files uploaded for this client that no menu item uses any more (and are over a day old) are removed automatically each time you save. This shows what would go.</p>
            <Button busy={previewPhotos.isPending} onClick={() => previewPhotos.mutate()}>Preview unused files</Button>
            {photos && (<div className="mt-3 rounded-lg border border-line p-3 text-sm">
              {photos.skipped ? <span>Nothing was checked: {photos.skipped}.</span> : <span><b>{photos.would_delete.length}</b> unused file(s) would be deleted. {photos.in_use} in use, {photos.too_new} too new to touch.</span>}
              {photos.would_delete.length > 0 && <div className="mt-2"><Button variant="danger" size="sm" busy={deletePhotos.isPending} onClick={() => confirm(`Delete ${photos.would_delete.length} unused file(s) permanently?`) && deletePhotos.mutate()}>Delete {photos.would_delete.length} file(s)</Button></div>}
            </div>)}
          </div>
          <div>
            <div className="text-sm font-medium">Customers with two chats</div>
            <p className="mb-2 text-sm text-muted">New customers now keep ONE chat across all of this client's numbers. A customer who already messaged two numbers still has two. This joins them (nothing is lost).</p>
            <Button busy={previewChats.isPending} onClick={() => previewChats.mutate()}>Check for duplicates</Button>
            {chats && (<div className="mt-3 rounded-lg border border-line p-3 text-sm">
              {chats.customers_with_duplicates === 0 ? "No customer has duplicate chats." : <span><b>{chats.customers_with_duplicates}</b> customer(s) have more than one chat.</span>}
              {chats.customers_with_duplicates > 0 && <div className="mt-2"><Button size="sm" busy={mergeChats.isPending} onClick={() => mergeChats.mutate()}>Join their chats</Button></div>}
            </div>)}
          </div>
          <div>
            <div className="text-sm font-medium">Knowledge search</div>
            <p className="mb-2 text-sm text-muted">Rebuilds what the assistant searches when answering questions (FAQs, listings). Needed after the embedding model changes. Safe to repeat.</p>
            <Button busy={reindex.isPending} onClick={() => reindex.mutate()}>Rebuild search index</Button>
          </div>
          <div>
            <div className="text-sm font-medium">Follow-up messages</div>
            <p className="mb-2 text-sm text-muted">Test mode no longer advances anyone's progress, so nothing needs resetting when you go live. This is only for conversations advanced by an older version.</p>
            <Button busy={nudge.isPending} onClick={() => confirm("Reset follow-up progress for all open conversations of this client?") && nudge.mutate()}>Reset follow-up progress</Button>
          </div>
        </div>
      </Card>

      <Card title="Customer privacy">
        <p className="mb-3 text-sm text-muted">For a customer's request to see or delete their data. Erasing removes their number, name, messages, bookings and delivery addresses. Orders and payments are kept as your records, without their details.</p>
        <Field label="Customer's phone number"><Input value={phone} onChange={(e) => { setPhone(e.target.value); setErase(null); }} placeholder="2348012345678" /></Field>
        <div className="mt-3 flex flex-wrap gap-3">
          <Button disabled={phone.replace(/\D/g, "").length < 7} busy={exportData.isPending} onClick={() => exportData.mutate()}>Download their data</Button>
          <Button disabled={phone.replace(/\D/g, "").length < 7} busy={previewErase.isPending} onClick={() => previewErase.mutate()}>Preview erasing</Button>
        </div>
        {erase && (<div className="mt-3 rounded-lg border border-line p-3 text-sm">
          Would erase <b>{erase.messages}</b> message(s), <b>{erase.conversations}</b> chat(s), <b>{erase.bookings}</b> booking(s) and <b>{erase.queued_webhooks}</b> queued message copy(ies). <b>{erase.orders_kept_but_scrubbed}</b> order(s) are kept with the address removed. This cannot be undone.
          <div className="mt-2"><Button variant="danger" size="sm" busy={doErase.isPending} onClick={() => confirm("Permanently erase this customer's personal data?") && doErase.mutate()}>Erase now</Button></div>
        </div>)}
      </Card>

      <Card title="Delete this client">
        <p className="mb-3 text-sm text-muted">Permanently removes the client, all its data, its uploaded files and its WhatsApp numbers from the server. {suspended ? "Type the short id to confirm." : "Pause the client first (Overview), then come back."}</p>
        <div className="flex items-end gap-3">
          <Field label={`Type “${detail.tenant.slug}”`} className="flex-1"><Input aria-label="Confirm client id" value={typedSlug} onChange={(e) => setTypedSlug(e.target.value)} disabled={!suspended} /></Field>
          <Button variant="danger" busy={del.isPending} disabled={!suspended || typedSlug !== detail.tenant.slug} onClick={() => confirm(`Delete ${detail.tenant.name} forever? This cannot be undone.`) && del.mutate()}>Delete client</Button>
        </div>
      </Card>

      <Modal open={!!newKey} onClose={() => setNewKey(null)} title="Key created"><p className="mb-3 text-sm text-muted">Copy it now: it will never be shown again.</p>{newKey && <CopyField value={newKey} />}</Modal>
    </div>
  );
}
