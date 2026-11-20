// One chat experience for the dashboard Test chat and the public demo page: a phone-style conversation with the REAL assistant on the
// left, and "behind the scenes" on the right (what it captured, what the owner's phone receives, each step it took).
import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import type { ChatActivity, ChatHistoryItem, ChatState, ChatTurn } from "../lib/api";
import { cx, errMsg } from "../lib/util";
import { Button, Spinner } from "./ui";

type Line = { from: "customer" | "assistant" | "team" | "note"; text: string; url?: string | null };

const TOOL_WORDS: Record<string, string> = {
  add_product: "Added an item to the order", remove_item: "Removed an item", set_quantity: "Changed a quantity", set_fulfillment: "Saved pickup or delivery",
  checkout: "Created the payment link", show_menu: "Sent the menu", show_item: "Showed an item with its photos", share_item: "Shared a matching listing",
  record_slots: "Noted the customer's details", request_booking: "Sent a booking request to the team", request_human: "Brought in a person",
};
const nice = (s?: string | null) => (s ?? "").replace(/_/g, " ").toLowerCase();

export function describe(a: ChatActivity): string | null {
  switch (a.event) {
    case "TURN_COMPLETED": {
      const t = (a.tools ?? []).map((x) => TOOL_WORDS[x] ?? nice(x));
      const ms = a.timings?.total_ms;
      return [t.length ? t.join(" · ") : "Answered from what it knows", ms ? `replied in ${(ms / 1000).toFixed(1)} s` : ""].filter(Boolean).join(" — ");
    }
    case "STATE_TRANSITION": return `Moved the conversation from ${nice(a.from_state)} to ${nice(a.to_state)}`;
    case "ESCALATED": return `Handed the chat to a person (${nice(a.reason)})`;
    case "BOOKING_REQUESTED": return "Saved a booking request for the team to confirm";
    case "ORDER_PAID": return "Payment confirmed and the order marked paid";
    case "REPLY_LEAK_BLOCKED": return "Stopped a reply that looked wrong and asked again";
    case "LLM_UNKNOWN_TOOL": return "Ignored an action that doesn't exist";
    case "RATE_LIMITED": return "Slowed down: too many messages in a minute";
    case "DAILY_REPLY_CAP_REACHED": return "Daily limit for this chat reached: handed to a person";
    case "GREETING_REPLY_SUPPRESSED": return "Sent the business's own welcome";
    default: return null;
  }
}

// WhatsApp-style *bold*, and links shown as text (a test payment link is never a real page)
function Wa({ text }: { text: string }) {
  return <>{text.split(/(\*[^*\n]+\*)/g).map((part, i) => part.startsWith("*") && part.endsWith("*") && part.length > 2 ? <b key={i}>{part.slice(1, -1)}</b> : <Fragment key={i}>{part}</Fragment>)}</>;
}

export function money(minor: number, currency = "NGN") {
  const sym: Record<string, string> = { NGN: "₦", USD: "$", GBP: "£", GHS: "GH₵" };
  return `${sym[currency] ?? currency + " "}${(minor / 100).toLocaleString(undefined, { maximumFractionDigits: minor % 100 ? 2 : 0 })}`;
}

export interface ChatPanelProps {
  title: string;
  subtitle?: string;
  suggestions?: string[];
  currency?: string;
  send: (text: string) => Promise<ChatTurn>;
  pay: () => Promise<ChatTurn>;
  history?: () => Promise<{ messages: ChatHistoryItem[]; state: ChatState }>;
  onNewChat?: () => void;
  backstage?: boolean;                 // show the "behind the scenes" panel
  footer?: ReactNode;
  disabled?: string | null;            // a reason the chat can't be used yet
}

export default function ChatPanel({ title, subtitle, suggestions = [], currency = "NGN", send, pay, history, onNewChat, backstage = true, footer, disabled }: ChatPanelProps) {
  const [lines, setLines] = useState<Line[]>([]);
  const [state, setState] = useState<ChatState | null>(null);
  const [alerts, setAlerts] = useState<string[]>([]);
  const [activity, setActivity] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let live = true;
    if (!history) return;
    history().then((h) => {
      if (!live) return;
      setLines(h.messages.map((m) => ({ from: m.from, text: m.text, url: m.url })));
      setState(h.state);
    }).catch(() => { /* a fresh chat */ });
    return () => { live = false; };
  }, [history]);
  useEffect(() => { end.current?.scrollIntoView({ block: "end", behavior: "smooth" }); }, [lines.length, busy]);

  function absorb(r: ChatTurn) {
    setLines((l) => [...l, ...r.replies.map((x): Line => ({ from: "assistant", text: x.text, url: x.url }))]);
    if (r.status === "handoff" || r.status === "paused") setLines((l) => [...l, { from: "note", text: "The assistant is paused for this chat. A person on the team takes it from here." }]);
    setState(r.state);
    if (r.owner_alerts.length) setAlerts((a) => [...r.owner_alerts, ...a].slice(0, 6));
    const steps = r.activity.map(describe).filter((x): x is string => !!x);
    if (steps.length) setActivity((a) => [...steps.reverse(), ...a].slice(0, 30));
  }

  async function go(t: string) {
    const msg = t.trim();
    if (!msg || busy) return;
    setErr(""); setText(""); setBusy(true);
    setLines((l) => [...l, { from: "customer", text: msg }]);
    try { absorb(await send(msg)); } catch (e) { setErr(errMsg(e)); } finally { setBusy(false); }
  }
  async function doPay() {
    setErr(""); setBusy(true);
    setLines((l) => [...l, { from: "note", text: "The customer paid on the payment page (test payment)." }]);
    try { absorb(await pay()); } catch (e) { setErr(errMsg(e)); } finally { setBusy(false); }
  }
  function reset() { setLines([]); setState(null); setAlerts([]); setActivity([]); setErr(""); onNewChat?.(); }

  const slots = Object.entries(state?.slots ?? {}).filter(([, v]) => v !== null && v !== "" && v !== undefined);
  return (
    <div className={cx("grid gap-4", backstage && "lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]")}>
      <section className="flex h-[640px] max-h-[80vh] min-h-[460px] flex-col overflow-hidden rounded-2xl border border-line bg-panel" aria-label="Chat">
        <header className="flex items-center gap-3 border-b border-line bg-raised px-4 py-3">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-full bg-amber font-display text-sm font-bold text-on-amber">{(title || "?").slice(0, 1)}</div>
          <div className="min-w-0 flex-1"><div className="truncate font-medium">{title}</div><div className="truncate text-xs text-muted">{subtitle ?? "replies instantly"}</div></div>
          {onNewChat && <Button size="sm" variant="ghost" onClick={reset} disabled={busy}>New chat</Button>}
        </header>
        <div className="flex min-h-0 flex-1 flex-col gap-1.5 overflow-y-auto bg-chat p-3" aria-live="polite">
          {lines.length === 0 && !disabled && <p className="m-auto max-w-[260px] text-center text-sm text-muted">Say hello as a customer would. These are real AI replies; nothing is sent on WhatsApp.</p>}
          {disabled && <p className="m-auto max-w-[280px] text-center text-sm text-muted">{disabled}</p>}
          {lines.map((l, i) => l.from === "note"
            ? <div key={i} className="self-center rounded-lg bg-raised px-3 py-1 text-center text-[11.5px] text-muted">{l.text}</div>
            : <div key={i} className={cx("max-w-[85%] whitespace-pre-wrap break-words rounded-xl px-3 py-2 text-[14.5px] leading-snug",
                l.from === "customer" ? "self-end rounded-tr-sm bg-bubble-out" : "self-start rounded-tl-sm bg-panel", l.from === "team" && "border border-dashed border-amber")}>
                {l.url && /\.(jpe?g|png|webp|gif)(\?|$)/i.test(l.url) && <img src={l.url} alt="" className="mb-1 max-h-48 rounded-lg object-cover" loading="lazy" />}
                {l.url && /\.(pdf|docx?|xlsx?|pptx?|csv|txt|epub|zip)(\?|$)/i.test(l.url) && <div className="mb-1 text-xs text-muted">📄 {l.url.split("?")[0].split("/").pop()}</div>}
                {l.url && /\.(mp4|mov|3gp)(\?|$)/i.test(l.url) && <div className="mb-1 text-xs text-muted">▶ video</div>}
                {l.from === "team" && <div className="mb-0.5 text-[10px] uppercase tracking-wider text-amber">team</div>}
                <Wa text={l.text} />
              </div>)}
          {busy && <div className="self-start rounded-xl bg-panel px-3 py-2.5"><Spinner className="h-4 w-4 text-muted" /></div>}
          <div ref={end} />
        </div>
        <footer className="space-y-2 border-t border-line bg-raised p-2.5">
          {state?.payment_due && <Button variant="primary" className="w-full" busy={busy} onClick={doPay}>Pay now (test payment, no money moves)</Button>}
          {suggestions.length > 0 && lines.length < 2 && !disabled && (
            <div className="flex flex-wrap gap-1.5">{suggestions.map((s) => <button key={s} type="button" disabled={busy} onClick={() => go(s)}
              className="cursor-pointer rounded-full border border-line bg-panel px-3 py-1 text-xs text-text hover:border-amber disabled:opacity-50">{s}</button>)}</div>)}
          {err && <div role="alert" className="rounded-lg border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad">{err}</div>}
          <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); go(text); }}>
            <label htmlFor="chat-input" className="sr-only">Message</label>
            <input id="chat-input" value={text} onChange={(e) => setText(e.target.value)} placeholder="Type a message" maxLength={500} disabled={busy || !!disabled} autoComplete="off"
              className="min-w-0 flex-1 rounded-full border border-line bg-panel px-4 py-2 text-sm text-text placeholder:text-muted/70 focus:border-amber focus:outline-none" />
            <Button type="submit" variant="primary" disabled={!text.trim() || busy || !!disabled} className="rounded-full">Send</Button>
          </form>
          {footer}
        </footer>
      </section>

      {backstage && (
        <section className="grid content-start gap-4" aria-label="Behind the scenes">
          <div className="rounded-xl border border-line bg-panel p-4">
            <div className="mb-3 flex items-center justify-between gap-2"><h3 className="text-xs font-semibold uppercase tracking-wider text-muted">What Hooze captured</h3>
              {state?.status && <span className={cx("rounded-full border px-2 py-0.5 text-[11px]", state.status === "handoff" ? "border-bad/30 bg-bad/10 text-bad" : "border-ok/30 bg-ok/10 text-ok")}>{state.status === "handoff" ? "with a person" : nice(state.stage) || "active"}</span>}</div>
            {!slots.length && !state?.order && !state?.booking && <p className="text-sm text-muted">Details the customer gives (budget, area, items, a time) appear here as the chat goes.</p>}
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
              {slots.map(([k, v]) => <Fragment key={k}><dt className="text-muted">{nice(k)}</dt><dd className="break-words font-medium">{String(v)}</dd></Fragment>)}
              {state?.order && state.order.items.length > 0 && <>
                <dt className="text-muted">order</dt>
                <dd className="font-medium">{state.order.items.map((it, i) => <div key={i}>{it.quantity}× {it.name}{it.options.length ? ` (${it.options.join(", ")})` : ""} · {money(it.line_total_minor, currency)}</div>)}</dd>
                <dt className="text-muted">total</dt><dd className="font-medium tabular-nums">{money(state.order.subtotal_minor, currency)}</dd>
                <dt className="text-muted">status</dt><dd className="font-medium">{nice(state.order.status)}{state.order.fulfillment ? ` · ${nice(state.order.fulfillment)}` : ""}</dd>
                {state.order.address && <><dt className="text-muted">address</dt><dd className="font-medium">{state.order.address}</dd></>}
              </>}
              {state?.booking && <><dt className="text-muted">booking</dt><dd className="font-medium">{state.booking.subject}{state.booking.when ? ` · ${state.booking.when}` : ""} · {nice(state.booking.status)}</dd></>}
            </dl>
          </div>
          <div className="rounded-xl border border-line bg-panel p-4">
            <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted">Message to the owner's phone</h3>
            {alerts.length === 0 ? <p className="text-sm text-muted">When a sale, booking or hand-off needs the team, the WhatsApp alert they'd get shows here (in a test, nobody is texted).</p>
              : <div className="space-y-2">{alerts.map((a, i) => <div key={i} className={cx("whitespace-pre-wrap rounded-lg border-l-2 border-amber bg-raised px-3 py-2 text-sm", i > 0 && "opacity-60")}>{a}</div>)}</div>}
          </div>
          <div className="rounded-xl border border-line bg-panel p-4">
            <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted">What Hooze did</h3>
            {activity.length === 0 ? <p className="text-sm text-muted">Each step appears here: what it looked up, what it saved, who it told.</p>
              : <ol className="space-y-1.5 text-sm">{activity.map((a, i) => <li key={i} className={cx("flex gap-2", i > 3 && "text-muted")}><span className="text-amber">•</span><span>{a}</span></li>)}</ol>}
          </div>
        </section>
      )}
    </div>
  );
}
