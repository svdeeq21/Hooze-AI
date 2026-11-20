import { useCallback, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type TenantDetail } from "../lib/api";
import { ago, errMsg, when } from "../lib/util";
import ChatPanel from "../components/ChatPanel";
import { Badge, Button, Card, CopyField, Field, Input, Modal, useToast } from "../components/ui";

export function newSession(): string {
  try { return crypto.randomUUID(); } catch { return `s${Date.now().toString(36)}${Math.random().toString(36).slice(2, 12)}`; }
}
function storedSession(key: string): string {
  try { const v = localStorage.getItem(key); if (v) return v; const s = newSession(); localStorage.setItem(key, s); return s; } catch { return newSession(); }
}

export default function TestChatTab({ id, detail, go }: { id: string; detail: TenantDetail; go: (tab: string) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const key = `hooze.testchat.${id}`;
  const [session, setSession] = useState(() => storedSession(key));
  const live = detail.tenant.status === "active" && !!detail.active_config;
  const cfg = detail.active_config?.config;
  const enable = useMutation({ mutationFn: () => api.testChatEnable(id), onSuccess: () => { toast("ok", "Test chat is on. Go to Setup and press Go live to start chatting."); qc.invalidateQueries({ queryKey: ["tenant", id] }); }, onError: (e) => toast("bad", errMsg(e)) });
  const clear = useMutation({ mutationFn: () => api.testChatClear(id), onSuccess: (r) => { toast("ok", `Removed ${r.customers} test customer${r.customers === 1 ? "" : "s"}.`); restart(); qc.invalidateQueries(); }, onError: (e) => toast("bad", errMsg(e)) });

  const send = useCallback((t: string) => api.testChat(id, session, t), [id, session]);
  const pay = useCallback(() => api.testChatPay(id, session), [id, session]);
  const history = useCallback(() => api.testChatHistory(id, session), [id, session]);
  function restart() { const s = newSession(); try { localStorage.setItem(key, s); } catch { /* per-tab only */ } setSession(s); }

  const ideas = cfg ? (cfg.sales_process.goal === "order"
    ? ["What's on the menu?", cfg.offerings[0] ? `I'd like 2 ${cfg.offerings[0].name}` : "What do you recommend?", "Can I talk to a real person?"]
    : [cfg.offerings[0] ? `Tell me about ${cfg.offerings[0].name}` : "What do you offer?", cfg.faqs[0]?.question ?? "How much does it cost?", "Can I talk to a real person?"]) : [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="max-w-2xl"><h2 className="font-display text-lg font-bold">Test chat</h2>
          <p className="text-sm text-muted">Chat with this client's assistant exactly as a customer would. The replies are real (same AI, menu, rules and hand-offs); nothing is sent on WhatsApp, the team isn't texted, and payments are simulated. Test chats show up in Conversations and Orders until you clear them.</p></div>
        <Button size="sm" variant="ghost" busy={clear.isPending} onClick={() => clear.mutate()}>Clear all test chats</Button>
      </div>
      {!live && (
        <Card title="Not live yet">
          <div className="space-y-3 text-sm">
            <p>The test chat talks to the <b>live</b> setup. Finish Setup and press <b>Go live</b> first.</p>
            <p className="text-muted">No WhatsApp number yet? Turn on the test chat: the client can then go live for testing and demo links straight away, and you pair WhatsApp whenever the phone is in hand.</p>
            <div className="flex flex-wrap gap-2"><Button variant="primary" busy={enable.isPending} onClick={() => enable.mutate()}>Turn on test chat</Button><Button onClick={() => go("setup")}>Open Setup</Button></div>
          </div>
        </Card>
      )}
      <ChatPanel key={session} title={detail.tenant.name} subtitle={cfg?.communication.mode === "assist" ? "test chat · runs as Full auto so you see every reply" : "test chat · real AI replies"}
        suggestions={ideas} currency={cfg?.identity.currency ?? "NGN"} send={send} pay={pay} history={history} onNewChat={restart}
        disabled={live ? null : "Go live first, then chat here."} />
      <DemoLinks id={id} live={live} />
    </div>
  );
}

function DemoLinks({ id, live }: { id: string; live: boolean }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [days, setDays] = useState(14);
  const [max, setMax] = useState(150);
  const [made, setMade] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["demo-links", id], queryFn: () => api.demoLinks(id) });
  const create = useMutation({
    mutationFn: () => api.createDemoLink(id, label.trim(), days, max),
    onSuccess: (r) => { setMade(`${window.location.origin}/try/${r.token}`); qc.invalidateQueries({ queryKey: ["demo-links", id] }); },
    onError: (e) => toast("bad", errMsg(e)),
  });
  const revoke = useMutation({ mutationFn: (lid: string) => api.revokeDemoLink(id, lid), onSuccess: () => { toast("ok", "Link switched off."); qc.invalidateQueries({ queryKey: ["demo-links", id] }); }, onError: (e) => toast("bad", errMsg(e)) });
  const now = Date.now();
  return (
    <Card title="Demo links for prospects" action={<Button size="sm" variant="primary" disabled={!live} onClick={() => { setMade(null); setOpen(true); }}>New demo link</Button>}>
      <p className="mb-3 text-sm text-muted">A private web page where a prospect chats with this assistant from their own phone, with no WhatsApp and no sign-in. Each link expires and has a message allowance, so a forwarded link can't run up your AI bill.</p>
      {!q.data?.length ? <p className="text-sm text-muted">{live ? "No links yet." : "Go live first, then create a link."}</p> : (
        <ul className="divide-y divide-line">{q.data.map((l) => {
          const expired = new Date(l.expires_at).getTime() < now;
          const state = l.revoked_at ? <Badge tone="bad">switched off</Badge> : expired ? <Badge tone="mute">expired</Badge> : l.used_messages >= l.max_messages ? <Badge tone="warn">used up</Badge> : <Badge tone="ok">active</Badge>;
          return (
            <li key={l.id} className="flex flex-wrap items-center justify-between gap-2 py-2.5 text-sm">
              <div className="min-w-0"><div className="font-medium">{l.label || "Untitled link"} {state}</div>
                <div className="text-xs text-muted">{l.used_messages} of {l.max_messages} messages · made {ago(l.created_at)} · expires {when(l.expires_at)}</div></div>
              {!l.revoked_at && !expired && <Button size="sm" variant="danger" busy={revoke.isPending} onClick={() => revoke.mutate(l.id)}>Switch off</Button>}
            </li>);
        })}</ul>)}
      <Modal open={open} onClose={() => setOpen(false)} title="New demo link">
        {made ? (
          <div className="space-y-3">
            <p className="text-sm">Send this to the prospect. It is shown <b>once</b>; you can always make another.</p>
            <CopyField value={made} />
            <p className="text-xs text-muted">It opens a chat with this assistant. The page uses your API address; make sure this dashboard's address is in CORS_ALLOWED_ORIGINS on the server.</p>
            <div className="flex justify-end"><Button onClick={() => setOpen(false)}>Done</Button></div>
          </div>
        ) : (
          <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
            <Field label="Who it's for" hint="Only you see this."><Input id="demo-label" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Chops & Grill, Lekki" maxLength={80} /></Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Expires after (days)"><Input id="demo-days" type="number" min={1} max={90} value={days} onChange={(e) => setDays(Math.max(1, Math.min(90, Number(e.target.value) || 1)))} /></Field>
              <Field label="Message allowance"><Input id="demo-max" type="number" min={10} max={2000} value={max} onChange={(e) => setMax(Math.max(10, Math.min(2000, Number(e.target.value) || 10)))} /></Field>
            </div>
            <div className="flex justify-end gap-2"><Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" variant="primary" busy={create.isPending}>Create link</Button></div>
          </form>
        )}
      </Modal>
    </Card>
  );
}

export function WeeklyReportCard({ id }: { id: string }) {
  const toast = useToast();
  const q = useQuery({ queryKey: ["weekly-report", id], queryFn: () => api.weeklyReport(id), retry: false });
  const send = useMutation({ mutationFn: () => api.sendWeeklyReport(id), onSuccess: (r) => toast(r.delivered ? "ok" : "bad", r.delivered ? `Sent to ${r.delivered} staff number${r.delivered === 1 ? "" : "s"}.` : "Couldn't deliver it. Check the client has a connected WhatsApp number and staff numbers."), onError: (e) => toast("bad", errMsg(e)) });
  if (q.error) return null;
  return (
    <Card title="Weekly results for the owner" action={<Button size="sm" busy={send.isPending} disabled={!q.data} onClick={() => send.mutate()}>Send now</Button>}>
      {!q.data ? <p className="text-sm text-muted">Loading…</p> : (
        <div className="space-y-2">
          <div className="whitespace-pre-wrap rounded-lg border-l-2 border-amber bg-raised px-3 py-2 text-sm">{q.data.text}</div>
          <p className="text-xs text-muted">{q.data.automatic ? "Goes to the staff numbers automatically every Monday from 8:00." : "Automatic Monday reports are switched off on the server (WEEKLY_REPORT_ENABLED)."} Test chats are never counted.</p>
        </div>)}
    </Card>
  );
}
