# Hooze Platform — Session 3 Handoff (re-audit, hardening, next steps)

Picks up from `HOOZE_FULL_HANDOFF.md` (Sessions 1–2). Read that first for *why* hooze-core exists and how the first live round trip was achieved.

## 1. State at takeover (verified, not assumed)
- Extracted the zip, installed deps in a clean venv (Python 3.12): **171/171 tests passed**, matching the handoff.
- `.env` parses correctly (inline `# comments` are stripped by python-dotenv; vault key is a valid Fernet key; `assert_production_ready()` passes).
- Applied `migrations/0001` to a scratch **Postgres 16 + pgvector 0.6** (not Supabase): clean, 20 tables.

## 2. What the re-audit found (all reproduced; ✔ = fixed + regression-tested in `tests/test_hardening.py`)

| # | Severity | Defect | Evidence | Status |
|---|---|---|---|---|
| 1 | **High (money)** | A payment that arrives after checkout expired (30 min tick) or was cancelled was logged `PAYMENT_DUPLICATE_IGNORED` and dropped: customer charged, order not paid, no alert | test red on old code | ✔ now marks PAID, emits `PAYMENT_AFTER_EXPIRED/CANCELLED`, alerts staff to fulfil or refund; still exactly-once |
| 2 | **High (data)** | `claim_inbound` treated *every* exception as "duplicate" → any DB error silently dropped the customer's message | real PG: dup = `23505`, other failures differ (`23514`) | ✔ only `23505` is a duplicate; others raise → `TURN_CRASHED` |
| 3 | Medium | Nudges, booking confirmations, payment receipts were sent but never saved to the transcript (the LLM couldn't see what the customer was replying to) | grep: no caller passed `store` | ✔ `Outbox` records them; staff alerts deliberately excluded |
| 4 | **High (availability)** | Config activation = archive old, *then* insert new. A failure between left the tenant with no active config (bot silently ignores all messages). `activated_at` always NULL | code + real PG | ✔ migration `0002` `activate_config_tx`, one transaction; rollback proven by failure injection on real PG |
| 5 | Medium | Naive confirm time (`2026-10-20T15:00:00`) stored as 15:00 **UTC** (= 16:00 Lagos) while the customer was told 3pm | real PG | ✔ naive = tenant timezone |
| 6 | Medium (latency) | Dead/slow primary LLM re-tried every turn; `embed` AND `interpret` each paid the 20s timeout (the 24s turn in the first live test) | gateway code | ✔ per-provider cooldown (30s). `generate` tries all if all cooling (never fail-closed: that would pause AI for customers); live `embed` fails fast |
| 7 | Medium | Overlapping `/internal/tick` runs possible (the 2026-08-13 nudge incident class) | test red | ✔ non-blocking `try_lock`; returns `already_running` |
| 8 | Medium (scale) | `match_knowledge` returned **2 of 20** records whenever the planner chose the HNSW index (tenant filter applied after ANN scan). The H8 claim held only by planner luck | measured, 12k rows/41 tenants | ✔ `0002` ranks a MATERIALIZED tenant-filtered set; = index-free ground truth 10/10 |

Method note: one of my own checks briefly "failed" because the ground-truth query itself took the HNSW plan. Verify the verifier.

Result: **187 tests pass** (171 + 16). Files changed: see section 6.

## 2b. Operator dashboard + the endpoints it needed (added later in Session 3)
- **New API (`hooze/api/operator.py`, 26 tests + 5 deliberate-breakage checks):** fleet overview/health (LLM provider status), client list/detail, suspend/resume, extra API keys, WhatsApp provision (optionally creates the Evolution instance) / status / QR / webhook rotate, run-follow-ups-now, event log with filters, active config read, stats, conversation history, **reply-as-human** (records as `human`, pauses the assistant), orders list/detail/status flow (customer told when READY/CANCELLED), menu + sold-out toggles, integrations, follow-up reset, bookings by status with customer attached. Conversations/bookings listings now include the customer. CORS is an **allow-list** (`CORS_ALLOWED_ORIGINS`), never `*`.
- **Dashboard (`ui/`):** Vite + React + TS + TanStack Router/Query + Tailwind v4 (your site's stack, your brand). 14 workflow tests drive the real components against the real API on seeded data. `python -m scripts.demo_server` runs the API with demo data (no keys needed).
- **Not verified:** how it looks (no browser available), Evolution QR/webhook/create-instance calls against a live Evolution, mobile use.

## 2c. Found in the first real WhatsApp conversation (logs of 2026-10-14) — fixed
- **Stuck-lead escalation fired on every turn** once a chat passed N lifetime messages, even while the customer was answering (slots 3→2→1 missing). Now it tracks progress per contact: escalates only after N messages with nothing new learned; existing long chats get a fresh allowance. (`qualification.after_turn`; 3 new tests; one older test narrowed from "attributes == {}" to "no slots written".)
- **`share_item` sent only the title** for records without card fields, so an FAQ went out as the bare question "How do I get started?". Now includes the record's text when only a title would render. Property cards with real fields are unchanged.
- **Not fixed, needs your machine:** Gemini times out on every turn (20s wasted, ~35s per reply). Run `python -m scripts.llm_check`; then set `LLM_TIMEOUT_SECONDS=8` and `LLM_PROVIDER_COOLDOWN_SECONDS=300`.
- **Content, not code:** the assistant doesn't answer "can I trust you?" (no proof, founder, case studies, process in its knowledge base), and a returning test contact in state BOOKED is greeted with "your request has been received". Test with a fresh number, and add trust content under Setup/knowledge.

## 2d. Durable queue + alerts + first scale step (added after the first real conversation)
- **Durable inbound queue** (`migrations/0003`, `runtime/inbox.py`): the webhook INSERTs the message, then acknowledges, then processes. Recovery loop (`runtime/maintenance.py`, started in the app lifespan) re-claims interrupted rows via atomic DB leases; 5 attempts with 30s/60s/120s/240s backoff; then `failed` + `INBOUND_FAILED` alert; operator Retry from the dashboard. Replay guard in the pipeline: a replayed message is only re-run if nothing was sent in answer to it. **Verified on real Postgres:** dedupe (23505), atomic claims, no stealing of fresh/leased rows, expired-lease recovery, backoff gating, permissions, and a real two-session race (disjoint claims). **Tested in Python with deliberate breakage:** crash mid-turn → exactly one reply; worker dies after replying → no second reply; DB down at enqueue → 503 not a false ack.
- **Alerts** (`hooze/alerts.py`): 14 rules over real event names (a test fails if a rule watches a name nothing emits), de-duplicated per client+problem, thresholds for repeating problems, plain-language advice, WhatsApp + Resend email, never blocks or breaks a turn. WhatsApp-connection monitor emits `CHANNEL_DISCONNECTED/RECOVERED`. `GET /health/ready` for an external uptime monitor. Dashboard: queue card (failed list + Retry), alerts card (+ *Send test alert*).
- **Scale step:** dashboard stats were ~25 COUNT queries per refresh and the client list 2 per client; now one aggregate query each (`migrations/0004`, verified against naive counts on real Postgres). Concurrency of turns is capped per process (`INBOUND_MAX_CONCURRENCY`).
- **Bug fixed along the way:** the dashboard's Events page read `data` but the column is `metadata` (details showed blank); my test had used the same wrong name.
- **Still true:** one worker only; alerts cannot report the app itself being down; delivery of alerts through Evolution depends on that number staying connected (email is the backstop).

## 2e. Real chat transcript + logs (14–16 Oct): what was wrong, and what was NOT
An outside review of the chat suggested duplicate webhooks, an un-modelled conversation state, and the product being "real-estate shaped". Checked against the code and logs, the causes were different:
| Symptom | Actual cause | Status |
|---|---|---|
| Two greetings to one "Hello" | The scripted welcome is sent on a new contact's first message, and the model then replied with its own greeting. Not duplicate webhooks (logs show exactly two sends, the first before any AI call). | Fixed + tested |
| "…request for General consultation ()" | Booking tool accepted an empty time; template printed empty parentheses | Fixed: asks for the time first |
| "Sorry, could you rephrase that?" after "Yes i would" | The gateway's fallback for UNPARSEABLE model output (Gemini 503 + Groq reasoning model returning empty/non-JSON), not a comprehension failure | Fixed: retry on the other provider; neutral fallback; outage = hold + retry |
| Brochure card repeated | `share_item` over-used by the model and re-sent every time | Fixed: once per customer per 24h; prompt rule |
| "How do I get started?" on a trust question | Card rendered only the record's title (an FAQ question) | Fixed earlier |
| "Confirmed: … 12:36PM" | That was a HUMAN confirmation made from the dashboard (the request had correctly said "they'll confirm"), not the bot confirming | Not a bug |
| Real-estate wording for a games shop | The tenant's own product card says "Ideal customers: real estate companies" | **Content**, not code: rewrite the card; prompt now says tailor to the customer's business |
| Trust question answered poorly | No trust content in the knowledge base | **Content** |
| Long waits (10–19s) | Gemini 503 each turn (then Groq), ~15 sequential DB round trips from Nigeria (~1.5s), AND a typing wait that happened twice | Typing fixed; order/effort settings + `llm_check`; deploy near Supabase to cut DB time |
Also: channel monitor wrote an ERROR row every 2 min per dead number (now once per 30 min, active clients only); alert sender now uses a CONNECTED number; empty error messages now show the exception type.

## 3. Found but NOT fixed (decide / schedule)
1. **Secrets were included in the uploaded zip** (`.env`: Supabase service-role key, Gemini, Groq, platform admin, cron secret, vault key). Treat as exposed: rotate. Rotate `VAULT_MASTER_KEY` **now, while only one test tenant exists** (it encrypts stored channel/Paystack keys; later it means re-entering every client's credentials). First run `scripts/vault_check.py`: `.env` also has an **orphan Fernet-format line** (not equal to any setting) that may be an older vault key.
2. Webhook secret may be sent as `?token=` (ends up in access logs). Prefer the `x-hooze-secret` header; consider removing the query form.
3. Webhook acks then processes in an in-process background task: a restart/deploy mid-turn loses that message (Evolution does not retry). Needs a durable queue before real clients.
4. Adapter retries `sendText` on any exception including timeouts after Evolution already accepted → possible duplicate message. Inherent without idempotency keys; keep attempts low.
5. Payments: `confirm_payment` updates order then payment in two writes (payment row can stay `pending` if the second fails); checkout reuses the same reference on retry (Paystack rejects duplicates if the first init actually succeeded); cancelling a checkout doesn't void the Paystack link (now safe thanks to fix 1, but staff will see it).
6. `_seed_catalog`: for existing products, modifier/option changes and price changes to options are **not** applied on re-activation, and products removed from config stay orderable.
7. Nudge dry-run **advances `step`/status by design** (existing test). Before flipping `dry_run` to false, reset `nudge_state` for open conversations or they will skip early templates.
8. Gemini embeddings use `RETRIEVAL_QUERY` for documents too (unverified quality impact; test `RETRIEVAL_DOCUMENT` for indexing). Also test whether `max_tokens=600` truncates JSON on "thinking" models: watch for `LLM_UNPARSEABLE_RESPONSE` / "empty response". Confirm model names in `.env` against the live providers (pd already had both providers decommissioned at once).
9. In-process lock dict grows per `tenant:phone` (small leak); `pause`/`resume` admin endpoint is two non-atomic writes; no retention/erasure endpoint (NDPR).

## 4. Next steps (ordered)
**Do first (you, ~30 min)**
1. Rotate the exposed keys; run `python scripts/vault_check.py [orphan_key]`; then decide the vault key.
2. Run `migrations/0002_…sql` in the Supabase SQL editor. If RPC returns 404 afterwards: `notify pgrst, 'reload schema';`. **Do not deploy the new code before 0002** (activation calls `activate_config_tx`).
3. Restart uvicorn; re-activate the test config (should now stamp `activated_at`); check `LLM_PROVIDER_FAILED … cooldown_s` appears once, then fast fallback turns.

**Then (original handoff items, unchanged in intent)**
4. Real staff number → test `resume <n>`, `pause <n>`, alerts. 5. Drive a booking; `GET /v1/tenants/{id}/bookings`, `POST …/bookings/{id}/confirm` with a naive local time (`{"when":"2026-10-20T15:00:00"}`) and check the customer sees the right hour.
6. Second tenant on the `order` pack + Paystack **test** keys; test pay-then-wait-45-min-then-webhook (the new late-payment path), underpayment, redelivery.
7. `POST /internal/tick` (header `x-cron-secret`) with dry-run on; call it twice quickly → second returns `already_running`.
8. Containerize: `deploy/docker-compose.hooze-core.yml` (**untested**; container-to-container URLs explained in the file).
9. Then: durable inbound queue (3.3), Upstash for >1 worker, onboarding collection layer, operator dashboard, quotas.

## 5. Verification matrix
| Verified by execution on real Postgres 16 | Verified only by fake-DB tests | Not verified at all |
|---|---|---|
| 0001 applies; `confirm_booking_tx`; `23505` vs `23514`; 0002 `activate_config_tx` incl. rollback + permissions; `match_knowledge` exactness | everything else in `tests/` (187) | Paystack, order pack live, real tick, multi-worker, Docker compose, pgvector ≥ 0.8, prompt quality at scale |

Caveat: scratch DB is plain Postgres, not Supabase (no PostgREST layer, so JSON param passing of the new RPC is untested end to end).

## 6. Files changed
New: `migrations/0002_…sql`, `tests/test_hardening.py`, `scripts/vault_check.py`, `deploy/docker-compose.hooze-core.yml`, `.dockerignore`, this doc.
Edited: `capabilities/{booking,knowledge,ordering}.py`, `config_engine/activation.py`, `kernel/{db,locks}.py`, `llm/gateway.py`, `runtime/{outbox,scheduler,store,tenant_runtime}.py`, `security.py`, `settings.py`, `tests/{fakes,test_schema}.py`, `README.md`, `docs/ARCHITECTURE.md`.
