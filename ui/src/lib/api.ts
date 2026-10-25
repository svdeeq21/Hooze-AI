// Typed client for the hooze-core operator API. The operator key lives in sessionStorage only (cleared when the tab closes).
export type Session = { baseUrl: string; key: string };
const SK = "hooze.operator.session";

export const getSession = (): Session | null => {
  try { return JSON.parse(sessionStorage.getItem(SK) || "null"); } catch { return null; }
};
export const setSession = (s: Session) => sessionStorage.setItem(SK, JSON.stringify({ baseUrl: s.baseUrl.replace(/\/+$/, ""), key: s.key.trim() }));
export const clearSession = () => { sessionStorage.removeItem(SK); window.dispatchEvent(new Event("hooze:logout")); };

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail?: unknown) { super(message); }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const s = getSession();
  if (!s) throw new ApiError(401, "Not signed in");
  let res: Response;
  try {
    res = await fetch(s.baseUrl + path, {
      method,
      // FormData (file uploads): let the browser set the multipart content-type and boundary itself
      headers: { "x-api-key": s.key, "ngrok-skip-browser-warning": "1", ...(body !== undefined && !(body instanceof FormData) ? { "content-type": "application/json" } : {}) },
      body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Can't reach the API. Is it running, and is this site's address in CORS_ALLOWED_ORIGINS?");
  }
  const text = await res.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* non-JSON error page (e.g. a tunnel interstitial) */ }
  if (res.status === 401 && path !== "/v1/platform/overview") clearSession();
  if (!res.ok) {
    const d = data?.detail;
    const msg = typeof d === "string" ? d : d?.message ?? (Array.isArray(d) ? d.map((x: any) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ") : `Request failed (${res.status})`);
    throw new ApiError(res.status, msg, d);
  }
  return data as T;
}
const get = <T,>(p: string) => req<T>("GET", p);
const post = <T,>(p: string, b: unknown = {}) => req<T>("POST", p, b);
const patch = <T,>(p: string, b: unknown) => req<T>("PATCH", p, b);
const del = <T,>(p: string) => req<T>("DELETE", p);

// ── types ─────────────────────────────────────────────────────────────
export interface Contact { id: string; phone: string; display_name: string | null; ai_paused: boolean; opted_out: boolean; slots: Record<string, unknown> }
export interface Tenant { id: string; slug: string; name: string; status: "onboarding" | "active" | "suspended"; created_at: string }
export interface TenantRow extends Tenant { channels: number; active_version: number | null; conversations: number; handoff: number }
export interface Channel { id: string; kind: string; instance_name: string; status: string; base_url: string | null; connection: string; webhook_url: string }
export type KeyRole = "admin" | "staff" | "viewer";
export interface ApiKeyRow { id: string; label: string; role?: KeyRole; revoked_at: string | null; created_at: string }
export interface MediaCleanup { dry_run: boolean; listed: number; in_use: number; too_new: number; deleted: number; would_delete: string[]; skipped: string }
export interface MergeReport { dry_run: boolean; customers_with_duplicates: number; conversations_removed: number }
export interface ErasePreview { dry_run: boolean; messages: number; conversations: number; bookings: number; queued_webhooks: number; orders_kept_but_scrubbed: number }
export interface DeleteResult { deleted: boolean; files_deleted: number; instances_deleted: number; instances_to_remove_by_hand: { instance: string; error: string }[] }
export interface ReindexResult { model: string; tenants: Record<string, { status: string; documents?: number; updated?: number; unchanged?: number; failed?: number; error?: string }> }
export interface Integration { id: string; kind: string; status: string; payment_webhook_url: string; created_at: string }
export interface Issue { code: string; message: string; field: string }
export interface Report { ok: boolean; completeness: number; blocking: Issue[]; warnings: Issue[] }
export interface ActiveConfig { version: number; activated_at: string | null; config: BusinessConfig; validation_report: Report | null }
export interface TenantDetail { tenant: Tenant; channels: Channel[]; api_keys: ApiKeyRow[]; integrations: Integration[]; active_config: ActiveConfig | null }
export interface EventRow { id?: string; severity: string; event: string; tenant_id: string | null; metadata: Record<string, unknown>; created_at: string }
export interface LlmProvider { name: string; model: string | null; cooling_generate_s: number; cooling_embed_s: number }
export interface InboundRow { id: string; tenant_id: string; tenant_name: string | null; status: string; attempts: number; last_error: string | null; received_at: string }
export interface Overview {
  alerts: { configured: boolean; destinations: string[] };
  inbound: { pending: number; processing: number; failed: number };
  tenants: { total: number; active: number; onboarding: number; suspended: number };
  llm_providers: LlmProvider[]; recent_issues: EventRow[];
  platform: { app_env: string; public_base_url: string; nudges_globally_enabled: boolean; shared_state: string; llm_timeout_seconds: number };
}
export interface Stats {
  conversations: Record<string, number>; messages_24h: { inbound: number; bot_replies: number };
  orders: Record<string, number>; bookings: Record<string, number>; revenue_minor: { total: number; last_7d: number; truncated: boolean };
}
export interface Conversation { id: string; contact_id: string; state: string; status: string; handoff_reason: string | null; message_count: number; last_inbound_at: string | null; updated_at: string; contact: Contact | null }
export interface Message { id: string; direction: "inbound" | "outbound"; sender_type: "contact" | "bot" | "human" | "system"; content: string; content_type: string; created_at: string }
export interface Booking { id: string; status: string; subject: string | null; preferred_text: string | null; suggested_at: string | null; confirmed_at: string | null; staff_notes: string | null; created_at: string; contact: Contact | null }
export interface Order { id: string; status: string; subtotal_minor: number; delivery_fee_minor: number; fulfillment_method: string | null; delivery_address: string | null; paid_at: string | null; created_at: string; contact: Contact | null }
export interface OrderDetail { order: Order; items: { id: string; product_name: string; quantity: number; unit_price_minor: number; line_total_minor?: number; modifiers?: unknown }[]; payments: { id: string; provider: string; reference: string; status: string; amount_minor: number; verified_at: string | null }[]; contact: Contact | null }
export interface Modifier { id: string; name: string; price_minor: number; available: boolean }
export interface Product { id: string; name: string; description: string | null; price_minor: number; available: boolean; groups: { id: string; name: string; required: boolean; modifiers: Modifier[] }[] }

// config (mirrors hooze/config_engine/schema.py)
export interface OptionChoice { name: string; price_minor: number }
export interface OptionGroup { name: string; selection: "single" | "multiple"; required: boolean; max_included: number | null; options: OptionChoice[] }
export interface Offering { name: string; description: string; price_minor: number | null; price_note: string; category: string; url: string; media: string[]; details: Record<string, string>; option_groups: OptionGroup[] }
export interface Slot { name: string; label: string; type: "text" | "number" | "money" | "enum" | "bool"; values: string[]; aliases: Record<string, string[]>; required: boolean; ask: string }
export interface BusinessConfig {
  identity: { name: string; industry: string; description: string; website: string; address: string; languages: string[]; timezone: string; currency: string; facts: Record<string, string> };
  offerings: Offering[]; faqs: { question: string; answer: string }[]; qualification_slots: Slot[]; stuck_after_user_messages: number;
  sales_process: { goal: "order" | "consult_and_book" | null; notes: string };
  booking: { enabled: boolean; subject_label: string };
  hours: Record<string, string[] | null>;
  handoff: { contact_name: string; staff_phones: string[]; human_request_patterns: string[] };
  communication: { tone: string; greeting: string; rules: string[] };
  restrictions: { never_say: string[]; prohibited_claims: string[] };
  integrations: { payments: string | null; payments_connected: boolean; channel_connected: boolean };
}

export const emptyConfig = (): BusinessConfig => ({
  identity: { name: "", industry: "", description: "", website: "", address: "", languages: ["English"], timezone: "Africa/Lagos", currency: "NGN", facts: {} },
  offerings: [], faqs: [], qualification_slots: [], stuck_after_user_messages: 14, sales_process: { goal: null, notes: "" },
  booking: { enabled: false, subject_label: "appointment" }, hours: { default: ["09:00", "18:00"] },
  handoff: { contact_name: "", staff_phones: [], human_request_patterns: [] }, communication: { tone: "warm, brief and professional", greeting: "", rules: [] },
  restrictions: { never_say: [], prohibited_claims: [] }, integrations: { payments: null, payments_connected: false, channel_connected: false },
});

const T = (id: string) => `/v1/tenants/${id}`;
const PT = (id: string) => `/v1/platform/tenants/${id}`;
export interface UploadedMedia { url: string; type: "image" | "video"; bytes: number; name: string }
export const api = {
  uploadMedia: (id: string, file: File) => { const f = new FormData(); f.append("file", file); return req<UploadedMedia>("POST", `/v1/tenants/${id}/media`, f); },
  overview: () => get<Overview>("/v1/platform/overview"),
  tenants: () => get<{ tenants: TenantRow[] }>("/v1/platform/tenants").then((r) => r.tenants),
  tenant: (id: string) => get<TenantDetail>(PT(id)),
  createTenant: (slug: string, name: string) => post<{ tenant: Tenant; api_key: string }>("/v1/platform/tenants", { slug, name }),
  patchTenant: (id: string, b: { status?: "active" | "suspended"; name?: string }) => patch<{ tenant: Tenant }>(PT(id), b),
  createKey: (id: string, label: string, role: KeyRole = "staff") => post<{ id: string; api_key: string; role: KeyRole }>(`${PT(id)}/api-keys`, { label, role }),
  cleanupMedia: (id: string, dry_run: boolean) => post<MediaCleanup>(`${PT(id)}/media/cleanup?dry_run=${dry_run}`),
  mergeChats: (id: string, dry_run: boolean) => post<MergeReport>(`${PT(id)}/conversations/merge?dry_run=${dry_run}`),
  reindex: (id: string) => post<ReindexResult>(`/v1/platform/embeddings/reindex?tenant_id=${id}`),
  deleteClient: (id: string, slug: string) => del<DeleteResult>(`${PT(id)}?confirm=${encodeURIComponent(slug)}`),
  exportCustomer: (id: string, phone: string) => get<Record<string, unknown>>(`${T(id)}/customers/${encodeURIComponent(phone)}/export`),
  eraseCustomer: (id: string, phone: string, dry_run: boolean) => del<ErasePreview>(`${T(id)}/customers/${encodeURIComponent(phone)}?dry_run=${dry_run}`),
  revokeKey: (id: string, kid: string) => del(`${PT(id)}/api-keys/${kid}`),
  provision: (id: string, b: { instance_name: string; base_url: string; api_key: string; create_instance: boolean; webhook_base_url?: string }) =>
    post<{ channel: Channel; webhook_url: string; webhook_configured: boolean; note: string | null }>(`${PT(id)}/channels/provision`, b),
  channelQr: (id: string, cid: string) => get<{ connected: boolean; qr_base64: string | null; pairing_code: string | null }>(`${PT(id)}/channels/${cid}/qr`),
  removeChannel: (id: string, cid: string) => del(`${PT(id)}/channels/${cid}`),
  channelStatus: (id: string, cid: string) => get<{ connection: string }>(`${PT(id)}/channels/${cid}/status`),
  reconnectWebhook: (id: string, cid: string, webhook_base_url?: string) => post<{ webhook_url: string }>(`${PT(id)}/channels/${cid}/webhook`, { webhook_base_url: webhook_base_url || null }),
  testAlert: () => post<{ configured: boolean; results: { sink: string; ok: boolean; error?: string }[] }>("/v1/platform/alerts/test"),
  inbound: (status = "failed") => get<{ inbound: InboundRow[] }>(`/v1/platform/inbound?status=${status}`).then((r) => r.inbound),
  retryInbound: (id: string) => post<{ retrying: boolean }>(`/v1/platform/inbound/${id}/retry`),
  tick: () => post<{ status: string; tenants: number; results: Record<string, unknown> }>("/v1/platform/tick"),
  events: (q: { tenant_id?: string; severity?: string; event?: string; limit?: number }) => {
    const p = new URLSearchParams(Object.entries(q).filter(([, v]) => v !== undefined && v !== "").map(([k, v]) => [k, String(v)]));
    return get<{ events: EventRow[] }>(`/v1/platform/events?${p}`).then((r) => r.events);
  },
  config: (id: string) => get<{ active: ActiveConfig | null }>(`${T(id)}/config`).then((r) => r.active),
  validate: (id: string, bc: BusinessConfig) => post<Report>(`${T(id)}/config/validate`, bc),
  activate: (id: string, bc: BusinessConfig) => post<{ activated: boolean; version: number; seeded: Record<string, number>; report: Report }>(`${T(id)}/config/activate`, bc),
  stats: (id: string) => get<Stats>(`${T(id)}/stats`),
  conversations: (id: string) => get<{ conversations: Conversation[] }>(`${T(id)}/conversations`).then((r) => r.conversations),
  messages: (id: string, cid: string) => get<{ conversation: Conversation; contact: Contact | null; messages: Message[] }>(`${T(id)}/conversations/${cid}/messages`),
  pauseResume: (id: string, cid: string, action: "pause" | "resume") => post(`${T(id)}/conversations/${cid}/${action}`),
  sendHuman: (id: string, cid: string, text: string) => post(`${T(id)}/conversations/${cid}/send`, { text }),
  bookings: (id: string, status = "all") => get<{ bookings: Booking[] }>(`${T(id)}/bookings?status=${status}`).then((r) => r.bookings),
  confirmBooking: (id: string, bid: string, when: string, notes?: string) => post(`${T(id)}/bookings/${bid}/confirm`, { when, notes: notes || null }),
  declineBooking: (id: string, bid: string, notes?: string) => post(`${T(id)}/bookings/${bid}/decline`, { when: "", notes: notes || null }),
  orders: (id: string, status = "all") => get<{ orders: Order[] }>(`${T(id)}/orders?status=${status}`).then((r) => r.orders),
  order: (id: string, oid: string) => get<OrderDetail>(`${T(id)}/orders/${oid}`),
  setOrderStatus: (id: string, oid: string, status: string, notify_customer = true) => post<{ customer_notified: boolean }>(`${T(id)}/orders/${oid}/status`, { status, notify_customer }),
  products: (id: string) => get<{ products: Product[] }>(`${T(id)}/products`).then((r) => r.products),
  setProductAvailable: (id: string, pid: string, available: boolean) => patch(`${T(id)}/products/${pid}`, { available }),
  setModifierAvailable: (id: string, mid: string, available: boolean) => patch(`${T(id)}/modifiers/${mid}`, { available }),
  integrations: (id: string) => get<{ integrations: Integration[] }>(`${T(id)}/integrations`).then((r) => r.integrations),
  connectPaystack: (id: string, secret_key: string, email_domain: string) => post<{ connected: boolean; webhook_url: string }>(`${T(id)}/integrations/paystack`, { secret_key, email_domain }),
  resetNudges: (id: string) => post<{ reset: number }>(`${T(id)}/nudges/reset`),
};
