# Hooze Platform — Architecture

One deployment, many businesses. A tenant is **data** (a compiled config row), never a fork.

## 1. Why this exists

`pd-crm-backend` (real-estate CRM) and `savoury-spud-backend` (food ordering) are the same system wearing different clothes:
Savoury was forked from PD's patterns and even kept byte-identical files (`distributed_state.py`, `retry.py`). Each new client
would have been a third fork, with every bug fixed N times. This repo extracts the shared spine and turns everything
client-specific into configuration.

| Stage of a turn | pd (real estate) | savoury (food) | Here |
|---|---|---|---|
| Dedupe + per-contact lock | `claim` + Redis lock | same | `runtime/store.claim_inbound`, `kernel/locks` |
| Understand the message | regex intent engine + LLM | LLM strict-JSON actions | one LLM call → `{actions, reply, intent}` |
| Collect facts | `qualification_filter` (Abuja regex) | draft-item modifiers | `qualification` / `ordering` capabilities |
| Ground the answer | RAG over listings | menu injected in prompt | `knowledge` / `ordering` `context()` |
| Change state | 10-state Python function | none (implicit) | `PlaybookSpec` — a data graph |
| Guard the reply | leak / similarity / claim checks | "added!" claim strip | `runtime/guards` + per-capability `check_reply` |
| Escalate | `escalation.py` | admin commands | `handoff` capability (mandatory) |
| Follow up | `follow_up_engine` | none | `nudges` capability |

## 2. Design decisions (each traces to something that actually broke)

1. **LLM proposes, code disposes.** The model returns strict JSON; every action is looked up in a tool registry and
   schema-validated. Hallucinated tools/products become logged no-ops. A tool may return an *authoritative* message that
   replaces the model's draft ("Adding it to your cart!" when nothing was added was a real Savoury bug), or raise
   `BusinessRuleError` to halt the turn with a message the customer must hear.
2. **Tenant isolation is structural.** `TenantDB` injects `tenant_id` on every insert and filters every select/update/
   delete; it refuses global tables. The database backs it up with composite `(tenant_id, id)` foreign keys and deny-all RLS.
   A forgotten `.eq("tenant_id", …)` cannot be written.
3. **Sales process selects behaviour, industry never does.** `sales_process.goal` (`order` | `consult_and_book`) picks a pack
   (playbook + capability defaults). A dental clinic and a real-estate agency compile to the *same* machinery
   (`test_same_goal_same_machinery_industry_is_only_a_label`).
4. **Fail closed.** A channel cannot exist without a webhook secret (`secret_hash NOT NULL`, length-checked); verification of
   a channel with no secret returns `False`; `/internal/tick` is unusable if its secret is unset; comparisons use `compare_digest`.
5. **Money is integer minor units** (kobo) end to end. Amounts are re-verified with the provider *and compared*, not just status-checked.
6. **Retry wraps only the network call.** Bookkeeping after a confirmed send is never inside a retried function. Nudges
   *reserve before sending* so a failed bookkeeping write can cost a send but never cause a resend loop.
7. **Playbooks are data and are validated.** `validate_spec()` proves initial state exists, targets exist, every state is reachable.
   pd's "never back to DISCOVERY after PITCH" is now the *absence of an edge* (tested structurally).
8. **Human handoff is mandatory.** Injected for every tenant regardless of config; the pipeline's failure paths (LLM down,
   qualification stuck) depend on it.
9. **A config cannot go live unvalidated or half-seeded.** `activate()` validates → compiles → seeds → *then* flips active; any
   failed embedding aborts (503, retryable) and the previous config keeps serving.

## 3. Layers

```
 api/            webhooks (whatsapp, payments) · admin (tenants, channels, config, bookings, usage) · /internal/tick
 runtime/        pipeline (turn orchestration) · playbook · actions (tool registry/executor) · guards · prompt · store · messenger · outbox · scheduler
 capabilities/   handoff* · qualification · knowledge · booking · nudges · availability · ordering      (* mandatory)
 config_engine/ schema (BusinessConfig) → validator → compiler → activation          packs/  consult_and_book · catalog_order
 llm/            gateway (provider chain, metering) · transcription        channels/  base · evolution · formatting        payments/  base · paystack
 kernel/         db (TenantDB/PlatformDB) · events · locks · retry            security.py (secrets, Vault)      settings.py (platform-only)
```

## 4. Turn lifecycle (`runtime/pipeline.py`)

1. Verify + parse (API layer). Ack immediately; process in the background.
2. Load tenant, active compiled config; per-(tenant, phone) lock.
3. Staff number? → deterministic command router (never the LLM).
4. Get/create contact + conversation → optional voice transcription → **idempotent claim** (DB unique index).
5. `on_inbound` hooks (nudge reset / opt-out) *before* any AI work.
6. Paused/handoff? stop. Non-text media? canned reply. First contact? deterministic welcome. Rate limit.
7. **Gates** (closed hours, payment pending, human request) — the LLM never sees these turns.
8. Build prompt (platform rules + persona + stage goal + capability contexts + tools) → `interpret` → execute actions.
9. Choose reply: authoritative override, else LLM text → capability `check_reply` → leak guard → deterministic next-step line.
10. Send → advance playbook → `after_turn` hooks → rolling summary. Emit `TURN_COMPLETED` with per-stage timings.

## 5. Tenant lifecycle (maps to the Hooze SaaS design)

`Payment → onboarding → BusinessConfig → validate → compile → seed → activate → live`

| Design-doc step | Status |
|---|---|
| Structured Business Configuration | **Built** — `config_engine/schema.py` |
| Validation ("pricing defined? escalation? hours? claims? integrations?") | **Built** — `validator.py`, blocking vs warning, completeness score |
| Automated deployment (knowledge + catalog + persona + playbook) | **Built** — `compiler.py`, `activation.py` |
| Channel connect + per-channel webhook secret | **Built** — admin API |
| Payment integration per tenant (encrypted) | **Built** — Paystack |
| **Onboarding collection** (guided form / AI interview / doc extraction) | **Not built.** Only the *consumer* exists. Anything that can emit a `BusinessConfig` plugs in. |
| Evolution instance provisioning, QR pairing | **Not built** (channel row is created by API; the instance itself is manual) |
| Control Center dashboard / customer dashboard | **Not built** — admin API exists to back one |
| Billing / quotas | **Metering only** (`usage_events`, `GET …/usage`); nothing enforces limits |

## 6. Where each audit finding is closed

| Finding | Closed by | Verified by |
|---|---|---|
| C1 webhook auth disabled | `EvolutionAdapter.verify` fail-closed; `channels.secret_hash NOT NULL` | `test_webhook_*`, mutation-tested |
| C2 sync calls in async paths | gateway is fully async (no sync path exists) | by construction |
| C3 unbounded LLM context | `history_window` + rolling summary | `test_rolling_summary_*` |
| C4 stuck leads (regex missed ₦150,000,000 / "Jabi") | LLM proposes slots; code only validates/normalises; stuck → human | `test_stuck_lead_*`, `test_money_*` |
| H3 retry re-sends | adapter `_post` only; Messenger order; nudge reserve-before-send | `test_reserve_before_send_*`, `test_send_failure_*` |
| H5 non-atomic booking write | `confirm_booking_tx` (booking + transcript row) | `test_human_confirms_via_atomic_rpc_*` (fake mirrors the SQL) |
| H6 no multi-tenancy | `TenantDB`, composite FKs, RLS | `test_tenantdb_*`, `test_schema` |
| H8 `LIMIT 20` before ranking | `match_knowledge` ranks a MATERIALIZED, tenant-filtered set (exact) — `0002`; the `0001` form could return 2 of 20 under an HNSW plan | executed on real Postgres vs an index-free ground truth |
| H9 sequential embedding froze the app | `sync_documents` bounded concurrency + hash diff | `test_sync_*` |
| H10 RPCs missing from migrations | all RPCs defined; consistency test | `test_schema.py` |
| M3/M4 bad dates / auto booking time | ISO validated first; `suggested_at` is a guess, `confirmed_at` is human | `test_invalid_confirm_*` |
| M6 "you can call me John" escalates | request-shaped patterns + lookbehinds | `test_call_me_*` |
| M7 hard-coded human name | `handoff.contact_name` config | `test_explicit_human_request_*` |
| M12 CORS wildcard | no CORS middleware at all; add an *allow-list* when a dashboard exists | — |
| M13 non-constant-time key compare | `hmac.compare_digest` everywhere | code review |
| H7 dead outreach subsystem, M8 outreach caps | **Not ported** (belonged to a different product) | — |

## 7. Data model

`tenants` · `channels` (kind, instance, encrypted key, `secret_hash`) · `api_keys` (hashed, revocable, per-person) ·
`tenant_configs` (versioned: `config` = BusinessConfig, `compiled` = RuntimeConfig, one `active`) · `tenant_integrations` ·
`contacts` (slots in `attributes.slots`) · `conversations` (state, status, rolling summary) · `messages` (idempotency index) ·
`nudge_state` · `knowledge_items` (pgvector) · `products/modifier_groups/modifiers` · `orders/order_items/payments` ·
`availability` · `bookings` · `events` · `usage_events`.

## 8. What is NOT verified or NOT built — read before trusting this in production

**Verification status — updated 2026-10-13 (supersedes the first bullet below where they differ)**
- **Now verified against real infrastructure** (Session 2 + re-audit): webhook auth, tenant/config load, contact/conversation creation + idempotency, `match_knowledge` against real pgvector, Gemini→Groq fallback, guarded reply, playbook advance, send-then-record, on a real Supabase project + Dockerized Evolution.
- **Also executed on a scratch Postgres 16 + pgvector 0.6 (re-audit):** `0001` applies cleanly (20 tables); `confirm_booking_tx` (atomic transcript row, double-confirm no-op, wrong-tenant no-op); the idempotency index raises SQLSTATE `23505` and a check-constraint failure raises `23514`; `0002` (`activate_config_tx` rollback under a forced failure; `match_knowledge` equals an index-free ground truth 10/10).
- **Found and fixed in the re-audit** (each has a regression test in `tests/test_hardening.py`): late payments after expiry/cancel were swallowed as "duplicate"; any DB error while claiming an inbound message was reported as a duplicate (message silently dropped); out-of-turn sends (nudges, booking confirmations, receipts) were never recorded in the transcript; config activation was not atomic and never set `activated_at`; naive booking times were stored as UTC; a failing LLM provider was retried (and timed out) on every turn, twice per turn including the embedding; overlapping cron ticks were possible; and `match_knowledge` returned 2 of 20 records whenever the planner chose the HNSW index (the H8 claim held only by planner luck).
- **Still NOT verified:** Paystack (test or live), the `order` pack end to end on live infra, `/internal/tick` against real data, more than one worker / Upstash, real latency tuning of the prompts, `RETRIEVAL_DOCUMENT` vs `RETRIEVAL_QUERY` embedding task types (documents are currently embedded with the query task type), pgvector >= 0.8 behaviour (scratch DB had 0.6.0).

**Verification gaps (original, 2026-09-24 — partly superseded above)**
- **Never run against a real Supabase/Postgres.** The 171 tests use an in-memory fake that enforces the same unique/partial-unique
  constraints, but it is not Postgres. The migration was syntax-checked with `pglast`; the `set_updated_at` trigger body could not
  be machine-checked (tool bug, reproduced on a trivial function). `match_knowledge` (pgvector, `hnsw`) and `confirm_booking_tx`
  have **not been executed**. First step in staging: apply the migration, then run the flow in the README.
- **Never run against real Evolution API, Paystack, Gemini or Groq.** Parsing/verification logic is unit-tested with recorded-shape
  payloads; HTTP behaviour is not. Paystack signature construction is tested against the documented HMAC-SHA512 recipe only.
- Prompts are *plausible*, not *tuned*. They are distilled from two repos' real-transcript fixes, but nothing here has been
  evaluated on real conversations. Expect a tuning pass per vertical. Nudges ship in **dry-run** for this reason.

**Functional gaps vs pd** (deliberate scope cuts, but real)
- **No structured filtering in retrieval.** `knowledge` is semantic-only. pd's budget/bedroom-aware `search_by_preferences` is not
  ported: a ₦150m budget is *shown* to the model but nothing deterministically excludes a ₦300m listing (the prompt tells it to flag
  mismatches). Fix: add typed filter columns to `knowledge_items` + a `p_max_price` argument to `match_knowledge`.
- Not ported: lead scoring/temperature, dashboard endpoints, Firebase listing sync (needs a `KnowledgeDoc` mapper, ~40 lines),
  WhatsApp-driven inspection admin commands (the API covers confirm/decline).
- Voice notes: English only (Hausa/Yoruba are rejected, not guessed).

**Operational risks**
- **Evolution API is an unofficial WhatsApp Web bridge.** Numbers get banned; interactive messages break on WhatsApp updates.
  That is acceptable for pilots and a business risk at SaaS scale. `ChannelAdapter` exists so a **Meta Cloud API** adapter is one
  class — but Cloud API has a 24h window and template rules that the `nudges` capability must then respect.
- **Single-process by default.** Locks/cache are in-memory until `UPSTASH_*` is set; run **one worker** until then.
- **DB round-trips per turn (measured against the fake DB, sequential):** 12–15 for a consult turn, 19–24 for an ordering turn
  (`TURN_COMPLETED.timings` gives real latency in staging). Production already skips the config read via the 30s `ConfigLoader` cache;
  the `tenants` row is still read every turn and the menu every ordering turn. Cache both and batch reads before hundreds of tenants.
- **Prompt injection** via customer text or ingested knowledge is bounded, not eliminated: the model can only *propose* registered
  actions, every write is validated, money is computed in code, and replies pass guards. It can still say something silly.
- **Compliance:** no data-retention or deletion endpoint yet (NDPR/GDPR-style erasure); no per-tenant BYO LLM keys; no quota enforcement.
- `VAULT_MASTER_KEY` loss = all tenant credentials unrecoverable. Back it up separately from the database.

## 9. Migrating the two existing clients (strangler, not big-bang)

1. Stand up the platform in staging; apply the migration.
2. Express **Praise Dynasty** as a `BusinessConfig` (slots: budget/location/purpose; offerings from the Firebase feed via a mapper;
   booking label "inspection"). Express **Savoury Spud** (menu with modifier groups, hours, Paystack). Run each in *shadow mode*
   on a test number and compare transcripts against the live bot.
3. Cut over one WhatsApp instance at a time (webhook URL swap); keep the old service as the rollback for a week.
4. Only then backfill history (contacts/messages) — optional; both bots can start with a clean slate.
