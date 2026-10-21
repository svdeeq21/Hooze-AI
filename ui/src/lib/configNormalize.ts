/**
 * Config normalizer (TypeScript twin of hooze/config_engine/normalize.py — keep the two in sync).
 *
 * Takes ANY parsed JSON (hand-written, AI-generated, older version, half-shaped) and rebuilds a complete BusinessConfig
 * from defaults, so the Setup form can never receive an object missing a field it calls `.join()` / `.map()` on.
 * Every repair is returned in `fixes` so the person sees exactly what changed. It never invents business facts:
 * missing prices, phones and `sales_process.goal` stay missing for the readiness check to flag.
 */
import type { BusinessConfig, Offering, OptionGroup, Slot } from "./api";

type Obj = Record<string, unknown>;
const SLOT_TYPES = ["text", "number", "money", "enum", "bool"] as const;
const KNOWN_TOP = ["identity", "offerings", "faqs", "qualification_slots", "stuck_after_user_messages", "sales_process", "booking", "hours", "handoff", "communication", "restrictions", "integrations"];

const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);
const obj = (v: unknown): Obj => (isObj(v) ? v : {});
const str = (v: unknown): string => {
  if (v == null || typeof v === "object") return "";
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v).trim();
};
const strList = (v: unknown): string[] => {
  if (v == null) return [];
  if (typeof v === "string") return v.split("\n").map((x) => x.trim()).filter(Boolean);
  if (Array.isArray(v)) return v.map(str).filter(Boolean);
  return [];
};
const snake = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
const title = (s: string) => { const t = snake(s).replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1); };
const num = (v: unknown): number | null => {
  if (v == null || typeof v === "boolean") return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  const n = parseFloat(String(v).replace(/[,₦\s]/g, ""));
  return Number.isFinite(n) ? n : null;
};
const posInt = (v: unknown, d: number) => { const n = num(v); return n != null && n > 0 ? Math.round(n) : d; };

export interface Normalized { config: BusinessConfig; fixes: string[] }

export function normalizeConfig(raw: unknown): Normalized {
  const fixes: string[] = [];
  const r = obj(raw);
  for (const k of Object.keys(r)) if (!KNOWN_TOP.includes(k)) fixes.push(`Ignored unknown field "${k}".`);

  // identity
  const i = obj(r.identity);
  const facts: Record<string, string> = {};
  if (isObj(i.facts)) {
    for (const [k, v] of Object.entries(i.facts)) {
      const t = Array.isArray(v) ? v.map(str).filter(Boolean).join(", ") : str(v);
      if (t) { facts[k] = t; if (Array.isArray(v)) fixes.push(`identity.facts.${k} was a list; joined into one text value.`); }
      else fixes.push(`identity.facts.${k} was empty or not text, so it was dropped.`);
    }
  } else if (i.facts != null && i.facts !== "") fixes.push("identity.facts must be an object of text values; it was reset to empty.");
  const identity = {
    name: str(i.name), industry: str(i.industry), description: str(i.description), website: str(i.website), address: str(i.address),
    languages: strList(i.languages).length ? strList(i.languages) : ["English"], timezone: str(i.timezone) || "Africa/Lagos", currency: str(i.currency) || "NGN", facts,
  };

  // offerings
  const offerings: Offering[] = [];
  (Array.isArray(r.offerings) ? r.offerings : []).forEach((item, n) => {
    let o: Obj;
    if (typeof item === "string") { o = { name: item }; fixes.push(`offerings[${n}] was plain text; turned into an offering named "${item}".`); }
    else o = obj(item);
    if (!str(o.name)) { fixes.push(`offerings[${n}] had no name, so it was dropped.`); return; }
    let pm = num(o.price_minor);
    if (pm == null && o.price != null) {
      const p = num(o.price);
      if (p != null) { pm = Math.round(p * 100); fixes.push(`offerings[${n}] used "price" (${p}); read it as naira and stored ${pm} in kobo (price_minor).`); }
    }
    const groups: OptionGroup[] = [];
    (Array.isArray(o.option_groups) ? o.option_groups : []).forEach((gr, gi) => {
      const g = obj(gr);
      if (!str(g.name)) { fixes.push(`offerings[${n}].option_groups[${gi}] had no name, so it was dropped.`); return; }
      const options = (Array.isArray(g.options) ? g.options : []).flatMap((op) =>
        typeof op === "string" ? [{ name: op.trim(), price_minor: 0 }] : isObj(op) && str(op.name) ? [{ name: str(op.name), price_minor: Math.round(num(op.price_minor) ?? 0) }] : []);
      const mi = num(g.max_included);
      groups.push({ name: str(g.name), selection: g.selection === "multiple" ? "multiple" : "single", required: !!g.required, max_included: mi != null ? Math.round(mi) : null, options });
    });
    const details: Record<string, string> = {};
    for (const [k, v] of Object.entries(obj(o.details))) if (str(v)) details[k] = str(v);
    offerings.push({ name: str(o.name), description: str(o.description), price_minor: pm != null ? Math.round(pm) : null, price_note: str(o.price_note), category: str(o.category), url: str(o.url), media: strList(o.media), details, option_groups: groups });
  });

  // faqs
  const faqs: { question: string; answer: string }[] = [];
  (Array.isArray(r.faqs) ? r.faqs : []).forEach((f, n) => {
    const x = obj(f); const q = str(x.question ?? x.q); const a = str(x.answer ?? x.a);
    if (q && a) faqs.push({ question: q, answer: a }); else fixes.push(`faqs[${n}] needs both a question and an answer, so it was dropped.`);
  });

  // qualification slots
  const slots: Slot[] = [];
  const seen = new Set<string>();
  (Array.isArray(r.qualification_slots) ? r.qualification_slots : []).forEach((item, n) => {
    let s: Obj;
    if (typeof item === "string") {
      s = { name: snake(item), label: title(item), required: false };
      fixes.push(`qualification_slots[${n}] was plain text ("${snake(item)}"); turned into an optional text question. Edit it to add the question wording.`);
    } else s = obj(item);
    const name = snake(str(s.name));
    if (!name || seen.has(name)) { fixes.push(`qualification_slots[${n}] had no usable unique name, so it was dropped.`); return; }
    seen.add(name);
    let type = (SLOT_TYPES as readonly unknown[]).includes(s.type) ? (s.type as Slot["type"]) : "text";
    const values = strList(s.values);
    if (type === "enum" && !values.length) { type = "text"; fixes.push(`qualification_slots[${n}] (${name}) was a choice with no choices; changed to free text.`); }
    const aliases: Record<string, string[]> = {};
    for (const [k, v] of Object.entries(obj(s.aliases))) aliases[k] = strList(v);
    slots.push({ name, label: str(s.label) || title(name), type, values, aliases, required: s.required !== false, ask: str(s.ask) });
  });

  // the small sections
  const sp = obj(r.sales_process);
  let goal: BusinessConfig["sales_process"]["goal"] = null;
  if (sp.goal === "order" || sp.goal === "consult_and_book") goal = sp.goal;
  else if (sp.goal != null && sp.goal !== "") fixes.push(`sales_process.goal "${str(sp.goal)}" is not valid (use "order" or "consult_and_book"); it was cleared.`);
  const b = obj(r.booking), h = obj(r.handoff), c = obj(r.communication), rs = obj(r.restrictions), ig = obj(r.integrations);

  let hoursRaw: unknown = r.hours;
  const hours: Record<string, string[] | null> = {};
  if (Array.isArray(hoursRaw) && hoursRaw.length === 2) { hoursRaw = { default: hoursRaw }; fixes.push('hours was a plain [open, close] pair; wrapped as {"default": [open, close]}.'); }
  if (isObj(hoursRaw)) {
    for (const [k, v] of Object.entries(hoursRaw)) {
      if (v === null) hours[k] = null;
      else if (Array.isArray(v) && v.length === 2 && v.every((x) => /^\d{1,2}:\d{2}$/.test(str(x)))) hours[k] = v.map(str);
      else fixes.push(`hours.${k} must look like ["09:00", "18:00"] or null; it was dropped.`);
    }
  } else if (hoursRaw != null && hoursRaw !== "") fixes.push('hours must be an object like {"default": ["09:00", "18:00"]}; it was reset.');

  const config: BusinessConfig = {
    identity, offerings, faqs, qualification_slots: slots, stuck_after_user_messages: posInt(r.stuck_after_user_messages, 14),
    sales_process: { goal, notes: str(sp.notes) },
    booking: { enabled: !!b.enabled, subject_label: str(b.subject_label) || "appointment" },
    hours,
    handoff: { contact_name: str(h.contact_name), staff_phones: strList(h.staff_phones), human_request_patterns: strList(h.human_request_patterns) },
    communication: { tone: str(c.tone) || "warm, brief and professional", greeting: str(c.greeting), rules: strList(c.rules) },
    restrictions: { never_say: strList(rs.never_say), prohibited_claims: strList(rs.prohibited_claims) },
    integrations: { payments: str(ig.payments) || null, payments_connected: !!ig.payments_connected, channel_connected: !!ig.channel_connected },
  };
  return { config, fixes };
}

/** Pasted JSON text -> normalized config, or a readable reason it can't be used (with the line/column when the browser reports one). */
export function parseConfigText(text: string): { ok: true; value: Normalized } | { ok: false; error: string } {
  const t = text.trim();
  if (!t) return { ok: false, error: "The box is empty. Paste a configuration first." };
  let parsed: unknown;
  try { parsed = JSON.parse(t); }
  catch (e) {
    const hint = /^```/.test(t) ? " It starts with ``` — paste only the JSON, without the code fence." : "";
    return { ok: false, error: `That is not valid JSON (${e instanceof Error ? e.message : String(e)}).${hint}` };
  }
  if (!isObj(parsed)) return { ok: false, error: "The configuration must be a JSON object ({ ... }), not a list or a single value." };
  return { ok: true, value: normalizeConfig(parsed) };
}

/** Paste this into any AI before asking it for a client's configuration. Mirrors hooze/config_engine/schema.py. */
export const SCHEMA_PROMPT = `You are preparing a configuration file (JSON) for the Hooze WhatsApp assistant platform.
Reply with ONLY one JSON object: no markdown code fence, no comments, no trailing commas, no explanation.

RULES
1. Use exactly the keys shown in the template below. Do not add other keys.
2. Every list of objects (offerings, faqs, qualification_slots, option_groups) must contain OBJECTS, never bare strings.
3. Include every field of every object, even when empty ("" for text, [] for lists, {} for objects, null where shown).
4. identity.facts is a flat object whose values are plain text only (e.g. {"Instagram": "@7trays"}). No lists, numbers, booleans or nulls.
5. Money is in kobo (minor units) in price_minor: N2,500 -> 250000. If the price is not known use null and put e.g. "on request" in price_note. Never guess a price.
6. sales_process.goal must be exactly "order" (customers buy and pay in chat) or "consult_and_book" (customers enquire and request a visit/booking).
7. Each qualification slot: "type" is one of text, number, money, enum, bool. Use "enum" only when you also fill "values". Make at most one or two slots "required": true.
8. hours: {"default": ["09:00", "18:00"]} (24-hour). Use "0".."6" (Mon..Sun) with null to mark a closed day.
8b. For menus: give every offering a "category" (e.g. "Rice", "Drinks") so the menu is shown in sections, and put direct public image/video links (.jpg .png .webp .mp4) in "media". Never invent links.
9. handoff.staff_phones: digits with country code, e.g. "2348012345678". Leave [] if unknown. Never invent phone numbers, prices, menu items or facts.
10. integrations flags stay false; the platform sets them when WhatsApp/payments are really connected.

TEMPLATE
{
  "identity": { "name": "", "industry": "", "description": "", "website": "", "address": "", "languages": ["English"], "timezone": "Africa/Lagos", "currency": "NGN", "facts": {} },
  "offerings": [
    { "name": "", "description": "", "price_minor": null, "price_note": "", "category": "", "url": "", "media": [], "details": {},
      "option_groups": [ { "name": "", "selection": "single", "required": false, "max_included": null, "options": [ { "name": "", "price_minor": 0 } ] } ] }
  ],
  "faqs": [ { "question": "", "answer": "" } ],
  "qualification_slots": [ { "name": "food_interest", "label": "What they want", "type": "text", "values": [], "aliases": {}, "required": true, "ask": "What would you like today?" } ],
  "stuck_after_user_messages": 14,
  "sales_process": { "goal": "consult_and_book", "notes": "" },
  "booking": { "enabled": true, "subject_label": "reservation request" },
  "hours": { "default": ["09:00", "18:00"] },
  "handoff": { "contact_name": "", "staff_phones": [], "human_request_patterns": ["speak to a human"] },
  "communication": { "tone": "warm, brief and professional", "greeting": "", "rules": [] },
  "restrictions": { "never_say": [], "prohibited_claims": [] },
  "integrations": { "payments": null, "payments_connected": false, "channel_connected": false }
}

THE BUSINESS TO CONFIGURE:
(describe the business here)`;
