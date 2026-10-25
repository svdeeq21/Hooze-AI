# Session 4 — bug-fix round (what changed, what to run, what is still open)

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
- **12** `no embedding provider available`: check `GEMINI_API_KEY` on the live server (run the llm check script). Not a code change.
- **18** A customer who messages two of a client's numbers gets two chats: decision needed.
- **22** Per-customer erasure / retention rule / privacy notice (the delete-client endpoint covers a whole client only).
- Cart prices are not re-priced if the menu price changes while a cart is open; an open cart's "in progress" draft can still name a removed product.
- Deleting a client does not delete its Evolution instance.
- Supabase Storage list/delete requests are tested against a mock, not the live service: run the cleanup dry run on a real client first.
- Dashboard UI has no controls yet for key roles, cleanup dry run or delete-client.
- `_seed_catalog` treats the config as the source of truth for the menu. When a CSV/sheet import manages `products` directly, it must not also be pruned by this sync.
