# Session 5 — pre-MVP audit (what was found, what changed, what to do on deploy)

Scope: full read of every module, all migrations, the operator UI, and the docs, looking for security, reliability
and flexibility gaps ahead of keeping an MVP permanently live for demos. Baseline: 443 tests passing. Now: **459
backend + 44 UI tests passing.**

## Deploy order
1. Run `migrations/0007_config_drafts.sql` (adds `tenant_configs.updated_at` + one-draft-per-tenant index) BEFORE deploying.
2. Deploy the API, then the dashboard (`ui/`).
3. **Rotate every platform secret — see `docs/SECURITY_ROTATION.md`.** The old `.env` (live values) travelled inside at
   least one hand-made zip of this repo. This is the single most important action in this round.
4. Point a free uptime monitor (UptimeRobot / Better Stack) at `GET /health/ready` if not already done. On Render's
   free tier this also keeps the service warm, so the first demo message doesn't hang ~50s on a cold start.

## Fixed in this round
| # | What | Why it mattered | Where |
|---|------|-----------------|-------|
| 1 | **Setup drafts.** `PUT/DELETE /v1/tenants/{id}/config/draft`; `GET …/config` now returns `{active, draft}`. The Setup form autosaves (1.2s debounce), shows a *draft* badge + saved time, loads the draft on reopen, offers Discard. Activation deletes the draft. | The only save was "Go live", which is blocked until WhatsApp/Paystack are connected — so setup done before connecting anything lived in a browser tab and died with it. Business info can now be completed first; WhatsApp connected whenever. | `api/admin.py`, `api/operator.py`, migration 0007, `ui/src/client/SetupTab.tsx`, `ui/src/lib/api.ts` |
| 2 | **Media captions are read.** A photo/video/document with a caption flows through the pipeline as the caption's text (transcript keeps `content_type=image`); only wordless media gets the canned reply. | Customers constantly send a product screenshot captioned "how much is this?" — the bot answered "I can't view images here" and ignored the question. | `channels/evolution.py`, `runtime/pipeline.py` |
| 3 | **Ephemeral / view-once unwrapping.** `ephemeralMessage`, `viewOnceMessage(V2…)`, `documentWithCaptionMessage` wrappers are descended (bounded, never loops). | Text sent in a disappearing-messages chat — common on WhatsApp Business — parsed as kind `other`: the customer got "Sorry, I didn't quite catch that" for every message. | `channels/evolution.py` |
| 4 | `.env` hygiene: removed a stray bare Fernet key on its own line and a duplicated `CORS_ALLOWED_ORIGINS`. | A loose secret in the file; the duplicate meant the first CORS line was silently ignored. | `.env` |
| 5 | Legacy `POST /v1/platform/tenants/{id}/channels` now 404s on an unknown tenant instead of 500ing on the FK. | Correct status codes for API consumers. | `api/admin.py` |
| 6 | `docs/SECURITY_ROTATION.md`: step-by-step rotation for every secret, in dependency order, with what breaks if skipped. | The `.env` exposure above. | docs |

## Judged sound (verified, not changed)
- **Auth**: fail-closed webhook verification (per-channel hashed secret + instance match, constant-time), hashed API
  keys with roles, constant-time platform key compare, `/internal/*` fail-closed, CORS allow-list, no Swagger in prod.
- **Tenancy**: `TenantDB` forced scoping + composite FKs + deny-all RLS; cross-tenant checked at the HTTP layer by tests.
- **Money**: webhook signature + server-side re-verify of status AND amount; ordered idempotent writes; conditional
  status transitions; late/double payment staff alerts; re-pricing at checkout with customer confirmation.
- **Reliability**: durable inbound queue (lease claims, backoff, replay-without-double-reply), send-retry only on
  provably-unsent failures, alert rules with dedupe + thresholds, LLM provider cooldown chain.

## Known limits, deliberately left (decide later)
- `channel_connected` readiness = "a channel row exists", not "the phone is actually paired". The dashboard's
  WhatsApp tab shows real connection state; the validator stays synchronous/cheap. A paired-state readiness check
  would need a live Evolution call per validate.
- Gemini is the only embedding provider (chat falls back to Groq; knowledge lookup doesn't). Known since Session 4.
- Alert WhatsApp failover can send operator alerts from a client's number if the dedicated ops number is down —
  deliberate (an alert that arrives beats channel purity). Configure `ALERT_CHANNEL_ID` with a dedicated ops number.
- Captioned media doesn't increment `user_message_count` (drives summary cadence / stuck detection): conservative,
  revisit if clients see mostly-image conversations.
- One uvicorn worker until `UPSTASH_*` is set (unchanged).
