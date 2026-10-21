import { describe, expect, it } from "vitest";
import broken from "./fixtures/broken_7_trays.json";
import { SCHEMA_PROMPT, normalizeConfig, parseConfigText } from "../src/lib/configNormalize";

describe("normalizeConfig", () => {
  it("makes the config that crashed the Setup tab safe to render", () => {
    const { config, fixes } = normalizeConfig(broken);
    expect(config.qualification_slots[0]).toMatchObject({ name: "customer_name", type: "text", values: [], required: false });
    // the exact calls the form makes:
    expect(() => config.qualification_slots.map((s) => s.values.join(", "))).not.toThrow();
    expect(() => config.offerings.map((o) => o.media.join("\n"))).not.toThrow();
    expect(config.identity.facts).toEqual({ products: "Food" });
    expect(fixes.some((f) => f.includes("qualification_slots[0]"))).toBe(true);
  });

  it("survives garbage without throwing and never invents phones or goal", () => {
    for (const junk of [null, 5, "x", [], {}, { offerings: "no", qualification_slots: 3, hours: 7, identity: [] }]) {
      const { config } = normalizeConfig(junk);
      expect(config.handoff.staff_phones).toEqual([]);
      expect(config.sales_process.goal).toBeNull();
      expect(Array.isArray(config.offerings)).toBe(true);
    }
  });

  it("converts naira price to kobo and reports it", () => {
    const { config, fixes } = normalizeConfig({ offerings: [{ name: "Jollof", price: "2,500" }] });
    expect(config.offerings[0].price_minor).toBe(250000);
    expect(fixes.join(" ")).toContain("kobo");
  });

  it("is idempotent on an already-valid config", () => {
    const first = normalizeConfig(broken).config;
    const second = normalizeConfig(first);
    expect(second.fixes).toEqual([]);
    expect(second.config).toEqual(first);
  });
});

describe("parseConfigText", () => {
  it("explains bad JSON, code fences and non-objects in plain words", () => {
    expect(parseConfigText("").ok).toBe(false);
    const fence = parseConfigText("```json\n{}\n```");
    expect(!fence.ok && fence.error).toContain("code fence");
    const arr = parseConfigText("[1]");
    expect(!arr.ok && arr.error).toContain("JSON object");
    expect(parseConfigText('{"identity":{"name":"A"}}').ok).toBe(true);
  });
  it("ships a schema prompt that names the rules that caused the crash", () => {
    expect(SCHEMA_PROMPT).toContain("OBJECTS, never bare strings");
    expect(SCHEMA_PROMPT).toContain("flat object");
  });
});
