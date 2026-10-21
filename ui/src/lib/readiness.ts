/**
 * Maps every readiness issue (by its `code` from hooze/config_engine/validator.py) to WHERE the person fixes it:
 * a section of the Setup form, or another tab (WhatsApp / Payments). This is what lets the UI highlight the exact
 * place instead of just listing errors. Unknown codes fall back to the goal section so nothing is ever unlinked.
 */
import type { Issue, Report } from "./api";

export type SectionId = "business" | "goal" | "offer" | "slots" | "faq" | "hours" | "team" | "tone";
export type FixTarget =
  | { kind: "section"; section: SectionId; label: string }
  | { kind: "tab"; tab: "whatsapp" | "payments"; label: string };

const sec = (section: SectionId, label: string): FixTarget => ({ kind: "section", section, label });
const tab = (t: "whatsapp" | "payments", label: string): FixTarget => ({ kind: "tab", tab: t, label });

const MAP: Record<string, FixTarget> = {
  "identity.name": sec("business", "Go to “The business”"),
  "identity.facts": sec("business", "Go to “The business”"),
  "sales_process.goal": sec("goal", "Go to “What the assistant should do”"),
  "booking.enabled": sec("goal", "Go to “What the assistant should do”"),
  playbook: sec("goal", "Go to “What the assistant should do”"),
  offerings: sec("offer", "Go to “What you offer”"),
  "offerings.price": sec("offer", "Go to “What you offer”"),
  "offerings.options": sec("offer", "Go to “What you offer”"),
  knowledge: sec("offer", "Go to “What you offer”"),
  qualification_slots: sec("slots", "Go to “Questions to ask”"),
  hours: sec("hours", "Go to “Opening hours”"),
  "handoff.staff_phones": sec("team", "Go to “Your team”"),
  "handoff.contact_name": sec("team", "Go to “Your team”"),
  restrictions: sec("tone", "Go to “Tone and limits”"),
  "integrations.channel": tab("whatsapp", "Open the WhatsApp tab"),
  "integrations.payments": sec("goal", "Go to “What the assistant should do”"),
  "integrations.payments_connected": tab("payments", "Open the Payments tab"),
};

export const fixTarget = (i: Issue): FixTarget => MAP[i.code] ?? sec("goal", "Go to “What the assistant should do”");

export interface SectionIssue { issue: Issue; level: "blocking" | "warning" }

/** Issues that live inside a form section, grouped by section (tab-type issues are not included). */
export function groupBySection(report: Report | null): Partial<Record<SectionId, SectionIssue[]>> {
  const out: Partial<Record<SectionId, SectionIssue[]>> = {};
  if (!report) return out;
  const add = (issue: Issue, level: SectionIssue["level"]) => {
    const t = fixTarget(issue);
    if (t.kind === "section") (out[t.section] ||= []).push({ issue, level });
  };
  report.blocking.forEach((i) => add(i, "blocking"));
  report.warnings.forEach((i) => add(i, "warning"));
  return out;
}
