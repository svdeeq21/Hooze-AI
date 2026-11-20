import { describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { normalizeConfig } from "../src/lib/configNormalize";
import ChatPanel, { describe as describeStep, money } from "../src/components/ChatPanel";
import { applyTheme, getTheme, setTheme } from "../src/lib/theme";
import type { ChatTurn } from "../src/lib/api";

describe("operating mode survives the Setup form", () => {
  it("keeps after-hours and assist, and turns anything else into full auto", () => {
    expect(normalizeConfig({ communication: { mode: "After Hours" } }).config.communication.mode).toBe("after_hours");
    expect(normalizeConfig({ communication: { mode: "assist" } }).config.communication.mode).toBe("assist");
    expect(normalizeConfig({ communication: { mode: "turbo" } }).config.communication.mode).toBe("full_auto");
    expect(normalizeConfig({}).config.communication.mode).toBe("full_auto");
  });
});

describe("theme switch", () => {
  it("follows the device by default and remembers an explicit choice", () => {
    setTheme("system");
    expect(getTheme()).toBe("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    setTheme("light");
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    applyTheme();
    expect(getTheme()).toBe("light");
    setTheme("system");
  });
});

describe("test chat panel", () => {
  it("explains each step in plain words", () => {
    expect(describeStep({ event: "TURN_COMPLETED", severity: "INFO", tools: ["add_product", "checkout"], timings: { total_ms: 2400 } }))
      .toBe("Added an item to the order · Created the payment link — replied in 2.4 s");
    expect(describeStep({ event: "ESCALATED", severity: "WARNING", reason: "HUMAN_REQUESTED" })).toBe("Handed the chat to a person (human requested)");
    expect(describeStep({ event: "SOMETHING_INTERNAL", severity: "INFO" })).toBeNull();
    expect(money(1280000)).toBe("₦12,800");
  });

  it("sends a message, shows the real reply, the owner alert and the pay button", async () => {
    const turn: ChatTurn = {
      status: "replied", replies: [{ type: "text", text: "Here's your link *₦12,800*" }], owner_alerts: ["🔔 New order"],
      activity: [{ event: "TURN_COMPLETED", severity: "INFO", tools: ["checkout"] }],
      state: { slots: { budget: "₦200m" }, stage: "BROWSING", status: "active", payment_due: true, booking: null,
               order: { id: "o1", status: "PAYMENT_PENDING", subtotal_minor: 1280000, fulfillment: "PICKUP", address: null, items: [{ name: "Jollof", quantity: 2, line_total_minor: 1280000, options: ["Turkey"] }] } },
    };
    const sent: string[] = [];
    render(<ChatPanel title="Mama Tee's Kitchen" send={async (t) => { sent.push(t); return turn; }} pay={async () => turn} />);
    await userEvent.type(screen.getByLabelText("Message"), "pay please");
    await userEvent.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(screen.getByText("₦12,800", { selector: "b" })).toBeInTheDocument());
    expect(sent).toEqual(["pay please"]);
    expect(screen.getByText("🔔 New order")).toBeInTheDocument();
    expect(screen.getByText("Created the payment link")).toBeInTheDocument();
    expect(screen.getByText(/2× Jollof \(Turkey\)/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Pay now/ })).toBeInTheDocument();
  });
});
