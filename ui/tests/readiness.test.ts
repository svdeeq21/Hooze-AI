import { describe, expect, it } from "vitest";
import { fixTarget, groupBySection } from "../src/lib/readiness";

const issue = (code: string, message = code) => ({ code, message, field: code });

describe("readiness -> where to fix it", () => {
  it("sends payments and WhatsApp problems to their tabs, and form problems to a section", () => {
    expect(fixTarget(issue("integrations.payments_connected"))).toMatchObject({ kind: "tab", tab: "payments" });
    expect(fixTarget(issue("integrations.channel"))).toMatchObject({ kind: "tab", tab: "whatsapp" });
    expect(fixTarget(issue("handoff.staff_phones"))).toMatchObject({ kind: "section", section: "team" });
    expect(fixTarget(issue("offerings.price"))).toMatchObject({ kind: "section", section: "offer" });
  });
  it("never leaves an unknown code unlinked", () => {
    expect(fixTarget(issue("something.new"))).toMatchObject({ kind: "section", section: "goal" });
  });
  it("groups only in-form issues by section, keeping blocking vs warning", () => {
    const g = groupBySection({ ok: false, completeness: 0.5, blocking: [issue("handoff.staff_phones"), issue("integrations.channel")], warnings: [issue("handoff.contact_name")] });
    expect(g.team).toHaveLength(2);
    expect(g.team?.map((x) => x.level)).toEqual(["blocking", "warning"]);
    expect(Object.keys(g)).toEqual(["team"]);          // the WhatsApp issue is not a form section
  });
});
