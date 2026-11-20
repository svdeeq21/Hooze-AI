import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Conversation, type Draft, type Message } from "../lib/api";
import { ago, cx, errMsg, when } from "../lib/util";
import { Badge, Button, Empty, ErrorBox, Loading, StatusBadge, Textarea, useToast } from "../components/ui";

export default function ConversationsTab({ id }: { id: string }) {
  const [sel, setSel] = useState<string | null>(null);
  const [onlyHuman, setOnlyHuman] = useState(false);
  const q = useQuery({ queryKey: ["conversations", id], queryFn: () => api.conversations(id), refetchInterval: 5000 });
  const drafts = useQuery({ queryKey: ["drafts", id], queryFn: () => api.drafts(id), refetchInterval: 5000 });
  const waiting = new Map<string, number>();
  for (const d of drafts.data ?? []) waiting.set(d.conversation_id, (waiting.get(d.conversation_id) ?? 0) + 1);
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} retry={() => q.refetch()} />;
  const list = (q.data ?? []).filter((c) => !onlyHuman || c.status === "handoff");
  if (!q.data?.length) return <Empty>No conversations yet. They appear here as soon as someone messages this number.</Empty>;
  return (
    <div className="grid h-[calc(100vh-15rem)] min-h-[480px] gap-4 md:grid-cols-[320px_1fr]">
      <div className="flex min-h-0 flex-col rounded-xl border border-line bg-panel">
        <div className="flex gap-2 border-b border-line p-2">
          <button onClick={() => setOnlyHuman(false)} className={cx("flex-1 rounded-lg py-1.5 text-xs cursor-pointer", !onlyHuman ? "bg-raised text-amber" : "text-muted")}>All ({q.data.length})</button>
          <button onClick={() => setOnlyHuman(true)} className={cx("flex-1 rounded-lg py-1.5 text-xs cursor-pointer", onlyHuman ? "bg-raised text-amber" : "text-muted")}>Needs a human ({q.data.filter((c) => c.status === "handoff").length})</button>
        </div>
        <ul className="min-h-0 flex-1 divide-y divide-line overflow-y-auto">{list.map((c) => <Row key={c.id} c={c} drafts={waiting.get(c.id) ?? 0} active={sel === c.id} onClick={() => setSel(c.id)} />)}
          {list.length === 0 && <li className="p-6 text-center text-sm text-muted">Nobody is waiting for a human. 🎉</li>}</ul>
      </div>
      {sel ? <Thread key={sel} id={id} cid={sel} /> : <div className="hidden place-items-center rounded-xl border border-dashed border-line text-sm text-muted md:grid">Pick a conversation</div>}
    </div>
  );
}

function Row({ c, active, onClick, drafts }: { c: Conversation; active: boolean; onClick: () => void; drafts: number }) {
  return (
    <li><button onClick={onClick} className={cx("w-full px-3 py-3 text-left transition cursor-pointer", active ? "bg-raised" : "hover:bg-raised/60")}>
      <div className="flex items-center justify-between gap-2"><span className="truncate text-sm font-medium">{c.contact?.display_name || c.contact?.phone || "Unknown"}</span><span className="shrink-0 text-[11px] text-muted">{ago(c.last_inbound_at ?? c.updated_at)}</span></div>
      <div className="mt-1 flex items-center gap-1.5">{c.status === "handoff" ? <Badge tone="warn">needs human</Badge> : <StatusBadge status={c.status} />}{drafts > 0 && <Badge tone="warn">{drafts} to approve</Badge>}<span className="text-[11px] text-muted">{c.state.replace(/_/g, " ").toLowerCase()}</span></div>
    </button></li>
  );
}

const bubble: Record<Message["sender_type"], string> = {
  contact: "self-start bg-raised", bot: "self-end bg-amber/15 border border-amber/20", human: "self-end bg-ok/15 border border-ok/30", system: "self-center bg-transparent text-xs text-muted italic",
};

function Thread({ id, cid }: { id: string; cid: string }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const end = useRef<HTMLDivElement>(null);
  const q = useQuery({ queryKey: ["messages", id, cid], queryFn: () => api.messages(id, cid), refetchInterval: 4000 });
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [q.data?.messages.length]);
  const refresh = () => { qc.invalidateQueries({ queryKey: ["messages", id, cid] }); qc.invalidateQueries({ queryKey: ["conversations", id] }); qc.invalidateQueries({ queryKey: ["stats", id] }); };
  const send = useMutation({ mutationFn: () => api.sendHuman(id, cid, text.trim()), onSuccess: () => { setText(""); toast("ok", "Sent. The assistant is paused for this customer."); refresh(); }, onError: (e) => toast("bad", errMsg(e)) });
  const toggle = useMutation({ mutationFn: (a: "pause" | "resume") => api.pauseResume(id, cid, a), onSuccess: (_, a) => { toast("ok", a === "pause" ? "Assistant paused for this customer." : "Assistant is answering again."); refresh(); }, onError: (e) => toast("bad", errMsg(e)) });
  if (q.isLoading) return <div className="rounded-xl border border-line bg-panel p-4"><Loading /></div>;
  if (q.error) return <ErrorBox error={q.error} />;
  const { contact, messages } = q.data!;
  const paused = !!contact?.ai_paused;
  return (
    <div className="flex min-h-0 flex-col rounded-xl border border-line bg-panel">
      <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div><div className="font-medium">{contact?.display_name || contact?.phone}</div><div className="text-xs text-muted">{contact?.phone}{Object.keys(contact?.slots ?? {}).length > 0 && ` · ${Object.entries(contact!.slots).map(([k, v]) => `${k}: ${String(v)}`).join(" · ")}`}</div></div>
        {paused ? <Button size="sm" variant="primary" busy={toggle.isPending} onClick={() => toggle.mutate("resume")}>Hand back to assistant</Button>
                : <Button size="sm" busy={toggle.isPending} onClick={() => toggle.mutate("pause")}>Pause assistant</Button>}
      </header>
      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-4">
        {messages.map((m) => (
          <div key={m.id} className={cx("max-w-[78%] whitespace-pre-wrap rounded-2xl px-3.5 py-2 text-sm", bubble[m.sender_type])} title={when(m.created_at)}>
            {m.content_type !== "text" && <div className="mb-0.5 text-[10px] uppercase text-muted">{m.content_type}</div>}{m.content}
            {m.sender_type !== "system" && <div className="mt-1 text-[10px] text-muted">{m.sender_type === "human" ? "you · " : m.sender_type === "bot" ? "assistant · " : ""}{ago(m.created_at)}</div>}
          </div>))}
        <div ref={end} />
      </div>
      <Drafts id={id} cid={cid} onDone={refresh} />
      <footer className="border-t border-line p-3">
        {!paused && <p className="mb-2 text-xs text-muted">Sending a message pauses the assistant for this customer so it can't talk over you.</p>}
        <div className="flex gap-2">
          <Textarea value={text} onChange={(e) => setText(e.target.value)} placeholder="Reply as the business…" className="min-h-[44px] flex-1"
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim()) send.mutate(); }} />
          <Button variant="primary" busy={send.isPending} disabled={!text.trim()} onClick={() => send.mutate()}>Send</Button>
        </div>
      </footer>
    </div>
  );
}

// Assist mode: replies the assistant wrote that wait for a person. Send as written, edit then send, or discard.
function Drafts({ id, cid, onDone }: { id: string; cid: string; onDone: () => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["drafts", id, cid], queryFn: () => api.drafts(id, cid), refetchInterval: 4000 });
  const [editing, setEditing] = useState<Record<string, string>>({});
  const done = () => { qc.invalidateQueries({ queryKey: ["drafts", id] }); onDone(); };
  const send = useMutation({ mutationFn: (d: Draft) => api.sendDraft(id, d.id, editing[d.id]), onSuccess: (r) => { toast("ok", r.edited ? "Sent with your changes." : "Sent."); done(); }, onError: (e) => toast("bad", errMsg(e)) });
  const drop = useMutation({ mutationFn: (d: Draft) => api.discardReply(id, d.id), onSuccess: () => { toast("ok", "Discarded."); done(); }, onError: (e) => toast("bad", errMsg(e)) });
  if (!q.data?.length) return null;
  return (
    <div className="space-y-2 border-t border-amber/40 bg-amber/5 p-3">
      <div className="text-xs font-medium text-amber">Waiting for your approval ({q.data.length})</div>
      {q.data.map((d) => (
        <div key={d.id} className="rounded-lg border border-line bg-panel p-2.5">
          {d.kind === "media" && <div className="mb-1 text-xs text-muted">{d.media_type ?? "image"}: {d.media_url}</div>}
          {editing[d.id] !== undefined
            ? <Textarea value={editing[d.id]} onChange={(e) => setEditing((s) => ({ ...s, [d.id]: e.target.value }))} className="min-h-[70px]" aria-label="Edit the reply" />
            : <div className="whitespace-pre-wrap text-sm">{d.content || <span className="text-muted">(no caption)</span>}</div>}
          <div className="mt-2 flex flex-wrap gap-2">
            <Button size="sm" variant="primary" busy={send.isPending && send.variables?.id === d.id} onClick={() => send.mutate(d)}>{editing[d.id] !== undefined ? "Send edited" : "Send"}</Button>
            {editing[d.id] === undefined && d.kind === "text" && <Button size="sm" onClick={() => setEditing((s) => ({ ...s, [d.id]: d.content }))}>Edit</Button>}
            <Button size="sm" variant="ghost" busy={drop.isPending && drop.variables?.id === d.id} onClick={() => drop.mutate(d)}>Discard</Button>
          </div>
        </div>))}
    </div>
  );
}
