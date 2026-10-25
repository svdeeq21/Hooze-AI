# Session 4 — bug-fix round (what changed, what to run, what is still open)

## ROUND 2 (read this first)
**Embeddings were failing because Google shut down `gemini-embedding-2-preview` on 10 Aug 2026.** Chat kept working (Groq fallback);
only knowledge/FAQ lookup died. The code now defaults to `EMBEDDING_MODEL=gemini-embedding-2` and supports both model families
(`-2` takes the task as text, `-001` takes `task_type`). Vectors from different models cannot be compared, so after deploying:
1. Set nothing (the new default applies) or set `EMBEDDING_MODEL=gemini-embedding-2` explicitly. Keep `EMBEDDING_DIMS=768`.
2. Deploy, then run `POST /v1/platform/embeddings/reindex` (or Settings -> Rebuild search index per client).
3. `python scripts/llm_check.py` now reports "EMBEDDINGS ARE FAILING" instead of hiding it.

Also new in round 2: one chat per customer across a client's numbers (`merge` tool for old split chats), checkout re-prices from the live
menu and asks the customer to confirm if a price changed, per-customer export/erase + optional message retention
(`MESSAGE_RETENTION_DAYS`), deleting a client also removes its Evolution instances, and Settings-tab controls for all of it.
Draft privacy notice: `docs/PRIVACY_NOTICE_DRAFT.md` (for a lawyer to review).

## ROUND 1

## Deploy order (IMPORTANT)
1. Run `migrations/0006_api_key_roles.sql` (adds `api_keys.role`). The new code writes that column, so run it BEFORE deploying.
2. Deploy the code.
3. Existing tenant keys become `staff`. Issue an `admin` key only where someone really needs to activate configs / change Paystack:
   `POST /v1/platform/tenants/{id}/api-keys {"label": "...", "role": "admin|staff|viewer"}`.
4. Re-activate each client's config once: knowledge documents are re-embedded in the correct (document) mode, and the menu is synced.
5. Existing nudge_state polluted by old dry-runs: `POST /v1/tenants/{id}/nudges/reset` once (new dry-runs no longer advance the step).

## Fixed
| # | What | Where |
|---|------|-------|
| 16 | Webhook secret only accepted in the `x-hooze-secret` header. `?token=` rejected (`WEBHOOK_QUERY_TOKEN_REJECTED`) unless `WEBHOOK_ALLOW_QUERY_TOKEN=true` | `api/webhooks.py`, `settings.py` |
| 15 | API key roles: admin / staff / viewer. Staff cannot activate configs, connect Paystack, reset nudges or upload media. Viewer is GET-only | `api/deps.py`, `admin.py`, `operator.py`, migration 0006 |
| 1,2 | Menu sync on activation: price/description/photos/options update in place; products and options not in the config are deleted; sold-out toggles kept; empty catalog changes nothing | `config_engine/activation.py` |
| – | Checkout refuses a cart holding an item that was removed / sold out / had a chosen option removed | `capabilities/ordering.py` |
| 6 | `confirm_payment`: payment row first, then order, both idempotent, so a provider retry finishes a half-done confirmation | `capabilities/ordering.py` |
| 7 | New Paystack reference per checkout attempt. A second different payment for a paid order raises `PAYMENT_DOUBLE_PAYMENT` + staff message | `ordering.py`, `alerts.py` |
| 3,4,5 | Orphaned uploads deleted after each save (24 h age gate, refuses without an active config). Dry run: `POST /v1/platform/tenants/{id}/media/cleanup`. New `DELETE /v1/platform/tenants/{id}?confirm=<slug>` (must be suspended; purges files first) | `media/cleanup.py`, `media/storage.py`, `operator.py` |
| 11 | WhatsApp sends retried only on connect failure / 429, never on read timeout or 5xx (avoids duplicate messages) | `channels/evolution.py` |
| 10 | In-process lock table no longer grows forever (ref-counted, removed when idle) | `kernel/locks.py` |
| 9 | Pause/resume via one helper with ordered writes; failures are reported (HTTP 500 / staff WhatsApp message) instead of crashing | `capabilities/handoff.py`, `admin.py` |
| 13 | Gemini: documents embedded as `RETRIEVAL_DOCUMENT`, questions as `RETRIEVAL_QUERY`; `EMBED_RECIPE` in the content hash forces a one-time re-embed | `llm/gateway.py`, `capabilities/knowledge.py` |
| 14 | Interpretation retry gets 1800 tokens (first try 900) | `llm/gateway.py` |
| 19 | Staff alerts fail over across all active numbers in order | `runtime/outbox.py` |
| 21 | Nudge dry-run no longer advances `step`, so going live needs no reset | `capabilities/nudges.py` |
| 20 | Demo server serves its uploads at `/demo-media/...` | `scripts/demo_server.py` |

## Still open
- **Provider outage drill:** Gemini is the only embedding provider. If it fails, chat works but knowledge lookup is down (`KNOWLEDGE_LOOKUP_FAILED` alert fires). A second embedding provider would need its own vector column; not built.
- **Privacy:** the legal parts (retention period, notice wording, processor agreements, registration) need a lawyer; the code only does what you decide. Logs/events are not scrubbed of anything beyond what is already masked.
- **Retention** deletes messages and the summaries built from them; orders/payments are never touched.
- Supabase Storage list/delete requests and Evolution delete are tested against mocks, not the live services: run the cleanup dry run on a real client first.
- Old split chats stay split until you run Settings -> Check for duplicates -> Join (or `POST /v1/platform/tenants/{id}/conversations/merge`).
