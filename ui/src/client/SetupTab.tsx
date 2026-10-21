import { useEffect, useState, type ReactNode, useMemo, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, emptyConfig, type BusinessConfig, type Offering, type OptionGroup, type Report, type Slot, type TenantDetail, type Issue } from "../lib/api";
import { csv, errMsg, lines, normalizePhone } from "../lib/util";
import { SCHEMA_PROMPT, normalizeConfig, parseConfigText } from "../lib/configNormalize";
import { MediaField } from "./MediaField";
import { BulkMedia } from "./BulkMedia";
import { fixTarget, groupBySection, type SectionId, type SectionIssue } from "../lib/readiness";
import { Badge, Button, Card, Field, Input, Loading, Select, Textarea, Toggle, useToast } from "../components/ui";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
// Every config (from the server OR pasted) is rebuilt from defaults, so the form never meets a missing field.
const merge = (c: unknown): BusinessConfig => normalizeConfig(c).config;

export default function SetupTab({ id, detail, go }: { id: string; detail: TenantDetail; go: (tab: string) => void }) {
  const q = useQuery({ queryKey: ["config", id], queryFn: () => api.config(id) });
  if (q.isLoading) return <Loading />;
  return <Form key={q.data?.version ?? 0} go={go} id={id} initial={q.data ? merge(q.data.config) : emptyConfig()} version={q.data?.version ?? null} name={detail.tenant.name} />;
}

function Form({ id, initial, version, name, go }: { id: string; initial: BusinessConfig; version: number | null; name: string; go: (tab: string) => void }) {
  const toast = useToast();
  const qc = useQueryClient();
  const [cfg, setCfg] = useState<BusinessConfig>(() => ({ ...initial, identity: { ...initial.identity, name: initial.identity.name || name } }));
  const [phonesText, setPhonesText] = useState(initial.handoff.staff_phones.join("\n"));     // what the person TYPES; the saved config gets the normalised form
  const [report, setReport] = useState<Report | null>(null);
  const [showJson, setShowJson] = useState(false);
  const [json, setJson] = useState("");
  const [fixes, setFixes] = useState<string[]>([]);       // what the normalizer repaired in the last paste
  const set = (fn: (c: BusinessConfig) => void) => setCfg((c) => { const n = structuredClone(c); fn(n); return n; });
  const [bulk, setBulk] = useState(false);
  // Applies to the LATEST draft (functional update), so several uploads finishing at once can't overwrite each other.
  const addMedia = (i: number, urls: string[]) => set((c) => { const t = c.offerings[i]; if (t) t.media.push(...urls.filter((u) => !t.media.includes(u))); });
  const goal = cfg.sales_process.goal;

  // keep derived fields consistent with the chosen goal
  useEffect(() => {
    set((c) => { c.integrations.payments = c.sales_process.goal === "order" ? "paystack" : null; if (c.sales_process.goal === "consult_and_book") c.booking.enabled = true; });
  }, [goal]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── live readiness: re-check quietly while the person edits, so problems are highlighted where they are ──
  const checkSeq = useRef(0);
  useEffect(() => {
    const n = ++checkSeq.current;
    const t = setTimeout(() => { api.validate(id, cfg).then((r) => { if (n === checkSeq.current) setReport(r); }).catch(() => { /* silent: the buttons report errors */ }); }, 900);
    return () => clearTimeout(t);
  }, [cfg, id]);
  const bySection = useMemo(() => groupBySection(report), [report]);
  const sp = (sid: SectionId) => ({ id: sid, issues: bySection[sid] ?? [] });
  const jumpTo = (sid: SectionId) => {
    const el = document.getElementById(`sec-${sid}`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.classList.add("ring-2", "ring-amber");
    setTimeout(() => el.classList.remove("ring-2", "ring-amber"), 1800);
  };
  const fix = (i: Issue) => { const t = fixTarget(i); if (t.kind === "tab") go(t.tab); else jumpTo(t.section); };
  const blockingCount = report?.blocking.length ?? 0;

  const check = useMutation({ mutationFn: () => api.validate(id, cfg), onSuccess: setReport, onError: (e) => toast("bad", errMsg(e)) });
  const live = useMutation({
    mutationFn: () => api.activate(id, cfg),
    onSuccess: (r) => { setReport(r.report); toast("ok", `Live! Version ${r.version} is now answering customers.`); qc.invalidateQueries(); },
    onError: (e: any) => {
      const rep = e?.detail?.report as Report | undefined;
      if (rep) setReport(rep);
      toast("bad", e?.status === 503 ? `A service was unavailable, nothing was changed. Try again. (${errMsg(e)})` : rep ? "Not ready to go live yet. See what's missing below." : errMsg(e));
    },
  });

  return (
    <div className="space-y-5">
      <div className="sticky top-0 z-20 -mx-1 flex flex-wrap items-center justify-between gap-3 border-b border-line bg-ink/95 px-1 py-2 backdrop-blur">
        <p className="flex items-center gap-2 text-sm text-muted">{report && (report.ok ? <Badge tone="ok">ready</Badge> : <Badge tone="bad">{blockingCount} to fix</Badge>)}{version ? `Live version ${version}. Changes go live only when you press “Go live”.` : "Nothing is live yet for this client."}</p>
        <div className="flex gap-2">
          <Button size="sm" variant="ghost" onClick={() => { setJson(JSON.stringify(cfg, null, 2)); setShowJson((s) => !s); }}>{showJson ? "Hide" : "Advanced: JSON"}</Button>
          <Button busy={check.isPending} onClick={() => check.mutate()}>Check readiness</Button>
          <Button variant="primary" busy={live.isPending} onClick={() => live.mutate()}>Go live{version ? " (new version)" : ""}</Button>
        </div>
      </div>

      {report && <ReportCard report={report} onFix={fix} />}

      {showJson && (
        <Card title="Advanced: raw configuration" action={<div className="flex gap-2">
          <Button size="sm" variant="ghost" onClick={() => { navigator.clipboard.writeText(SCHEMA_PROMPT).then(() => toast("ok", "Schema prompt copied. Paste it into any AI, then describe the business."), () => toast("bad", "Could not copy. Your browser blocked clipboard access.")); }}>Copy schema prompt</Button>
          <Button size="sm" onClick={() => {
            const r = parseConfigText(json);
            if (!r.ok) { toast("bad", r.error); return; }
            const { config: m, fixes: fx } = r.value;
            setCfg(m); setFixes(fx); setPhonesText(m.handoff.staff_phones.join("\n")); setShowJson(false);
            toast("ok", fx.length ? `Applied, with ${fx.length} automatic fix${fx.length === 1 ? "" : "es"}. Review them below, then check readiness.` : "Applied. Review the form, then check readiness.");
          }}>Apply</Button></div>}>
          <Textarea value={json} onChange={(e) => setJson(e.target.value)} className="min-h-[320px] font-mono text-xs" spellCheck={false} />
        </Card>)}

      <BulkMedia tenantId={id} offerings={cfg.offerings} open={bulk} onClose={() => setBulk(false)} onApply={addMedia} />

      {fixes.length > 0 && (
        <Card title={`Fixed automatically (${fixes.length})`} action={<Button size="sm" variant="ghost" onClick={() => setFixes([])}>Dismiss</Button>}>
          <p className="mb-2 text-xs text-muted">The pasted configuration was slightly off. These repairs were made so it loads. Please check them.</p>
          <ul className="list-disc space-y-1 pl-5 text-sm">{fixes.map((f, k) => <li key={k}>{f}</li>)}</ul>
        </Card>)}

      <Section {...sp("business")} title="1 · The business">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Business name"><Input value={cfg.identity.name} onChange={(e) => set((c) => { c.identity.name = e.target.value; })} /></Field>
          <Field label="What kind of business?" hint="Just a description, e.g. “fast-food restaurant”."><Input value={cfg.identity.industry} onChange={(e) => set((c) => { c.identity.industry = e.target.value; })} /></Field>
          <Field label="Short description" className="md:col-span-2"><Textarea value={cfg.identity.description} onChange={(e) => set((c) => { c.identity.description = e.target.value; })} /></Field>
          <Field label="Website"><Input value={cfg.identity.website} onChange={(e) => set((c) => { c.identity.website = e.target.value; })} /></Field>
          <Field label="Address / location"><Input value={cfg.identity.address} onChange={(e) => set((c) => { c.identity.address = e.target.value; })} /></Field>
          <Field label="Languages" hint="Comma separated."><Input value={cfg.identity.languages.join(", ")} onChange={(e) => set((c) => { c.identity.languages = csv(e.target.value); })} /></Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Timezone"><Input value={cfg.identity.timezone} onChange={(e) => set((c) => { c.identity.timezone = e.target.value; })} /></Field>
            <Field label="Currency"><Input value={cfg.identity.currency} onChange={(e) => set((c) => { c.identity.currency = e.target.value.toUpperCase(); })} /></Field>
          </div>
        </div>
        <div className="mt-4"><KV label="Facts the assistant may quote" hint="Official links, handles, emails, bank details… only things you want it to repeat." value={cfg.identity.facts} onChange={(v) => set((c) => { c.identity.facts = v; })} /></div>
      </Section>

      <Section {...sp("goal")} title="2 · What the assistant should do">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Main goal" hint={goal === "order" ? "Needs a menu with prices and Paystack connected (Payments tab) before it can go live." : goal === "consult_and_book" ? "No payments needed. Customers ask questions and request a booking; your team confirms it." : "Pick one. This decides what has to be set up before going live."}>
            <Select value={goal ?? ""} onChange={(e) => set((c) => { c.sales_process.goal = (e.target.value || null) as BusinessConfig["sales_process"]["goal"]; })}>
              <option value="">Choose…</option><option value="order">Take orders and payments (food, products)</option><option value="consult_and_book">Answer questions and book visits / appointments</option></Select></Field>
          {goal === "consult_and_book" && <Field label="What are bookings called?" hint="e.g. inspection, appointment, consultation."><Input value={cfg.booking.subject_label} onChange={(e) => set((c) => { c.booking.subject_label = e.target.value; })} /></Field>}
          <Field label="Extra instructions for the sales process" className="md:col-span-2"><Textarea value={cfg.sales_process.notes} onChange={(e) => set((c) => { c.sales_process.notes = e.target.value; })} placeholder="e.g. Always mention the free delivery above ₦10,000." /></Field>
        </div>
      </Section>

      <Section {...sp("offer")} title={goal === "order" ? "3 · Menu / products" : "3 · What you offer"} action={<div className="flex gap-2"><Button size="sm" onClick={() => setBulk(true)} disabled={cfg.offerings.length === 0}>Bulk upload photos</Button><Button size="sm" onClick={() => set((c) => { c.offerings.push({ name: "", description: "", price_minor: null, price_note: "", category: "", url: "", media: [], details: {}, option_groups: [] }); })}>+ Add</Button></div>}>
        {cfg.offerings.length === 0 && <p className="text-sm text-muted">Nothing added yet.</p>}
        <div className="space-y-4">{cfg.offerings.map((o, i) => (
          <OfferingEditor key={i} o={o} order={goal === "order"} tenantId={id}
            onChange={(n) => set((c) => { c.offerings[i] = n; })} onRemove={() => set((c) => { c.offerings.splice(i, 1); })}
            onMediaAdd={(urls) => addMedia(i, urls)} onMediaRemove={(u) => set((c) => { const t = c.offerings[i]; if (t) t.media = t.media.filter((x) => x !== u); })} />))}</div>
      </Section>

      {goal === "consult_and_book" && (
        <Section {...sp("slots")} title="4 · Questions to ask every customer" action={<Button size="sm" onClick={() => set((c) => { c.qualification_slots.push({ name: "", label: "", type: "text", values: [], aliases: {}, required: true, ask: "" }); })}>+ Add question</Button>}>
          <p className="mb-3 text-sm text-muted">The assistant gathers these before booking, one at a time.</p>
          <div className="space-y-3">{cfg.qualification_slots.map((s, i) => (
            <SlotEditor key={i} s={s} onChange={(n) => set((c) => { c.qualification_slots[i] = n; })} onRemove={() => set((c) => { c.qualification_slots.splice(i, 1); })} />))}</div>
        </Section>)}

      <Section {...sp("faq")} title="5 · Common questions" action={<Button size="sm" onClick={() => set((c) => { c.faqs.push({ question: "", answer: "" }); })}>+ Add</Button>}>
        <div className="space-y-3">{cfg.faqs.map((f, i) => (
          <div key={i} className="grid gap-2 rounded-lg border border-line p-3 md:grid-cols-[1fr_1.5fr_auto]">
            <Input placeholder="Question" value={f.question} onChange={(e) => set((c) => { c.faqs[i].question = e.target.value; })} />
            <Input placeholder="Answer" value={f.answer} onChange={(e) => set((c) => { c.faqs[i].answer = e.target.value; })} />
            <Button size="sm" variant="ghost" onClick={() => set((c) => { c.faqs.splice(i, 1); })}>✕</Button></div>))}</div>
      </Section>

      <Section {...sp("hours")} title="6 · Opening hours">
        <Hours hours={cfg.hours} onChange={(h) => set((c) => { c.hours = h; })} />
      </Section>

      <Section {...sp("team")} title="7 · Your team (alerts and hand-over)">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Contact person's name" hint="Who customers are told will follow up."><Input value={cfg.handoff.contact_name} onChange={(e) => set((c) => { c.handoff.contact_name = e.target.value; })} /></Field>
          <Field label="Staff WhatsApp numbers" hint="One per line. Type it any normal way (08012345678 or +234 801 234 5678): it is saved as 2348012345678. They receive alerts and can reply “resume 080…”."><Textarea value={phonesText} onChange={(e) => { setPhonesText(e.target.value); set((c) => { c.handoff.staff_phones = lines(e.target.value).map(normalizePhone).filter(Boolean); }); }}
            onBlur={() => setPhonesText(lines(phonesText).map(normalizePhone).filter(Boolean).join("\n"))} /></Field>
          <Field label="Extra phrases that mean “I want a human”" hint="One per line. Common ones (“speak to a human”, “agent”) are built in." className="md:col-span-2"><Textarea value={cfg.handoff.human_request_patterns.join("\n")} onChange={(e) => set((c) => { c.handoff.human_request_patterns = lines(e.target.value); })} /></Field>
        </div>
      </Section>

      <Section {...sp("tone")} title="8 · Tone and limits">
        <div className="grid gap-4 md:grid-cols-2">
          <Field label="Tone" hint="How the assistant sounds. Example: warm, friendly and upbeat, a few emojis, short and clear."><Input value={cfg.communication.tone} onChange={(e) => set((c) => { c.communication.tone = e.target.value; })} /></Field>
          <Field label="Greeting" hint="Optional first message."><Input value={cfg.communication.greeting} onChange={(e) => set((c) => { c.communication.greeting = e.target.value; })} /></Field>
          <Field label="Style rules" hint="One per line, e.g. “Never use more than 2 emojis”."><Textarea value={cfg.communication.rules.join("\n")} onChange={(e) => set((c) => { c.communication.rules = lines(e.target.value); })} /></Field>
          <Field label="Never say" hint="Words/phrases to avoid, one per line."><Textarea value={cfg.restrictions.never_say.join("\n")} onChange={(e) => set((c) => { c.restrictions.never_say = lines(e.target.value); })} /></Field>
          <Field label="Claims it must never make" hint="e.g. “guaranteed returns”, “we deliver nationwide”." className="md:col-span-2"><Textarea value={cfg.restrictions.prohibited_claims.join("\n")} onChange={(e) => set((c) => { c.restrictions.prohibited_claims = lines(e.target.value); })} /></Field>
        </div>
      </Section>

      <div className="flex justify-end gap-2 border-t border-line pt-4">
        <Button busy={check.isPending} onClick={() => check.mutate()}>Check readiness</Button>
        <Button variant="primary" busy={live.isPending} onClick={() => live.mutate()}>Go live{version ? " (new version)" : ""}</Button>
      </div>
    </div>
  );
}

function Section({ id, title, children, action, issues = [] }: { id: SectionId; title: ReactNode; children: ReactNode; action?: ReactNode; issues?: SectionIssue[] }) {
  const blocking = issues.filter((x) => x.level === "blocking");
  const badge = blocking.length ? <Badge tone="bad">{blocking.length} to fix</Badge> : issues.length ? <Badge tone="warn">{issues.length} suggestion{issues.length === 1 ? "" : "s"}</Badge> : null;
  return (
    <div id={`sec-${id}`} className={`scroll-mt-20 rounded-xl transition ${blocking.length ? "ring-1 ring-bad/60" : issues.length ? "ring-1 ring-warn/40" : ""}`}>
      <Card title={<span className="flex items-center gap-2">{title}{badge}</span>} action={action}>
        {issues.length > 0 && (
          <ul className="mb-4 space-y-1 rounded-lg border border-line bg-raised/50 p-3 text-sm">
            {issues.map((x, k) => <li key={k} className={x.level === "blocking" ? "text-bad" : "text-warn"}>{x.level === "blocking" ? "⚠ " : "💡 "}{x.issue.message}</li>)}
          </ul>)}
        {children}
      </Card>
    </div>
  );
}

function ReportCard({ report, onFix }: { report: Report; onFix: (i: Issue) => void }) {
  const row = (i: Issue, tone: string) => {
    const t = fixTarget(i);
    return (
      <li key={`${i.code}-${i.message}`} className="flex flex-wrap items-center justify-between gap-2">
        <span className={tone}>• {i.message}</span>
        <Button size="sm" onClick={() => onFix(i)}>{t.kind === "tab" ? t.label : "Fix this"} →</Button>
      </li>
    );
  };
  return (
    <Card title={report.ok ? "Ready to go live ✓" : "Not ready yet"} action={<span className="text-xs text-muted">{Math.round(report.completeness * 100)}% complete</span>}>
      <div className="mb-3 h-1.5 overflow-hidden rounded-full bg-line"><div className="h-full bg-amber transition-all" style={{ width: `${report.completeness * 100}%` }} /></div>
      {report.blocking.length > 0 && <><div className="mb-1 text-xs font-medium uppercase tracking-wider text-bad">Must fix before going live</div><ul className="mb-3 space-y-2 text-sm">{report.blocking.map((i) => row(i, "text-bad"))}</ul></>}
      {report.warnings.length > 0 && <><div className="mb-1 text-xs font-medium uppercase tracking-wider text-warn">Worth fixing</div><ul className="space-y-2 text-sm">{report.warnings.map((i) => row(i, "text-muted"))}</ul></>}
      {report.ok && report.warnings.length === 0 && <p className="text-sm text-ok">Everything checks out.</p>}
    </Card>
  );
}

function KV({ label, hint, value, onChange }: { label: string; hint?: string; value: Record<string, string>; onChange: (v: Record<string, string>) => void }) {
  const rows = Object.entries(value);
  const update = (next: [string, string][]) => onChange(Object.fromEntries(next));
  return (
    <div>
      <div className="mb-1 flex items-center justify-between"><span className="text-xs font-medium text-muted">{label}</span><Button size="sm" variant="ghost" onClick={() => update([...rows, [`item${rows.length + 1}`, ""]])}>+ Add</Button></div>
      {hint && <div className="mb-2 text-xs text-muted/80">{hint}</div>}
      <div className="space-y-2">{rows.map(([k, v], i) => (
        <div key={i} className="grid grid-cols-[1fr_2fr_auto] gap-2">
          <Input value={k} onChange={(e) => update(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} placeholder="Name" />
          <Input value={v} onChange={(e) => update(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} placeholder="Value" />
          <Button size="sm" variant="ghost" onClick={() => update(rows.filter((_, j) => j !== i))}>✕</Button></div>))}</div>
    </div>
  );
}

function OfferingEditor({ o, order, tenantId, onChange, onRemove, onMediaAdd, onMediaRemove }: { o: Offering; order: boolean; tenantId: string; onChange: (o: Offering) => void; onRemove: () => void; onMediaAdd: (urls: string[]) => void; onMediaRemove: (url: string) => void }) {
  const upd = (fn: (x: Offering) => void) => { const n = structuredClone(o); fn(n); onChange(n); };
  return (
    <div className="rounded-lg border border-line p-4">
      <div className="grid gap-3 md:grid-cols-4">
        <Field label="Name" className="md:col-span-2"><Input value={o.name} onChange={(e) => upd((x) => { x.name = e.target.value; })} /></Field>
        <Field label="Price (₦)" hint={order ? "" : "Leave empty if it varies."}><Input type="number" min={0} step="0.01" value={o.price_minor == null ? "" : o.price_minor / 100}
          onChange={(e) => upd((x) => { x.price_minor = e.target.value === "" ? null : Math.round(parseFloat(e.target.value) * 100); })} /></Field>
        <Field label="Price note" hint="e.g. “from”, “on request”"><Input value={o.price_note} onChange={(e) => upd((x) => { x.price_note = e.target.value; })} /></Field>
        <Field label="Description" className="md:col-span-4"><Textarea value={o.description} onChange={(e) => upd((x) => { x.description = e.target.value; })} className="min-h-[56px]" /></Field>
        <Field label="Category" hint={order ? "Groups the menu into sections, e.g. Rice, Drinks." : ""}><Input value={o.category} onChange={(e) => upd((x) => { x.category = e.target.value; })} /></Field>
        <Field label="Link" className="md:col-span-2"><Input value={o.url} onChange={(e) => upd((x) => { x.url = e.target.value; })} /></Field>
        <Field label="Photos and videos" className="md:col-span-4"><MediaField tenantId={tenantId} urls={o.media} onAdd={onMediaAdd} onRemove={onMediaRemove} /></Field>
      </div>
      <div className="mt-3"><KV label="Details" hint="Facts shown to customers: size, location, title…" value={o.details} onChange={(v) => upd((x) => { x.details = v; })} /></div>
      {order && (
        <div className="mt-4">
          <div className="mb-2 flex items-center justify-between"><span className="text-xs font-medium text-muted">Choices (size, toppings, extras…)</span>
            <Button size="sm" variant="ghost" onClick={() => upd((x) => { x.option_groups.push({ name: "", selection: "single", required: false, max_included: null, options: [] }); })}>+ Add group</Button></div>
          <div className="space-y-3">{o.option_groups.map((g, gi) => <GroupEditor key={gi} g={g} onChange={(n) => upd((x) => { x.option_groups[gi] = n; })} onRemove={() => upd((x) => { x.option_groups.splice(gi, 1); })} />)}</div>
        </div>)}
      <div className="mt-3 text-right"><Button size="sm" variant="danger" onClick={() => confirm("Remove this item?") && onRemove()}>Remove item</Button></div>
    </div>
  );
}

function GroupEditor({ g, onChange, onRemove }: { g: OptionGroup; onChange: (g: OptionGroup) => void; onRemove: () => void }) {
  const upd = (fn: (x: OptionGroup) => void) => { const n = structuredClone(g); fn(n); onChange(n); };
  return (
    <div className="rounded-lg bg-ink p-3">
      <div className="grid gap-2 md:grid-cols-[2fr_1fr_auto_auto]">
        <Input placeholder="Group name (e.g. Size)" value={g.name} onChange={(e) => upd((x) => { x.name = e.target.value; })} />
        <Select value={g.selection} onChange={(e) => upd((x) => { x.selection = e.target.value as OptionGroup["selection"]; })}><option value="single">Pick one</option><option value="multiple">Pick several</option></Select>
        <Toggle checked={g.required} onChange={(v) => upd((x) => { x.required = v; })} label="Required" />
        <Button size="sm" variant="ghost" onClick={onRemove}>✕</Button>
      </div>
      <div className="mt-2 space-y-1.5">{g.options.map((op, oi) => (
        <div key={oi} className="grid grid-cols-[2fr_1fr_auto] gap-2">
          <Input placeholder="Option" value={op.name} onChange={(e) => upd((x) => { x.options[oi].name = e.target.value; })} />
          <Input type="number" min={0} step="0.01" placeholder="Extra ₦" value={op.price_minor / 100} onChange={(e) => upd((x) => { x.options[oi].price_minor = Math.round((parseFloat(e.target.value) || 0) * 100); })} />
          <Button size="sm" variant="ghost" onClick={() => upd((x) => { x.options.splice(oi, 1); })}>✕</Button></div>))}
        <Button size="sm" variant="ghost" onClick={() => upd((x) => { x.options.push({ name: "", price_minor: 0 }); })}>+ Add option</Button></div>
    </div>
  );
}

function SlotEditor({ s, onChange, onRemove }: { s: Slot; onChange: (s: Slot) => void; onRemove: () => void }) {
  const upd = (fn: (x: Slot) => void) => { const n = structuredClone(s); fn(n); onChange(n); };
  return (
    <div className="grid gap-2 rounded-lg border border-line p-3 md:grid-cols-6">
      <Field label="Internal name" hint="no spaces, e.g. budget"><Input value={s.name} onChange={(e) => upd((x) => { x.name = e.target.value.replace(/\s+/g, "_").toLowerCase(); })} /></Field>
      <Field label="Label"><Input value={s.label} onChange={(e) => upd((x) => { x.label = e.target.value; })} /></Field>
      <Field label="Type"><Select value={s.type} onChange={(e) => upd((x) => { x.type = e.target.value as Slot["type"]; })}><option value="text">Text</option><option value="number">Number</option><option value="money">Money</option><option value="enum">Choice</option><option value="bool">Yes / no</option></Select></Field>
      <Field label="Choices" hint="Comma separated (for “Choice”)"><Input value={s.values.join(", ")} disabled={s.type !== "enum"} onChange={(e) => upd((x) => { x.values = csv(e.target.value); })} /></Field>
      <Field label="How to ask it"><Input value={s.ask} onChange={(e) => upd((x) => { x.ask = e.target.value; })} /></Field>
      <div className="flex items-end justify-between gap-2"><Toggle checked={s.required} onChange={(v) => upd((x) => { x.required = v; })} label="Required" /><Button size="sm" variant="ghost" onClick={onRemove}>✕</Button></div>
    </div>
  );
}

function Hours({ hours, onChange }: { hours: Record<string, string[] | null>; onChange: (h: Record<string, string[] | null>) => void }) {
  const def = hours.default ?? ["09:00", "18:00"];
  const upd = (fn: (h: Record<string, string[] | null>) => void) => { const n = { ...hours }; fn(n); onChange(n); };
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Opens"><Input type="time" value={def[0]} onChange={(e) => upd((h) => { h.default = [e.target.value, def[1]]; })} /></Field>
        <Field label="Closes"><Input type="time" value={def[1]} onChange={(e) => upd((h) => { h.default = [def[0], e.target.value]; })} /></Field>
      </div>
      <div><div className="mb-2 text-xs font-medium text-muted">Closed on</div>
        <div className="flex flex-wrap gap-2">{DAYS.map((d, i) => {
          const closed = String(i) in hours && hours[String(i)] === null;
          return <button key={d} type="button" onClick={() => upd((h) => { if (closed) delete h[String(i)]; else h[String(i)] = null; })}
            className={`rounded-lg border px-3 py-1.5 text-sm cursor-pointer ${closed ? "border-bad/40 bg-bad/10 text-bad" : "border-line text-muted hover:text-text"}`}>{d}</button>;
        })}</div></div>
    </div>
  );
}
