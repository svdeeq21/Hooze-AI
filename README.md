# Hooze Platform

Multi-tenant conversational-commerce runtime for WhatsApp. One codebase, N businesses; a tenant is a compiled config row.
See **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** for the design, audit traceability, and — importantly — §8, what is *not* verified.

## Run the tests (no network, no Supabase needed)
```bash
pip install -r requirements-dev.txt
python -m pytest            # 506 tests, ~60s  (Windows: set PYTHONUTF8=1 first)
```

## Layout
`hooze/{api,runtime,capabilities,config_engine,packs,llm,channels,payments,kernel}` · `migrations/` · `tests/` · `docs/`

## Bring up a staging environment
1. Create a Supabase project; run **every** file in `migrations/` in order (`0001` … `0009`). Each later file is required by the code: `0002` activation, `0003` the inbound queue, `0004` dashboard counts, `0005` menu categories and product photos/videos (activation still works without it, but menus have no sections or media until it is run), `0006` API key roles, `0007` setup drafts, `0008` operating modes, test chat, demo links and weekly reports, `0009` digital products.
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

## Onboarding: drafts before connections
Business setup is saved **independently of going live**: the Setup form autosaves a draft (`PUT /v1/tenants/{id}/config/draft`)
after every quiet moment, with nothing connected — no WhatsApp, no Paystack. Closing the tab loses nothing; reopening Setup loads
the draft (badge: *draft*), which can be discarded back to the live version. "Go live" is still gated on full readiness, and a
successful activation deletes the draft so it can't shadow the live config. Onboard in whatever order suits the client:
business info first, WhatsApp pairing whenever the phone is in hand.

## Conversation behaviour (what the first real chats taught us)
- **Captions count.** A photo/video/document sent **with a caption** ("how much is this one?") is answered as if the caption were
  typed; only wordless media gets the canned "could you describe that?" reply. Text inside disappearing-message chats and
  view-once media (ephemeral/viewOnce wrappers) is read too — it used to parse as unreadable.
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

## Demo a prospect in 10 minutes (no WhatsApp needed)
Seven ready-made businesses live in `templates/demo/` (restaurant, retail, real estate, clinic, salon, school, digital products). One command creates the
client, puts it live with real AI replies, and prints a private demo link for the prospect:
```bash
PLATFORM_ADMIN_KEY=... python -m scripts.demo_tenant --template salon --name "Their Salon" --staff-phone 080... --api https://your-api --go-live
```
The link opens `https://<dashboard>/try/<token>`: a chat page that needs no sign-in, with "behind the scenes" (what was captured, what
the owner's phone receives). Each link expires and has a message allowance. Set `VITE_API_URL` when building the dashboard so the page
knows where the API is. Pair a WhatsApp number later to put the same client on a real number.

## Modes, test chat and owner reports
- **Operating modes** (Setup → section 2): *Full auto* (default); *After-hours*: silent during opening hours, answers (and takes orders)
  while you're closed; *Assist*: every reply waits in Conversations for a person to send, edit or discard.
- **Test chat** (client → Test chat tab): talk to the live assistant with real AI replies. Nothing is sent on WhatsApp, the team isn't
  texted (alerts are shown in the page), payments are simulated. Test customers use made-up `000…` numbers; "Clear all test chats"
  removes them. A client with only the test chat enabled can go live before its WhatsApp number is paired.
- **Weekly report**: every Monday from 08:00 (client's timezone) the `/internal/tick` sweep sends each client's staff numbers their
  week in numbers. Preview or send now from the client's Overview tab. `WEEKLY_REPORT_ENABLED=false` turns it off.
- Needs `migrations/0008_modes_demo_reports.sql`. Security review of this release: `docs/SESSION6_LAUNCH_AUDIT.md`.

## Listings: links, photos, videos and brochures
Each offering (a property, service or product) can carry a **link** (shown on its card) and **photos, MP4 videos and PDF brochures**
(Setup → section 3; PDFs upload like photos). When a customer asks to see one ("send pictures", "yes" to "want to see it?"), the
assistant sends the card and every file. It looks records up using the whole conversation (not only the last message), so short
follow-ups keep working, and it is told which records really have photos.

## Digital products (e-books, courses, templates)
Give a product a **download link** (Setup → section 3, order businesses). Orders made only of digital products skip
"pickup or delivery?", and the link is sent automatically the moment Paystack confirms the payment, never before (the AI never sees
it). Needs `migrations/0009_digital_products.sql`.

## Adding a new kind of business
- Fits an existing goal (`order` / `consult_and_book`)? **Write a `BusinessConfig`. No code.**
- New sales process? One pack in `hooze/packs/` (a playbook + default capabilities) and, if needed, one capability subclass + `@register`.

## Menu photos and videos

In **Setup → Menu / products**, each item has **Add photos / videos** (click or drag files in), and the section has **Bulk upload photos**: drop a folder of files and any named like an item (`jollof-rice.jpg`, `jollof-rice-2.jpg`) attach to it automatically; you confirm the matches, then upload.

- Files go to a public Supabase Storage bucket (`MEDIA_BUCKET`, default `media`, created on first upload) in a folder per client (`<tenant_id>/<random>.jpg`). The database stores only the link.
- Photos (JPG/PNG/WebP/GIF) are rotated upright, shrunk to 1600 px, converted to JPEG and stripped of metadata. Videos must be MP4 under 16 MB (WhatsApp's limit); they are validated, not re-encoded. HEIC and .mov are refused with instructions.
- The file's own bytes decide its type, never its name. A client's key can only upload into that client.
- Needs `pip install -r requirements.txt` (adds `python-multipart` and `Pillow`) and migration `0005`.

