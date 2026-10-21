# Hooze Platform

Multi-tenant conversational-commerce runtime for WhatsApp. One codebase, N businesses; a tenant is a compiled config row.
See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the design, audit traceability, and — importantly — §8, what is *not* verified.

## Run the tests (no network, no Supabase needed)
```bash
pip install -r requirements-dev.txt
python -m pytest            # 311 tests, ~7s
```

## Layout
`hooze/{api,runtime,capabilities,config_engine,packs,llm,channels,payments,kernel}` · `migrations/` · `tests/` · `docs/`

## Bring up a staging environment
1. Create a Supabase project; run **every** file in `migrations/` in order (`0001` … `0005`). Each later file is required by the code: `0002` activation, `0003` the inbound queue, `0004` dashboard counts, `0005` menu categories and product photos/videos (activation still works without it, but menus have no sections or media until it is run).
2. `cp .env.example .env` and fill it (generate `VAULT_MASTER_KEY`, `PLATFORM_ADMIN_KEY`, `INTERNAL_CRON_SECRET`).
3. `uvicorn hooze.main:app` (or the `Dockerfile`). One worker until `UPSTASH_*` is set.
4. Onboard a tenant with the platform key:
   ```bash
   H='x-api-key: $PLATFORM_ADMIN_KEY'
   curl -XPOST $BASE/v1/platform/tenants -H "$H" -d '{"slug":"acme","name":"Acme"}'                       # -> tenant id + tenant API key (shown once)
   curl -XPOST $BASE/v1/platform/tenants/$TID/channels -H "$H" \
        -d '{"instance_name":"acme","base_url":"https://evolution...","api_key":"..."}'                    # -> webhook_url + secret (shown once)
   curl -XPOST $BASE/v1/tenants/$TID/integrations/paystack -H "x-api-key: $TENANT_KEY" -d '{"secret_key":"sk_..."}'   # order tenants only
   curl -XPOST $BASE/v1/tenants/$TID/config/validate -H "x-api-key: $TENANT_KEY" -d @business_config.json  # what's still missing?
   curl -XPOST $BASE/v1/tenants/$TID/config/activate -H "x-api-key: $TENANT_KEY" -d @business_config.json
   ```
5. Point the Evolution webhook at the returned URL with header `x-hooze-secret: <secret>`; set Paystack's webhook to the returned URL.
6. Schedule `POST /internal/tick` (header `x-cron-secret`) every 15 min. Nudges start in **dry-run**; review `NUDGE_DRY_RUN_WOULD_SEND` events before enabling.

## Conversation behaviour (what the first real chats taught us)
- **One welcome.** If a client has a scripted welcome (Setup → Tone → Greeting), a plain "Hello" gets that welcome only; a real question gets the welcome, then the answer (the model is told not to greet again).
- **Bookings need a time.** The assistant asks "what day and time?" instead of sending the team a request with no time. A flexible answer ("whenever you're free") counts.
- **Cards once.** A full record card (details + photos) is sent at most once per customer per 24h; the assistant is told to answer in its own words and share a card only when asked.
- **AI output that can't be read** is retried once on the other provider, with a stricter instruction, before anything reaches the customer.
- **An AI outage no longer pauses the customer.** They get one "One moment" message; the queue retries (30s, 60s…); only if it persists (3 attempts) is a human alerted and the assistant paused for that customer.
- **Typing.** A reply waits once (Evolution shows "typing…" for it). It used to wait twice, with no indicator during the first wait.
- Phone numbers in Setup, alerts and `.env` can be written `08012345678`, `+234 801 234 5678` or `2348012345678`.
- What no code can fix: the *content*. The assistant only knows what is in the client's knowledge base. Trust answers (who you are, past clients, process) and industry-neutral product descriptions must be written there.

## Reliability: queue and alerts
- **No lost messages.** The webhook saves every customer message (table `inbound_events`) *before* acknowledging it. A recovery loop inside the API re-runs anything interrupted by a restart, crash or database blip (5 attempts, 30s→4min backoff, then it is marked failed and you are alerted; Retry is on the dashboard). A replay only answers a message nobody has answered yet, so customers don't get duplicates.
- **Alerts.** Set `ALERT_WHATSAPP_NUMBERS` and/or `ALERT_EMAIL_TO` + `RESEND_API_KEY` (see `.env.example`), restart, then Dashboard → Overview → *Send test alert*. You are told once per problem, in plain language, with what to do: failed messages, crashes, payment anomalies, a disconnected WhatsApp number, a client with no live setup, a repeatedly failing AI provider.
- **The app can't tell you it is down.** Point a free uptime monitor (UptimeRobot, Better Stack) at `GET /health/ready`: 200 only when the database answers.
- Run **one worker** until `UPSTASH_*` is set (alert de-duplication and the connection monitor are per-process).

## Operator dashboard
`ui/` is a web app that replaces the command line (clients, setup form, WhatsApp pairing, conversations + human takeover, bookings, orders, menu, payments, events). See `ui/README.md`. Try it with no keys: `python -m scripts.demo_server` + `cd ui && npm install && npm run dev`. The API side lives in `hooze/api/operator.py` (set `CORS_ALLOWED_ORIGINS` so the browser may call it).

## Operations helpers
- `python scripts/vault_check.py [extra_key…]` — reports which `VAULT_MASTER_KEY` can decrypt the credentials already stored (never prints keys or plaintext). Run before rotating or deleting any key.
- `python -m scripts.llm_check` — times each AI provider directly and shows the real error (use when replies are slow). Prints no keys.
- `deploy/docker-compose.hooze-core.yml` — compose override that runs hooze-core in the same stack as Evolution API (**untested**; see the comments in the file for the container-network URLs).
- `POST /internal/tick` returns `{"status": "already_running"}` if a previous sweep is still in progress (overlaps are refused, not queued).
- `LLM_PROVIDER_COOLDOWN_SECONDS` (default 30) — a provider that just failed is skipped for this long; `LLM_TIMEOUT_SECONDS` (default 20) is the per-call ceiling.

## Adding a new kind of business
- Fits an existing goal (`order` / `consult_and_book`)? **Write a `BusinessConfig`. No code.**
- New sales process? One pack in `hooze/packs/` (a playbook + default capabilities) and, if needed, one capability subclass + `@register`.

## Menu photos and videos

In **Setup → Menu / products**, each item has **Add photos / videos** (click or drag files in), and the section has **Bulk upload photos**: drop a folder of files and any named like an item (`jollof-rice.jpg`, `jollof-rice-2.jpg`) attach to it automatically; you confirm the matches, then upload.

- Files go to a public Supabase Storage bucket (`MEDIA_BUCKET`, default `media`, created on first upload) in a folder per client (`<tenant_id>/<random>.jpg`). The database stores only the link.
- Photos (JPG/PNG/WebP/GIF) are rotated upright, shrunk to 1600 px, converted to JPEG and stripped of metadata. Videos must be MP4 under 16 MB (WhatsApp's limit); they are validated, not re-encoded. HEIC and .mov are refused with instructions.
- The file's own bytes decide its type, never its name. A client's key can only upload into that client.
- Needs `pip install -r requirements.txt` (adds `python-multipart` and `Pillow`) and migration `0005`.

