/** Drives the real dashboard components against the real hooze-core API (seeded demo data) over HTTP. */
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { App, makeQueryClient } from "../src/App";
import { api, setSession, type TenantRow } from "../src/lib/api";
import { router } from "../src/router";

const BASE = "http://127.0.0.1:8765";
const signIn = () => setSession({ baseUrl: BASE, key: "test-key" });
const tenantByName = async (name: string): Promise<TenantRow> => (await api.tenants()).find((t) => t.name === name)!;

async function open(tenantId: string, tab: string) {
  render(<App client={makeQueryClient()} />);
  await act(async () => { await router.navigate({ to: "/clients/$tenantId", params: { tenantId }, search: { tab } }); });
}
async function openPath(to: "/" | "/clients") {
  render(<App client={makeQueryClient()} />);
  await act(async () => { await router.navigate({ to }); });
}
beforeEach(() => { signIn(); });

describe("sign-in", () => {
  it("rejects a wrong key with a readable message, then accepts the right one", async () => {
    sessionStorage.clear();
    const user = userEvent.setup();
    render(<App client={makeQueryClient()} />);
    await user.clear(screen.getByLabelText(/API address/i)); await user.type(screen.getByLabelText(/API address/i), BASE);
    await user.type(screen.getByLabelText(/Platform key/i), "wrong-key");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByText(/invalid|unauthor|api key/i)).toBeInTheDocument();
    await user.clear(screen.getByLabelText(/Platform key/i)); await user.type(screen.getByLabelText(/Platform key/i), "test-key");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByText("Clients live")).toBeInTheDocument();
  });
  it("tells the operator plainly when the API can't be reached", async () => {
    sessionStorage.clear();
    const user = userEvent.setup();
    render(<App client={makeQueryClient()} />);
    await user.clear(screen.getByLabelText(/API address/i)); await user.type(screen.getByLabelText(/API address/i), "http://127.0.0.1:1");
    await user.type(screen.getByLabelText(/Platform key/i), "x");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByText(/can't reach the API/i)).toBeInTheDocument();
  });
});

describe("overview and clients", () => {
  it("shows fleet health and both seeded clients", async () => {
    await openPath("/");
    expect(await screen.findByText("Clients live")).toBeInTheDocument();
    expect(await screen.findByText("Savoury Spud")).toBeInTheDocument();
    expect(screen.getByText("Praise Dynasty Properties")).toBeInTheDocument();
    expect(await screen.findByText(/payment unknown reference/i)).toBeInTheDocument();     // seeded ERROR event surfaces as a problem
  });
  it("creates a client, shows the key once, and lands on Setup", async () => {
    const user = userEvent.setup();
    await openPath("/clients");
    await user.click(await screen.findByRole("button", { name: /new client/i }));
    await user.type(screen.getByLabelText(/business name/i), "Lagos Lashes");
    expect(screen.getByLabelText(/short id/i)).toHaveValue("lagos-lashes");
    await user.click(screen.getByRole("button", { name: /^create$/i }));
    expect(await screen.findByText(/save this key now/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /continue to setup/i }));
    expect(await screen.findByText(/nothing is live yet/i)).toBeInTheDocument();
    expect((await api.tenants()).some((t) => t.slug === "lagos-lashes" && t.status === "onboarding")).toBe(true);
  });
});

describe("queue, alerts and events", () => {
  it("shows a message the queue gave up on, and Retry puts it back so it completes", async () => {
    const user = userEvent.setup();
    await openPath("/");
    expect(await screen.findByText(/1 failed/i)).toBeInTheDocument();
    expect(await screen.findByText(/boom: database timeout/i)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /^retry$/i }));
    expect(await screen.findByText(/back in the queue/i)).toBeInTheDocument();
    await waitFor(async () => expect(await api.inbound("failed")).toHaveLength(0));
    await waitFor(async () => expect((await api.inbound("done")).some((r) => r.last_error === "unparseable")).toBe(true));
    expect(await screen.findByText(/no failed messages/i)).toBeInTheDocument();
  });
  it("sends a test alert and says where it went", async () => {
    const user = userEvent.setup();
    await openPath("/");
    expect(await screen.findByText(/Console \(demo\)/)).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: /send test alert/i }));
    expect(await screen.findByText(/Test alert sent to Console \(demo\)/i)).toBeInTheDocument();
  });
  it("shows an event's details when you open it (they live in `metadata`)", async () => {
    const user = userEvent.setup();
    render(<App client={makeQueryClient()} />);
    await act(async () => { await router.navigate({ to: "/events" }); });
    await user.click(await screen.findByText("LLM_PROVIDER_FAILED"));
    expect(await screen.findByText(/gemini 503/)).toBeInTheDocument();
  });
});

describe("food client: orders, menu, payments", () => {
  it("moves a paid order through the kitchen and the API agrees", async () => {
    const t = await tenantByName("Savoury Spud");
    const user = userEvent.setup();
    await open(t.id, "orders");
    const row = await screen.findByText(/#\w{6}/);
    await user.click(row);
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/Loaded Potato Box/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: /start preparing/i }));
    await waitFor(async () => expect((await api.orders(t.id, "all"))[0].status).toBe("PREPARING"));
    await user.click(await within(dialog).findByRole("button", { name: /mark ready/i }));
    await waitFor(async () => expect((await api.orders(t.id, "all"))[0].status).toBe("READY"));
  });
  it("marks a product sold out from the menu", async () => {
    const t = await tenantByName("Savoury Spud");
    const user = userEvent.setup();
    await open(t.id, "menu");
    const card = (await screen.findByText("Loaded Potato Box")).closest("section")!;
    await user.click(within(card).getAllByRole("switch")[0]);
    await waitFor(async () => expect((await api.products(t.id)).find((p) => p.name === "Loaded Potato Box")!.available).toBe(false));
    expect(await screen.findByText("sold out")).toBeInTheDocument();
  });
  it("connects Paystack and warns that a localhost webhook can't be reached by Paystack", async () => {
    const t = await tenantByName("Savoury Spud");
    const user = userEvent.setup();
    await open(t.id, "payments");
    await user.type(await screen.findByLabelText(/paystack secret key/i), "sk_test_abc123");
    await user.click(screen.getByRole("button", { name: /connect paystack/i }));
    expect(await screen.findByText(/Paystack connected\./)).toBeInTheDocument();
    expect(await screen.findByText(/payment confirmations/i)).toBeInTheDocument();
    expect(await screen.findByText(/Paystack can't reach it/i)).toBeInTheDocument();
    expect((await api.integrations(t.id)).some((i) => i.kind === "paystack")).toBe(true);
  });
});

describe("conversations", () => {
  it("lets a person read the chat, reply as the business, and pauses the assistant", async () => {
    const t = await tenantByName("Savoury Spud");
    const user = userEvent.setup();
    await open(t.id, "conversations");
    await user.click((await screen.findAllByRole("button", { name: /ago|just now/i }))[0]);
    expect(await screen.findByPlaceholderText(/reply as the business/i)).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText(/reply as the business/i), "Hi, your order is nearly ready!");
    await user.click(screen.getByRole("button", { name: /^send$/i }));
    expect(await screen.findByText(/assistant is paused/i)).toBeInTheDocument();
    const conv = (await api.conversations(t.id))[0];
    const hist = await api.messages(t.id, conv.id);
    expect(hist.messages.some((m) => m.sender_type === "human" && /nearly ready/.test(m.content))).toBe(true);
    expect(hist.contact?.ai_paused).toBe(true);
    expect(await screen.findByRole("button", { name: /hand back to assistant/i })).toBeInTheDocument();
  });
});

describe("property client: bookings, setup", () => {
  it("confirms a booking at a typed local time, which the API stores in the tenant's timezone", async () => {
    const t = await tenantByName("Praise Dynasty Properties");
    const user = userEvent.setup();
    await open(t.id, "bookings");
    await user.click(await screen.findByRole("button", { name: /confirm…/i }));
    const dialog = await screen.findByRole("dialog");
    const dt = within(dialog).getByLabelText(/date and time/i);
    await user.clear(dt); await user.type(dt, "2026-10-20T15:00");
    await user.click(within(dialog).getByRole("button", { name: /confirm & tell customer/i }));
    expect(await screen.findByText(/Confirmed\. The customer has been told/i)).toBeInTheDocument();
    const b = (await api.bookings(t.id, "confirmed"))[0];
    expect(b.confirmed_at).toMatch(/15:00:00\+01:00$/);
  });
  it("round-trips the live configuration through the form, passes the readiness check, and goes live as a new version", async () => {
    const t = await tenantByName("Praise Dynasty Properties");
    const user = userEvent.setup();
    await open(t.id, "setup");
    expect(await screen.findByDisplayValue("Praise Dynasty Properties")).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: /check readiness/i })[0]);
    expect(await screen.findByText(/ready to go live/i)).toBeInTheDocument();
    await user.click(screen.getAllByRole("button", { name: /go live/i })[0]);
    expect(await screen.findByText(/Live! Version 2/i)).toBeInTheDocument();
    const live = await api.config(t.id);
    expect(live?.version).toBe(2);
    expect(live?.config.identity.name).toBe("Praise Dynasty Properties");
    expect(live?.config.offerings.length).toBeGreaterThan(0);                   // the form didn't drop their listings
  });
  it("says what's missing instead of going live when the setup is incomplete", async () => {
    const t = await tenantByName("Lagos Lashes");
    const user = userEvent.setup();
    await open(t.id, "setup");
    await user.click((await screen.findAllByRole("button", { name: /go live/i }))[0]);
    expect(await screen.findByText(/not ready yet/i)).toBeInTheDocument();
    expect(await screen.findByText(/must fix/i)).toBeInTheDocument();
    expect((await api.config(t.id))).toBeNull();
  });
});

describe("whatsapp and settings", () => {
  it("adds a WhatsApp number and reports the webhook address", async () => {
    const t = await tenantByName("Lagos Lashes");
    const user = userEvent.setup();
    await open(t.id, "whatsapp");
    await user.type(await screen.findByLabelText(/instance name/i), "lashes-main");
    await user.type(screen.getByLabelText(/evolution api key/i), "evo-key-123");
    await user.click(screen.getByRole("button", { name: /add number/i }));
    expect(await screen.findByText(/webhook set/i)).toBeInTheDocument();
    expect((await api.tenant(t.id)).channels.map((c) => c.instance_name)).toContain("lashes-main");
    const row = (await screen.findByText("lashes-main")).closest("li")!;
    await user.click(within(row).getByRole("button", { name: /^remove$/i }));
    expect(await screen.findByText(/number removed/i)).toBeInTheDocument();
    await waitFor(async () => expect((await api.tenant(t.id)).channels).toHaveLength(0));
  });
  it("saves staff numbers typed the local way in the international form", async () => {
    const t = await tenantByName("Praise Dynasty Properties");
    const user = userEvent.setup();
    await open(t.id, "setup");
    const box = await screen.findByLabelText(/staff whatsapp numbers/i);
    await user.clear(box);
    await user.type(box, "08012345678");
    await user.click(screen.getAllByRole("button", { name: /go live/i })[0]);
    await waitFor(async () => expect((await api.config(t.id))?.config.handoff.staff_phones).toEqual(["2348012345678"]));
  });
  it("creates and revokes a client key, and resets follow-up progress", async () => {
    const t = await tenantByName("Praise Dynasty Properties");
    const user = userEvent.setup();
    await open(t.id, "settings");
    await user.type(await screen.findByLabelText(/new key label/i), "owner");
    await user.click(screen.getByRole("button", { name: /create key/i }));
    expect(await screen.findByText(/never be shown again/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /close/i }));
    await waitFor(async () => expect((await api.tenant(t.id)).api_keys.some((k) => k.label === "owner")).toBe(true));
    const item = (await screen.findByText("owner")).closest("li")!;
    await user.click(within(item).getByRole("button", { name: /revoke/i }));
    await waitFor(async () => expect((await api.tenant(t.id)).api_keys.find((k) => k.label === "owner")!.revoked_at).toBeTruthy());
    await user.click(await screen.findByRole("button", { name: /reset follow-up progress/i }));
    expect(await screen.findByText(/Follow-up progress reset/i)).toBeInTheDocument();
  });
  it("creates a read-only key and shows each key's access level", async () => {
    const t = await tenantByName("Praise Dynasty Properties");
    const user = userEvent.setup();
    await open(t.id, "settings");
    await user.type(await screen.findByLabelText(/new key label/i), "accountant");
    await user.selectOptions(screen.getByLabelText(/key access/i), "viewer");
    expect(screen.getByText(/^Read-only\./)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /create key/i }));
    await user.click(await screen.findByRole("button", { name: /close/i }));
    await waitFor(async () => expect((await api.tenant(t.id)).api_keys.find((k) => k.label === "accountant")?.role).toBe("viewer"));
    const item = (await screen.findByText("accountant")).closest("li")!;
    expect(within(item).getByText("viewer")).toBeInTheDocument();
  });
  it("previews unused photos and duplicate chats, and rebuilds the search index", async () => {
    const t = await tenantByName("Praise Dynasty Properties");
    const user = userEvent.setup();
    await open(t.id, "settings");
    await user.click(await screen.findByRole("button", { name: /preview unused files/i }));
    expect(await screen.findByText(/unused file\(s\) would be deleted/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /check for duplicates/i }));
    expect(await screen.findByText(/no customer has duplicate chats/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /rebuild search index/i }));
    expect(await screen.findByText(/search index up to date/i)).toBeInTheDocument();
  });
  it("downloads a customer's data and previews an erasure without changing anything", async () => {
    const t = await tenantByName("Praise Dynasty Properties");
    const phone = (await api.conversations(t.id))[0].contact!.phone;
    (URL as any).createObjectURL = () => "blob:test"; (URL as any).revokeObjectURL = () => {};
    const user = userEvent.setup();
    await open(t.id, "settings");
    expect(await screen.findByRole("button", { name: /download their data/i })).toBeDisabled();       // needs a number first
    await user.type(await screen.findByLabelText(/customer's phone number/i), phone);
    await user.click(screen.getByRole("button", { name: /download their data/i }));
    expect(await screen.findByText(/^Downloaded\./)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /preview erasing/i }));
    expect(await screen.findByText(/would erase/i)).toBeInTheDocument();
    expect((await api.conversations(t.id)).some((c) => c.contact?.phone === phone)).toBe(true);        // a preview deletes nothing
  });
  it("deletes a client only after it is paused and its id is typed", async () => {
    const made = await api.createTenant("to-delete", "To Delete");
    const id = made.tenant.id;
    const user = userEvent.setup();
    await open(id, "settings");
    const typed = await screen.findByLabelText(/confirm client id/i);
    expect(typed).toBeDisabled();                                                                    // not paused yet
    cleanup();                                                                                         // re-open from scratch once the client is paused
    await api.patchTenant(id, { status: "suspended" });
    await open(id, "settings");
    const box = await screen.findByLabelText(/confirm client id/i);
    await waitFor(() => expect(box).toBeEnabled());
    const btn = screen.getByRole("button", { name: /delete client/i });
    expect(btn).toBeDisabled();
    await user.type(box, "wrong-id");
    expect(btn).toBeDisabled();
    await user.clear(box); await user.type(box, "to-delete");
    await user.click(btn);
    await waitFor(async () => expect((await api.tenants()).some((x) => x.id === id)).toBe(false));
  });
  it("pauses and resumes a client's assistant from the overview", async () => {
    const t = await tenantByName("Praise Dynasty Properties");
    const user = userEvent.setup();
    await open(t.id, "overview");
    await user.click(await screen.findByRole("button", { name: /pause assistant/i }));
    await waitFor(async () => expect((await tenantByName("Praise Dynasty Properties")).status).toBe("suspended"));
    await user.click(await screen.findByRole("button", { name: /resume assistant/i }));
    await waitFor(async () => expect((await tenantByName("Praise Dynasty Properties")).status).toBe("active"));
  });
});
